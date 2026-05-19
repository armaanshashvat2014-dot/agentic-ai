import os
import re
import json
import tempfile
import threading
import http.server
import socketserver
import uuid
import zipfile
import io
import base64
import hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import streamlit as st

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="Forge AI",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

AI_ENDPOINT = "https://raujzsawwpmixwlcgcgs.supabase.co/functions/v1/public-ai-api"
AI_KEY      = os.getenv("COMPLEX_AI_KEY", "")

PREVIEW_PORT = 8765


# ─────────────────────────────────────────────
# URL SCRAPER
# ─────────────────────────────────────────────
SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def scrape_url(url: str, timeout: int = 15) -> dict:
    result = {
        "url": url, "title": "", "html_raw": "", "inline_styles": "",
        "inline_scripts": "", "linked_css_urls": [], "linked_js_urls": [],
        "text_content": "", "meta": {}, "error": None,
    }
    try:
        resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        raw = resp.text

        m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
        result["title"] = m.group(1).strip() if m else ""

        for m in re.finditer(
            r'<meta\s+(?:name|property)=["\']([^"\']+)["\'][^>]*content=["\']([^"\']*)["\']',
            raw, re.I
        ):
            result["meta"][m.group(1)] = m.group(2)

        styles = re.findall(r"<style[^>]*>(.*?)</style>", raw, re.I | re.S)
        result["inline_styles"] = "\n\n".join(styles)[:30_000]

        scripts = re.findall(
            r"<script(?![^>]*\bsrc\b)[^>]*>(.*?)</script>", raw, re.I | re.S
        )
        result["inline_scripts"] = "\n\n".join(scripts)[:30_000]

        css_links = re.findall(
            r'<link[^>]+rel=["\']stylesheet["\'][^>]*href=["\']([^"\']+)["\']', raw, re.I
        )
        result["linked_css_urls"] = [urljoin(url, h) for h in css_links[:8]]

        js_links = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', raw, re.I)
        result["linked_js_urls"] = [urljoin(url, s) for s in js_links[:8]]

        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        result["text_content"] = text[:15_000]
        result["html_raw"] = raw[:80_000]

    except Exception as e:
        result["error"] = str(e)

    return result


def fetch_linked_asset(url: str, max_bytes: int = 40_000) -> str:
    try:
        resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=10)
        resp.raise_for_status()
        return resp.text[:max_bytes]
    except Exception:
        return ""


def build_scrape_context(scrape: dict, fetch_assets: bool = True) -> str:
    if scrape.get("error"):
        return f"[Scrape failed: {scrape['error']}]"

    parts = [
        "=== SCRAPED PAGE ===",
        f"URL: {scrape['url']}",
        f"Title: {scrape['title']}",
    ]
    if scrape["meta"]:
        parts.append("Meta: " + json.dumps(scrape["meta"], ensure_ascii=False)[:500])
    parts.append(f"\n--- Visible Text (excerpt) ---\n{scrape['text_content'][:4000]}")
    if scrape["inline_styles"]:
        parts.append(f"\n--- Inline CSS ---\n{scrape['inline_styles'][:8000]}")
    if scrape["inline_scripts"]:
        parts.append(f"\n--- Inline JS ---\n{scrape['inline_scripts'][:8000]}")
    if fetch_assets and scrape["linked_css_urls"]:
        parts.append("\n--- Linked CSS files ---")
        for css_url in scrape["linked_css_urls"][:3]:
            content = fetch_linked_asset(css_url)
            if content:
                parts.append(f"/* {css_url} */\n{content[:6000]}")
    parts.append(f"\n--- Full HTML (first 30 KB) ---\n{scrape['html_raw'][:30_000]}")
    return "\n".join(parts)


# ─────────────────────────────────────────────
# VIRTUAL FILESYSTEM
# ─────────────────────────────────────────────
class VirtualFS:
    @property
    def _proj(self) -> dict:
        ap = st.session_state["active_project"]
        return st.session_state["projects"][ap]

    @property
    def files(self) -> dict:
        return self._proj["vfs_files"]

    @property
    def root(self) -> str:
        return self._proj["vfs_root"]

    def write(self, path: str, content: str):
        self._proj["vfs_files"][path] = content
        disk_path = Path(self.root) / path
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        disk_path.write_text(content, encoding="utf-8")

    def read(self, path: str) -> str | None:
        return self._proj["vfs_files"].get(path)

    def delete(self, path: str):
        self._proj["vfs_files"].pop(path, None)
        disk_path = Path(self.root) / path
        if disk_path.exists():
            disk_path.unlink()

    def list_files(self) -> list[str]:
        return sorted(self._proj["vfs_files"].keys())

    def clear(self):
        self._proj["vfs_files"] = {}
        root = Path(self.root)
        for f in root.rglob("*"):
            if f.is_file():
                f.unlink()

    def get_entry_html(self) -> str | None:
        for candidate in ("index.html", "app.html", "main.html"):
            if candidate in self.files:
                return self.files[candidate]
        for path, content in self.files.items():
            if path.endswith(".html"):
                return content
        return None

    def inject_css_js(self, html: str) -> str:
        def replace_link(m):
            href = m.group(1)
            css = self.files.get(href) or self.files.get(href.lstrip("./"))
            if css:
                return f"<style>{css}</style>"
            return m.group(0)

        def replace_script(m):
            src = m.group(1)
            js = self.files.get(src) or self.files.get(src.lstrip("./"))
            if js:
                return f"<script>{js}</script>"
            return m.group(0)

        html = re.sub(r'<link[^>]*href=["\']([^"\']+\.css)["\'][^>]*/?>',
                      replace_link, html)
        html = re.sub(r'<script[^>]+src=["\']([^"\']+\.js)["\'][^>]*></script>',
                      replace_script, html)
        return html

    def to_zip_bytes(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path, content in self.files.items():
                zf.writestr(path, content)
        return buf.getvalue()

    def to_single_html(self) -> str:
        html = self.get_entry_html() or "<html><body><p>No HTML found.</p></body></html>"
        return self.inject_css_js(html)


# ─────────────────────────────────────────────
# GITHUB API CLIENT (no gitpython needed)
# ─────────────────────────────────────────────
class GitHubClient:
    """
    Pure REST-based GitHub integration.
    Supports: create repo, push files, enable Pages, create PRs.
    """
    BASE = "https://api.github.com"

    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }

    def _get(self, path: str) -> dict:
        r = requests.get(f"{self.BASE}{path}", headers=self.headers, timeout=20)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        r = requests.post(f"{self.BASE}{path}", headers=self.headers,
                          json=body, timeout=30)
        r.raise_for_status()
        return r.json()

    def _put(self, path: str, body: dict) -> dict:
        r = requests.put(f"{self.BASE}{path}", headers=self.headers,
                         json=body, timeout=30)
        r.raise_for_status()
        return r.json()

    def _patch(self, path: str, body: dict) -> dict:
        r = requests.patch(f"{self.BASE}{path}", headers=self.headers,
                           json=body, timeout=20)
        r.raise_for_status()
        return r.json()

    def whoami(self) -> str:
        return self._get("/user")["login"]

    def repo_exists(self, owner: str, repo: str) -> bool:
        try:
            self._get(f"/repos/{owner}/{repo}")
            return True
        except Exception:
            return False

    def create_repo(self, name: str, private: bool = False, description: str = "") -> dict:
        return self._post("/user/repos", {
            "name": name,
            "description": description or f"Built with Forge AI ⚡",
            "private": private,
            "auto_init": False,
        })

    def get_default_branch(self, owner: str, repo: str) -> str:
        data = self._get(f"/repos/{owner}/{repo}")
        return data.get("default_branch", "main")

    def get_branch_sha(self, owner: str, repo: str, branch: str) -> str | None:
        try:
            data = self._get(f"/repos/{owner}/{repo}/git/refs/heads/{branch}")
            return data["object"]["sha"]
        except Exception:
            return None

    def get_file_sha(self, owner: str, repo: str, path: str, branch: str) -> str | None:
        try:
            data = self._get(f"/repos/{owner}/{repo}/contents/{path}?ref={branch}")
            return data.get("sha")
        except Exception:
            return None

    def upsert_file(self, owner: str, repo: str, path: str,
                    content: str, branch: str, message: str) -> dict:
        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        body = {"message": message, "content": b64, "branch": branch}
        existing_sha = self.get_file_sha(owner, repo, path, branch)
        if existing_sha:
            body["sha"] = existing_sha
        return self._put(f"/repos/{owner}/{repo}/contents/{path}", body)

    def create_branch(self, owner: str, repo: str, branch: str, from_sha: str):
        try:
            self._post(f"/repos/{owner}/{repo}/git/refs", {
                "ref": f"refs/heads/{branch}",
                "sha": from_sha,
            })
        except Exception:
            pass  # branch may already exist

    def init_repo_with_readme(self, owner: str, repo: str, branch: str = "main"):
        """Create an initial commit so the repo has a HEAD."""
        readme = f"# {repo}\n\nBuilt with [Forge AI](https://forge.ai) ⚡\n"
        self.upsert_file(owner, repo, "README.md", readme, branch, "Initial commit")

    def enable_pages(self, owner: str, repo: str, branch: str = "main") -> str | None:
        """Enable GitHub Pages on the given branch root. Returns the Pages URL."""
        try:
            r = requests.post(
                f"{self.BASE}/repos/{owner}/{repo}/pages",
                headers=self.headers,
                json={"source": {"branch": branch, "path": "/"}},
                timeout=20,
            )
            if r.status_code in (201, 409):
                # 409 = already enabled; fetch existing config
                info = requests.get(
                    f"{self.BASE}/repos/{owner}/{repo}/pages",
                    headers=self.headers, timeout=10,
                )
                if info.ok:
                    return info.json().get("html_url")
        except Exception:
            pass
        return None

    def create_pr(self, owner: str, repo: str, head: str, base: str, title: str, body: str) -> str:
        data = self._post(f"/repos/{owner}/{repo}/pulls", {
            "title": title, "body": body, "head": head, "base": base,
        })
        return data.get("html_url", "")

    def push_files(
        self,
        owner: str,
        repo: str,
        files: dict[str, str],
        branch: str = "main",
        commit_msg: str = "⚡ Forge AI — update",
        create_pr_to: str | None = None,
        enable_pages: bool = False,
        log_fn=None,
    ) -> dict:
        """
        Diff-aware multi-file push via GitHub Contents API.
        Returns {"branch": str, "pages_url": str | None, "pr_url": str | None,
                 "repo_url": str, "changed": int, "skipped": int}
        """
        def log(msg):
            if log_fn:
                log_fn(msg)

        log(f"🔍 Checking repo {owner}/{repo}…")

        # Ensure repo exists
        if not self.repo_exists(owner, repo):
            log(f"📦 Creating repo {repo}…")
            self.create_repo(repo)

        # Get or create default branch
        default_branch = "main"
        try:
            default_branch = self.get_default_branch(owner, repo)
        except Exception:
            pass

        base_sha = self.get_branch_sha(owner, repo, default_branch)

        # If repo is empty, push README first to create HEAD
        if base_sha is None:
            log("📄 Initialising repository…")
            self.init_repo_with_readme(owner, repo, default_branch)
            base_sha = self.get_branch_sha(owner, repo, default_branch)

        # Determine target branch
        target_branch = branch if branch != default_branch else default_branch
        if target_branch != default_branch:
            log(f"🌿 Creating branch `{target_branch}`…")
            self.create_branch(owner, repo, target_branch, base_sha)

        # Diff-aware push: only upload changed files
        changed = 0
        skipped = 0
        for path, content in files.items():
            # Compute remote SHA to detect changes
            remote_sha = self.get_file_sha(owner, repo, path, target_branch)
            local_sha = hashlib.sha1(
                f"blob {len(content.encode())}\0{content}".encode()
            ).hexdigest()

            if remote_sha and remote_sha == local_sha:
                skipped += 1
                continue

            log(f"{'✏️ Updating' if remote_sha else '➕ Creating'}  {path}…")
            try:
                self.upsert_file(owner, repo, path, content, target_branch,
                                 f"{commit_msg}\n\nUpdated {path}")
                changed += 1
            except Exception as e:
                log(f"⚠️  Skipped {path}: {e}")

        pages_url = None
        if enable_pages:
            log("🌐 Enabling GitHub Pages…")
            pages_url = self.enable_pages(owner, repo, target_branch)
            if pages_url:
                log(f"✅ Pages live at {pages_url}")

        pr_url = None
        if create_pr_to and target_branch != create_pr_to and changed > 0:
            log(f"📬 Opening PR → `{create_pr_to}`…")
            pr_url = self.create_pr(
                owner, repo, target_branch, create_pr_to,
                title=f"⚡ Forge AI — {commit_msg}",
                body="Automated update from [Forge AI](https://forge.ai). Review and merge when ready.",
            )
            if pr_url:
                log(f"✅ PR: {pr_url}")

        return {
            "branch": target_branch,
            "pages_url": pages_url,
            "pr_url": pr_url,
            "repo_url": f"https://github.com/{owner}/{repo}",
            "changed": changed,
            "skipped": skipped,
        }


# ─────────────────────────────────────────────
# AI CLIENT
# ─────────────────────────────────────────────
class AIClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _parse(self, data) -> str:
        if isinstance(data, dict):
            if "response" in data:  return data["response"]
            if "content"  in data:
                cnt = data["content"]
                if isinstance(cnt, list): return cnt[0].get("text", str(cnt))
                return cnt
            if "message" in data:   return data["message"]
            if "choices"  in data:  return data["choices"][0]["message"]["content"]
        return str(data)

    def ask(self, system: str, user: str, max_tokens: int = 4096) -> str:
        payload = {"messages": [{"role": "user", "content": f"{system}\n\n{user}"}]}
        resp = requests.post(
            AI_ENDPOINT,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=payload, timeout=180,
        )
        resp.raise_for_status()
        return self._parse(resp.json())

    def chat(self, conversation: list[dict]) -> str:
        full_messages = [
            {"role": "user",      "content": CHAT_SYSTEM_PROMPT + "\n\nAcknowledge these rules briefly."},
            {"role": "assistant", "content": "Understood. I am Forge AI, your web app building assistant. I only help with coding, building, and web development. What would you like to build?"},
        ] + conversation
        resp = requests.post(
            AI_ENDPOINT,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"messages": full_messages}, timeout=120,
        )
        resp.raise_for_status()
        return self._parse(resp.json())


# ─────────────────────────────────────────────
# AGENT PROMPTS  —  Lovable-class
# ─────────────────────────────────────────────
AGENT_SYSTEM_PROMPT = """IMPORTANT: You are Forge, an elite autonomous web application engineer operating at Lovable/v0/Cursor level. You build production-quality, shippable apps — not prototypes. You output ONLY raw JSON — no prose, no markdown, no explanations outside the JSON fields.

OUTPUT FORMAT — return this exact JSON shape and NOTHING ELSE:
{
  "summary": "one-line description of what was built or changed",
  "tech_stack": ["html", "css", "js"],
  "actions": [
    { "type": "create", "path": "index.html", "content": "..." },
    { "type": "edit",   "path": "style.css",  "content": "..." },
    { "type": "delete", "path": "old.js" }
  ]
}

═══════════════════════════════════════════
ENGINEERING STANDARDS (NON-NEGOTIABLE)
═══════════════════════════════════════════

1. ARCHITECTURE
   ─ Split every app: index.html + style.css + app.js (+ additional modules as needed)
   ─ Use ES modules (type="module") and import/export for all JS
   ─ Organise JS with clear layers: state management → business logic → UI rendering → event wiring
   ─ Singleton pattern for app state; pure functions for transformations
   ─ One-pagers only when explicitly trivial (landing page, static card, etc.)

2. CODE QUALITY
   ─ Write typed JSDoc on every exported function (param + return types)
   ─ Explicit error handling: try/catch, user-visible error states, never silent failures
   ─ Input validation on all user-facing forms (HTML5 + JS double-validation)
   ─ Never use innerHTML with user-supplied strings — use textContent or DOM APIs
   ─ Idempotent operations: running the same action twice should be safe
   ─ Guard all async operations: loading state → success/error → reset

3. UI / UX EXCELLENCE
   ─ Every app must feel COMPLETE: empty states, loading skeletons, error messages, success feedback
   ─ Responsive by default: mobile-first CSS with sensible breakpoints (480 / 768 / 1200px)
   ─ Keyboard navigable: tab order, focus styles, Enter/Space on interactive elements
   ─ ARIA roles on custom components (role="dialog", aria-label, aria-live for dynamic regions)
   ─ Colour contrast ≥ 4.5:1 for body text, ≥ 3:1 for large/UI text
   ─ Touch targets ≥ 44×44px
   ─ Smooth transitions (150–300ms ease) on interactive elements
   ─ Prefer CSS animations over JS for micro-interactions

4. VISUAL DESIGN
   ─ Design tokens via CSS custom properties on :root (never hardcode values inline)
   ─ Typography: pair a strong display font (Google Fonts) with a legible body font
   ─ Spacing scale: 4px base unit (4, 8, 12, 16, 24, 32, 48, 64, 96)
   ─ Tailwind CDN is allowed but optional; raw CSS is fine and often cleaner
   ─ Use CSS Grid for layout, Flexbox for alignment — not tables, not floats
   ─ Dark mode support via prefers-color-scheme media query at minimum
   ─ No placeholder lorem ipsum — write realistic copy that fits the domain

5. PERFORMANCE
   ─ Lazy-load heavy resources (images, large JS)
   ─ Debounce search/filter inputs (300ms)
   ─ requestAnimationFrame for canvas/scroll animations
   ─ Prefer CDN links from cdnjs.cloudflare.com, unpkg.com, or esm.sh
   ─ Keep total payload under 500 KB for simple apps

6. PRESERVATION RULE
   ─ You receive COMPLETE current file contents. Copy ALL existing content, then apply only the requested change.
   ─ NEVER remove features, functions, styles, or data unless explicitly asked.
   ─ Only include files that actually change — omit unchanged files.

═══════════════════════════════════════════
COMPONENT PATTERNS
═══════════════════════════════════════════

MODAL:
<div class="modal-overlay" role="dialog" aria-modal="true" aria-labelledby="modal-title">
  <div class="modal">
    <h2 id="modal-title">…</h2>
    <button class="modal-close" aria-label="Close">×</button>
  </div>
</div>
// JS: trap focus, Escape to close, lock body scroll

TOAST NOTIFICATIONS:
const toast = (msg, type='info') => { /* create, animate in, auto-dismiss 3s, remove */ }

FORM WITH VALIDATION:
// HTML5 required/pattern + JS constraint validation API
// Show inline errors next to each field, not just an alert
// Disable submit during async operation, restore on completion

DATA TABLE:
// Sort by column (ascending/descending toggle), client-side filter, pagination
// Empty state message, loading skeleton rows

DRAG AND DROP:
// HTML5 DnD API or Pointer Events — NOT SortableJS unless explicitly requested
// Visual feedback: drag ghost, drop zone highlight, drop animation

INFINITE SCROLL / PAGINATION:
// IntersectionObserver for infinite scroll; explicit page controls for pagination

═══════════════════════════════════════════
TECH CHOICES
═══════════════════════════════════════════

DEFAULT STACK (no framework):
- index.html with semantic HTML5
- style.css with CSS custom properties
- app.js as ES module entry point
- Additional .js modules for complex logic

WHEN TO USE LIBRARIES (CDN only, no build step):
- Charts       → Chart.js (cdnjs)
- Rich text    → Quill (cdnjs)
- Date picking → Flatpickr (cdnjs)
- Markdown     → marked.js (cdnjs)
- Syntax HL    → Prism.js (cdnjs)
- Icons        → Lucide (esm.sh) or Heroicons (SVG inline)
- 3D/Canvas    → Three.js (cdnjs) only when explicitly needed

NEVER USE (without explicit request):
- jQuery, Bootstrap, React, Vue, Angular, Svelte
- Any NPM package that requires a build step

═══════════════════════════════════════════
CLONE MODE (when scrape context is provided)
═══════════════════════════════════════════
- Faithfully reproduce layout, palette, typography, spacing, component hierarchy
- Upgrade: animations, responsiveness, accessibility, code cleanliness
- Use https://picsum.photos/{w}/{h} for image placeholders
- Strip: tracking pixels, cookie banners, ads, analytics, jQuery (rewrite in vanilla)
- Produce a fully self-contained build with zero external image dependencies

═══════════════════════════════════════════
AI-POWERED APP PATTERN
═══════════════════════════════════════════
Endpoint: POST https://raujzsawwpmixwlcgcgs.supabase.co/functions/v1/public-ai-api
Headers: { "Authorization": "Bearer FORGE_AI_KEY_PLACEHOLDER", "Content-Type": "application/json" }
Body: { "messages": [ ...conversationHistory ] }
Always use FORGE_AI_KEY_PLACEHOLDER — injected at runtime.

Return ONLY the JSON object. No prose. No markdown fences. No explanations outside JSON.
"""

CHAT_SYSTEM_PROMPT = """You are Forge AI, a friendly expert assistant built into a web app builder called Forge.
Your personality: sharp, concise, genuinely helpful — like a senior engineer on your team.

STRICT RULES:
- You are NOT a search engine. NEVER perform web searches or return search results.
- ONLY answer questions about: web development, app ideas, how to use Forge, coding help, UI/UX, design patterns.
- If someone asks something off-topic, politely redirect: explain you are a coding assistant and ask what they'd like to build.
- Keep answers short and practical.
- If the user wants to clone a website, tell them to paste the URL into the URL field in the Build tab and hit ⚡ Build.
- If the user describes an app idea, ask ONE clarifying question then encourage them to hit Build."""


# ─────────────────────────────────────────────
# AGENT RUNNER
# ─────────────────────────────────────────────
def run_agent(
    ai: AIClient,
    vfs: VirtualFS,
    task: str,
    existing_files: dict | None = None,
    scrape_context: str | None = None,
) -> dict:
    context_parts = []

    if scrape_context:
        context_parts.append(
            f"SCRAPED PAGE CONTEXT (study this carefully to clone and upgrade):\n\n{scrape_context}"
        )

    if existing_files:
        snippets = [
            f"### {path}\n```\n{content}\n```"
            for path, content in existing_files.items()
        ]
        context_parts.append(
            "CURRENT PROJECT FILES (complete — preserve everything not mentioned):\n\n"
            + "\n\n".join(snippets)
        )

    context_block = ("\n\n" + "\n\n".join(context_parts)) if context_parts else ""

    if existing_files:
        user_msg = (
            f"TASK: {task}{context_block}\n\n"
            "Return ONLY the files that need to change. "
            "For each changed file, include its FULL new content."
        )
    else:
        user_msg = (
            f"TASK: {task}{context_block}\n\n"
            "Build this from scratch. Return the complete project JSON."
        )

    raw = ai.ask(AGENT_SYSTEM_PROMPT, user_msg, max_tokens=8192)

    # Strip accidental markdown fences
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    if raw.endswith("```"):
        raw = raw[:-3].strip()

    parsed = json.loads(raw)

    for action in parsed.get("actions", []):
        t = action.get("type")
        path = action.get("path", "").lstrip("/")
        content = action.get("content", "")
        if isinstance(content, str):
            content = content.replace("FORGE_AI_KEY_PLACEHOLDER", AI_KEY)
        if t in ("create", "edit"):
            vfs.write(path, content)
        elif t == "delete":
            vfs.delete(path)

    return parsed


# ─────────────────────────────────────────────
# PUBLISH  (multi-strategy)
# ─────────────────────────────────────────────
def publish_app(vfs: VirtualFS) -> dict:
    zip_bytes = vfs.to_zip_bytes()

    # Strategy 1: Netlify raw zip POST
    try:
        resp = requests.post(
            "https://api.netlify.com/api/v1/sites",
            headers={"Content-Type": "application/zip"},
            data=zip_bytes, timeout=60,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            sub = data.get("subdomain") or data.get("id", "app")
            return {"url": f"https://{sub}.netlify.app", "method": "Netlify"}
    except Exception:
        pass

    # Strategy 2: Netlify multipart
    try:
        resp = requests.post(
            "https://api.netlify.com/api/v1/sites",
            files={"zip": ("project.zip", zip_bytes, "application/zip")},
            timeout=60,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            sub = data.get("subdomain") or data.get("id", "app")
            return {"url": f"https://{sub}.netlify.app", "method": "Netlify"}
    except Exception:
        pass

    # Strategy 3: data: URI (always works)
    single_html = vfs.to_single_html()
    b64 = base64.b64encode(single_html.encode("utf-8")).decode("ascii")
    data_uri = f"data:text/html;base64,{b64}"
    return {"url": data_uri, "method": "data-uri", "html": single_html}


# ─────────────────────────────────────────────
# STYLES
# ─────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Syne:wght@400;700;800&display=swap');

html, body, [data-testid="stAppViewContainer"] {
    background: #0a0a0f !important;
    color: #e8e6e3 !important;
}
[data-testid="stSidebar"] {
    background: #0d0d14 !important;
    border-right: 1px solid #1e1e2e !important;
}
h1, h2, h3 { font-family: 'Syne', sans-serif !important; letter-spacing: -0.02em; }
code, pre, .stCode { font-family: 'IBM Plex Mono', monospace !important; font-size: 12px !important; }

.stTextInput input, .stTextArea textarea, .stSelectbox select {
    background: #12121c !important;
    border: 1px solid #2a2a3e !important;
    color: #e8e6e3 !important;
    border-radius: 6px !important;
    font-family: 'IBM Plex Mono', monospace !important;
}
.stTextInput input:focus, .stTextArea textarea:focus {
    border-color: #7c6af7 !important;
    box-shadow: 0 0 0 2px rgba(124,106,247,0.2) !important;
}
.stButton > button[kind="primary"], .stButton > button:first-child {
    background: linear-gradient(135deg, #7c6af7, #a78bfa) !important;
    color: #fff !important; border: none !important;
    border-radius: 6px !important; font-family: 'Syne', sans-serif !important;
    font-weight: 700 !important; letter-spacing: 0.04em !important;
    padding: 0.5rem 1.5rem !important; transition: all 0.2s ease !important;
}
.stButton > button:hover {
    transform: translateY(-1px) !important;
    box-shadow: 0 4px 20px rgba(124,106,247,0.4) !important;
}
.stButton > button[kind="secondary"] {
    background: transparent !important; border: 1px solid #2a2a3e !important;
    color: #a0a0c0 !important; border-radius: 6px !important;
    font-family: 'IBM Plex Mono', monospace !important;
}
.streamlit-expanderHeader {
    background: #12121c !important; border: 1px solid #1e1e2e !important;
    border-radius: 6px !important; font-family: 'IBM Plex Mono', monospace !important;
    color: #a0a0c0 !important;
}
.stAlert { border-radius: 6px !important; font-family: 'IBM Plex Mono', monospace !important; font-size: 13px !important; }
.stTabs [role="tablist"] { gap: 4px; border-bottom: 1px solid #1e1e2e; }
.stTabs [role="tab"] {
    background: transparent !important; color: #606080 !important;
    border: none !important; font-family: 'IBM Plex Mono', monospace !important;
    font-size: 12px !important; padding: 6px 14px !important;
}
.stTabs [role="tab"][aria-selected="true"] { color: #a78bfa !important; border-bottom: 2px solid #7c6af7 !important; }
.build-log {
    background: #0d0d14; border: 1px solid #1e1e2e; border-radius: 8px;
    padding: 12px 16px; font-family: 'IBM Plex Mono', monospace; font-size: 12px;
    color: #7c6af7; max-height: 200px; overflow-y: auto;
}
.scrape-badge {
    display: inline-block; background: #1a1a2e; border: 1px solid #7c6af7;
    border-radius: 4px; padding: 3px 8px; font-family: 'IBM Plex Mono', monospace;
    font-size: 11px; color: #a78bfa; margin-bottom: 6px;
}
.gh-card {
    background: #12121c; border: 1px solid #2a2a3e; border-radius: 10px;
    padding: 16px 20px; margin: 8px 0;
    font-family: 'IBM Plex Mono', monospace; font-size: 12px;
}
.gh-badge {
    display: inline-block; background: #1a2a1a; border: 1px solid #2ea043;
    border-radius: 4px; padding: 3px 8px; font-family: 'IBM Plex Mono', monospace;
    font-size: 11px; color: #56d364; margin: 2px 3px;
}
.gh-badge-purple {
    display: inline-block; background: #1a1a2e; border: 1px solid #7c6af7;
    border-radius: 4px; padding: 3px 8px; font-family: 'IBM Plex Mono', monospace;
    font-size: 11px; color: #a78bfa; margin: 2px 3px;
}
hr { border-color: #1e1e2e !important; }
.stCaption { color: #606080 !important; }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────
def active_proj() -> dict:
    ap = st.session_state["active_project"]
    return st.session_state["projects"][ap]


def _make_project(name: str = "untitled") -> tuple[str, dict]:
    pid = str(uuid.uuid4())[:8]
    return pid, {
        "name": name,
        "chat_history": [],
        "build_log": [],
        "history": [],
        "last_preview_html": None,
        "vfs_files": {},
        "vfs_root": tempfile.mkdtemp(prefix=f"forge_{pid}_"),
        "published_url": None,
        "published_method": None,
        "scrape_cache": {},
        # GitHub state
        "github_repo": "",
        "github_branch": "main",
        "github_pages_url": None,
        "github_last_push": None,
    }


def init_state():
    if "projects" not in st.session_state:
        pid, pdata = _make_project()
        st.session_state["projects"] = {pid: pdata}
        st.session_state["active_project"] = pid

    if "active_project" not in st.session_state:
        st.session_state["active_project"] = list(st.session_state["projects"].keys())[0]

    for pid, pdata in st.session_state["projects"].items():
        if "vfs_root" not in pdata or not Path(pdata["vfs_root"]).exists():
            pdata["vfs_root"] = tempfile.mkdtemp(prefix=f"forge_{pid}_")
        pdata.setdefault("scrape_cache", {})
        pdata.setdefault("published_method", None)
        pdata.setdefault("github_repo", "")
        pdata.setdefault("github_branch", "main")
        pdata.setdefault("github_pages_url", None)
        pdata.setdefault("github_last_push", None)

    st.session_state.setdefault("selected_file", None)
    st.session_state.setdefault("last_scrape_url", "")
    st.session_state.setdefault("github_token", "")
    st.session_state.setdefault("gh_log", [])


def new_project():
    pid, pdata = _make_project()
    st.session_state["projects"][pid] = pdata
    st.session_state["active_project"] = pid
    st.session_state["selected_file"] = None
    st.session_state["last_scrape_url"] = ""
    st.session_state["gh_log"] = []


def switch_project(pid: str):
    st.session_state["active_project"] = pid
    st.session_state["selected_file"] = None
    st.session_state["last_scrape_url"] = ""
    st.session_state["gh_log"] = []


init_state()
vfs = VirtualFS()


# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚡ Forge AI")
    st.caption("Build · Clone · Upgrade · Deploy")

    if st.button("✦ New Chat", type="primary", use_container_width=True):
        new_project()
        st.rerun()

    st.divider()
    st.markdown("**Chats**")
    ap = st.session_state["active_project"]
    for pid, pdata in list(st.session_state["projects"].items()):
        is_active = pid == ap
        label = pdata["name"]
        pub = pdata.get("published_url")
        pub_method = pdata.get("published_method", "")
        display = f"{'▶ ' if is_active else ''}{label}"
        col_p, col_del = st.columns([5, 1])
        with col_p:
            if st.button(display, key=f"proj_{pid}", use_container_width=True,
                         type="primary" if is_active else "secondary"):
                if not is_active:
                    switch_project(pid)
                    st.rerun()
        with col_del:
            if len(st.session_state["projects"]) > 1:
                if st.button("✕", key=f"projdel_{pid}"):
                    del st.session_state["projects"][pid]
                    remaining = list(st.session_state["projects"].keys())
                    st.session_state["active_project"] = remaining[0]
                    st.rerun()
        if pub and pub_method != "data-uri":
            st.markdown(
                f'<a href="{pub}" target="_blank" style="font-family:IBM Plex Mono,monospace;'
                f'font-size:10px;color:#7c6af7;text-decoration:none;">🌐 {pub.replace("https://","")}</a>',
                unsafe_allow_html=True,
            )
        gh_pages = pdata.get("github_pages_url")
        if gh_pages:
            st.markdown(
                f'<a href="{gh_pages}" target="_blank" style="font-family:IBM Plex Mono,monospace;'
                f'font-size:10px;color:#56d364;text-decoration:none;">🐙 {gh_pages.replace("https://","")}</a>',
                unsafe_allow_html=True,
            )

    st.divider()

    proj_name = st.text_input(
        "Project name",
        value=st.session_state["projects"][ap]["name"],
        label_visibility="collapsed",
        placeholder="project-name",
    )
    if proj_name != st.session_state["projects"][ap]["name"]:
        st.session_state["projects"][ap]["name"] = proj_name
        st.rerun()

    st.markdown("**Files**")
    file_list = vfs.list_files()
    if not file_list:
        st.caption("No files yet — build something!")
    else:
        for f in file_list:
            col1, col2 = st.columns([5, 1])
            with col1:
                if st.button(f"📄 {f}", key=f"file_{f}", use_container_width=True):
                    st.session_state["selected_file"] = f
            with col2:
                if st.button("✕", key=f"del_{f}"):
                    vfs.delete(f)
                    if st.session_state["selected_file"] == f:
                        st.session_state["selected_file"] = None
                    st.rerun()

    st.divider()

    if st.button("🗑 Clear project", use_container_width=True):
        vfs.clear()
        for k in ("history", "chat_history", "build_log"):
            active_proj()[k] = []
        active_proj()["last_preview_html"] = None
        active_proj()["published_url"] = None
        active_proj()["published_method"] = None
        active_proj()["scrape_cache"] = {}
        active_proj()["github_pages_url"] = None
        active_proj()["github_last_push"] = None
        st.session_state["selected_file"] = None
        st.session_state["last_scrape_url"] = ""
        st.session_state["gh_log"] = []
        st.rerun()


# ─────────────────────────────────────────────
# MAIN AREA
# ─────────────────────────────────────────────
main_tabs = st.tabs(["🏗 Build", "👁 Preview", "📂 Editor", "🐙 GitHub", "📜 History"])


# ──────────── TAB 1 : BUILD + CHAT ─────────────────
with main_tabs[0]:

    with st.container():
        if not active_proj()["chat_history"]:
            st.markdown(
                '''<div style="text-align:center;padding:40px 0 20px;
                    font-family:IBM Plex Mono,monospace;color:#606080;font-size:13px;">
                    ⚡ Describe what to build, paste a URL to clone, or ask anything about web dev.
                </div>''',
                unsafe_allow_html=True,
            )
        for msg in active_proj()["chat_history"]:
            role = msg["role"]
            text = msg["content"]
            is_user = role == "user"
            bubble_bg    = "#2a2a3e" if is_user else "#12121c"
            bubble_align = "flex-end" if is_user else "flex-start"
            icon = "🧑" if is_user else "⚡"
            st.markdown(
                f'''<div style="display:flex;justify-content:{bubble_align};margin:6px 0;">
                  <div style="background:{bubble_bg};border-radius:10px;padding:10px 14px;
                       max-width:80%;font-family:IBM Plex Mono,monospace;font-size:13px;
                       color:#e8e6e3;line-height:1.5;">
                  {icon}&nbsp; {text}
                  </div></div>''',
                unsafe_allow_html=True,
            )

    st.divider()

    url_col, clear_col = st.columns([9, 1])
    with url_col:
        scrape_url_val = st.text_input(
            "🔗 URL to clone / draw inspiration from (optional)",
            value=st.session_state.get("last_scrape_url", ""),
            placeholder="https://example.com — Forge will scrape and clone/upgrade it",
            key="scrape_url_field",
        )
    with clear_col:
        st.markdown("<div style='margin-top:28px'>", unsafe_allow_html=True)
        if st.button("✕", key="clear_url_btn", help="Clear URL"):
            st.session_state["last_scrape_url"] = ""
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    if scrape_url_val.strip():
        st.markdown(
            f'<span class="scrape-badge">🔍 Will scrape: {scrape_url_val.strip()[:70]}</span>',
            unsafe_allow_html=True,
        )

    col1, col2, col3 = st.columns([6, 2, 2])
    with col1:
        prompt = st.text_area(
            "Message", height=80,
            placeholder="Clone this site with dark mode… or Build a kanban board with drag-and-drop…",
            key="prompt_input", label_visibility="collapsed",
        )
    with col2:
        build_clicked = st.button("⚡ Build", type="primary", use_container_width=True)
    with col3:
        mode = st.radio(
            "Mode", ["✨ New", "✏️ Edit"], horizontal=False,
            index=0 if not vfs.list_files() else 1,
        )

    BUILD_VERBS = [
        "build", "create", "make", "add", "generate", "write", "update",
        "change", "fix", "edit", "remove", "delete", "refactor", "style",
        "implement", "deploy", "convert", "migrate", "improve", "redesign",
        "dark mode", "light mode", "responsive", "mobile", "feature",
        "clone", "copy", "duplicate", "scrape", "port", "replicate", "upgrade",
    ]

    def is_build_intent(text: str) -> bool:
        t = text.lower()
        return any(v in t for v in BUILD_VERBS)

    if build_clicked:
        if not AI_KEY:
            st.error("⚠️  Set the `COMPLEX_AI_KEY` environment variable to use Forge AI.")
        elif not prompt.strip() and not scrape_url_val.strip():
            st.warning("Please enter a message or a URL to clone.")
        else:
            ai = AIClient(AI_KEY)
            user_text = prompt.strip() or f"Clone and upgrade this page: {scrape_url_val.strip()}"

            active_proj()["chat_history"].append({"role": "user", "content": user_text})

            url_provided = bool(scrape_url_val.strip().startswith("http"))

            if is_build_intent(user_text) or url_provided or "✨ New" in mode:

                scrape_context = None
                if url_provided:
                    cache_key = scrape_url_val.strip()
                    cached = active_proj()["scrape_cache"].get(cache_key)
                    if cached:
                        scrape_result = cached
                        scrape_info = "📋 Using cached scrape"
                    else:
                        with st.spinner(f"🔍 Scraping {scrape_url_val.strip()[:50]}…"):
                            scrape_result = scrape_url(scrape_url_val.strip())
                            active_proj()["scrape_cache"][cache_key] = scrape_result
                        scrape_info = (
                            f"⚠️ Scrape partial ({scrape_result['error']})"
                            if scrape_result.get("error")
                            else f"✅ Scraped: {scrape_result['title'] or scrape_url_val.strip()}"
                        )
                    scrape_context = build_scrape_context(scrape_result)
                    active_proj()["build_log"].append(scrape_info)
                    st.session_state["last_scrape_url"] = scrape_url_val.strip()

                existing = (
                    dict(vfs.files)
                    if "✏️ Edit" in mode and vfs.list_files()
                    else None
                )

                with st.spinner("⚡ Forge is building…"):
                    try:
                        result = run_agent(
                            ai, vfs, user_text,
                            existing_files=existing,
                            scrape_context=scrape_context,
                        )
                    except json.JSONDecodeError as e:
                        err = f"AI returned invalid JSON — try rephrasing. ({e})"
                        active_proj()["chat_history"].append({"role": "assistant", "content": err})
                        st.rerun()
                    except Exception as e:
                        err = f"Error: {e}"
                        active_proj()["chat_history"].append({"role": "assistant", "content": err})
                        st.rerun()

                summary = result.get("summary", "Done.")
                actions = result.get("actions", [])
                tech_stack = result.get("tech_stack", [])

                log_lines = [f"✅ {summary}", ""]
                for a in actions:
                    icon = {"create": "➕", "edit": "✏️", "delete": "🗑"}.get(a.get("type"), "•")
                    log_lines.append(f"{icon}  {a.get('path','?')}")
                if tech_stack:
                    log_lines.append(f"\n🔧 Stack: {', '.join(tech_stack)}")
                active_proj()["build_log"].extend(log_lines)
                active_proj()["history"].append({"role": "user",  "text": user_text})
                active_proj()["history"].append({"role": "agent", "text": summary})

                html = vfs.get_entry_html()
                if html:
                    active_proj()["last_preview_html"] = vfs.inject_css_js(html)

                # Auto-push to GitHub if configured
                gh_token = st.session_state.get("github_token", "")
                gh_repo  = active_proj().get("github_repo", "")
                if gh_token and gh_repo and vfs.list_files():
                    try:
                        gh = GitHubClient(gh_token)
                        owner = gh.whoami()
                        repo_name = gh_repo.split("/")[-1] if "/" in gh_repo else gh_repo
                        branch = active_proj().get("github_branch", "main")

                        push_result = gh.push_files(
                            owner, repo_name, dict(vfs.files),
                            branch=branch,
                            commit_msg=f"⚡ {summary}",
                            enable_pages=True,
                        )

                        if push_result.get("pages_url"):
                            active_proj()["github_pages_url"] = push_result["pages_url"]

                        active_proj()["github_last_push"] = push_result
                        active_proj()["build_log"].append(
                            f"🐙 Auto-synced to GitHub ({push_result['changed']} files changed)"
                        )
                    except Exception as e:
                        active_proj()["build_log"].append(f"⚠️ GitHub sync skipped: {e}")

                reply = f"✅ {summary}"
                if vfs.list_files():
                    reply += " — switch to **Preview** to see it!"
                if active_proj().get("github_pages_url"):
                    reply += f" Also live on [GitHub Pages]({active_proj()['github_pages_url']})."

                active_proj()["chat_history"].append({"role": "assistant", "content": reply})

            else:
                conversation = [
                    {"role": m["role"], "content": m["content"]}
                    for m in active_proj()["chat_history"]
                ]
                with st.spinner("Thinking…"):
                    try:
                        reply = ai.chat(conversation)
                    except Exception as e:
                        reply = f"Sorry, I hit an error: {e}"
                active_proj()["chat_history"].append({"role": "assistant", "content": reply})

            st.rerun()

    if active_proj()["build_log"]:
        with st.expander("Build Log", expanded=False):
            st.markdown(
                '<div class="build-log">'
                + "<br>".join(f"› {l}" for l in active_proj()["build_log"][-80:])
                + "</div>",
                unsafe_allow_html=True,
            )


# ──────────── TAB 2 : PREVIEW ───────────────
with main_tabs[1]:
    preview_html = active_proj().get("last_preview_html")

    if not preview_html:
        st.info("Build something first and the live preview will appear here.")
    else:
        proj       = active_proj()
        pub_url    = proj.get("published_url")
        pub_method = proj.get("published_method", "")
        pages_url  = proj.get("github_pages_url")

        col_pub, col_dl, col_zip, col_info = st.columns([2, 2, 2, 4])

        with col_pub:
            if st.button("🌐 Publish", type="primary", use_container_width=True,
                         help="Deploy via Netlify or fallback to data: URI"):
                if not vfs.list_files():
                    st.error("Nothing to publish.")
                else:
                    with st.spinner("Publishing…"):
                        try:
                            result_pub = publish_app(vfs)
                            proj["published_url"]    = result_pub["url"]
                            proj["published_method"] = result_pub["method"]
                            if result_pub["method"] == "data-uri":
                                st.warning(
                                    "Netlify unavailable — app packaged as a portable link. "
                                    "Use **Open App** or **Download ZIP** to self-host."
                                )
                            else:
                                st.success(f"Live at [{result_pub['url']}]({result_pub['url']})")
                                st.balloons()
                        except Exception as e:
                            st.error(f"Publish failed: {e}")

        with col_dl:
            st.download_button(
                "⬇️ HTML", data=preview_html, file_name="index.html",
                mime="text/html", use_container_width=True,
            )

        with col_zip:
            if vfs.list_files():
                st.download_button(
                    "📦 ZIP", data=vfs.to_zip_bytes(),
                    file_name="forge-project.zip", mime="application/zip",
                    use_container_width=True,
                )

        with col_info:
            links = []
            if pub_url:
                if pub_method == "data-uri":
                    links.append(
                        f'<a href="{pub_url}" target="_blank" '
                        f'style="display:inline-block;background:linear-gradient(135deg,#7c6af7,#a78bfa);'
                        f'color:#fff;text-decoration:none;border-radius:6px;padding:6px 14px;'
                        f'font-family:Syne,sans-serif;font-weight:700;font-size:13px;">🚀 Open App</a>'
                    )
                else:
                    links.append(
                        f'🌐 <a href="{pub_url}" target="_blank" style="color:#a78bfa;">'
                        f'{pub_url.replace("https://","")}</a>'
                    )
            if pages_url:
                links.append(
                    f'🐙 <a href="{pages_url}" target="_blank" style="color:#56d364;">'
                    f'GitHub Pages</a>'
                )
            if links:
                st.markdown(" &nbsp; ".join(links), unsafe_allow_html=True)

        st.divider()
        st.components.v1.html(preview_html, height=700, scrolling=True)


# ──────────── TAB 3 : EDITOR ────────────────
with main_tabs[2]:
    files = vfs.list_files()
    if not files:
        st.info("No files yet.")
    else:
        selected = st.selectbox(
            "File", files,
            index=(
                files.index(st.session_state["selected_file"])
                if st.session_state["selected_file"] in files else 0
            ),
            key="editor_file_select",
        )
        st.session_state["selected_file"] = selected

        content = vfs.read(selected) or ""
        edited = st.text_area(
            f"Editing: `{selected}`", value=content,
            height=500, key=f"editor_{selected}",
        )

        col_save, col_revert = st.columns(2)
        with col_save:
            if st.button("💾 Save", use_container_width=True):
                vfs.write(selected, edited)
                html = vfs.get_entry_html()
                if html:
                    active_proj()["last_preview_html"] = vfs.inject_css_js(html)
                st.success("Saved!")
                st.rerun()
        with col_revert:
            if st.button("↩ Revert", use_container_width=True):
                st.rerun()

        st.divider()
        st.markdown("**Create new file**")
        new_path = st.text_input("Path (e.g. utils.js)", key="new_file_path")
        if st.button("➕ Create empty file") and new_path.strip():
            vfs.write(new_path.strip(), "")
            st.session_state["selected_file"] = new_path.strip()
            st.rerun()


# ──────────── TAB 4 : GITHUB ────────────────
with main_tabs[3]:
    st.markdown("### 🐙 GitHub Sync")
    st.caption("Push your project to GitHub and host it for free with GitHub Pages.")

    # ── Token input ──────────────────────────
    gh_token = st.text_input(
        "GitHub Personal Access Token",
        type="password",
        value=st.session_state.get("github_token", ""),
        placeholder="ghp_xxxxxxxxxxxxxxxxxxxx",
        help="Create at github.com → Settings → Developer settings → Personal access tokens → Fine-grained. Needs: repo (read/write), pages (write).",
        key="gh_token_input",
    )
    if gh_token != st.session_state.get("github_token", ""):
        st.session_state["github_token"] = gh_token

    # Show logged-in user
    if gh_token:
        try:
            _gh = GitHubClient(gh_token)
            gh_user = _gh.whoami()
            st.markdown(
                f'<span class="gh-badge">✓ Authenticated as @{gh_user}</span>',
                unsafe_allow_html=True,
            )
        except Exception:
            st.markdown(
                '<span style="color:#f87171;font-family:IBM Plex Mono,monospace;font-size:12px;">'
                '✗ Invalid token or no network access</span>',
                unsafe_allow_html=True,
            )
            gh_user = None
    else:
        gh_user = None

    st.divider()

    # ── Repo config ──────────────────────────
    col_r, col_b = st.columns([3, 2])
    with col_r:
        gh_repo_input = st.text_input(
            "Repository name",
            value=active_proj().get("github_repo", ""),
            placeholder="my-forge-app",
            help="Just the repo name — we'll create it under your account if it doesn't exist.",
        )
        if gh_repo_input != active_proj().get("github_repo", ""):
            active_proj()["github_repo"] = gh_repo_input

    with col_b:
        gh_branch_input = st.text_input(
            "Branch",
            value=active_proj().get("github_branch", "main"),
            placeholder="main",
        )
        if gh_branch_input != active_proj().get("github_branch", "main"):
            active_proj()["github_branch"] = gh_branch_input

    col_priv, col_pages = st.columns(2)
    with col_priv:
        gh_private = st.checkbox("Private repository", value=False)
    with col_pages:
        gh_enable_pages = st.checkbox("Enable GitHub Pages", value=True,
                                      help="Auto-deploys your app to a free github.io URL")

    col_pr, col_commit = st.columns(2)
    with col_pr:
        gh_create_pr = st.checkbox(
            "Open Pull Request",
            value=False,
            help="Push to a feature branch and open a PR to main instead of pushing directly",
        )
    with col_commit:
        gh_commit_msg = st.text_input(
            "Commit message",
            value="⚡ Forge AI — update",
            placeholder="⚡ Forge AI — update",
        )

    st.divider()

    # ── Push button ──────────────────────────
    push_col, status_col = st.columns([2, 5])
    with push_col:
        push_clicked = st.button(
            "🚀 Push to GitHub", type="primary",
            use_container_width=True,
            disabled=not (gh_token and gh_repo_input and vfs.list_files()),
        )

    if not gh_token:
        st.caption("↑ Enter your GitHub token to enable push.")
    elif not gh_repo_input:
        st.caption("↑ Enter a repository name.")
    elif not vfs.list_files():
        st.caption("↑ Build something first.")

    if push_clicked and gh_token and gh_repo_input and vfs.list_files():
        gh_log_lines: list[str] = []

        def gh_log(msg: str):
            gh_log_lines.append(msg)

        progress_placeholder = st.empty()

        with st.spinner("Syncing with GitHub…"):
            try:
                gh = GitHubClient(gh_token)
                owner = gh.whoami()
                repo_name = gh_repo_input.strip().split("/")[-1]

                # Determine branches for PR workflow
                target_branch = gh_branch_input.strip() or "main"
                pr_target = None
                if gh_create_pr and target_branch == "main":
                    target_branch = "forge-ai"
                    pr_target = "main"
                elif gh_create_pr:
                    pr_target = gh_branch_input.strip()

                push_result = gh.push_files(
                    owner=owner,
                    repo=repo_name,
                    files=dict(vfs.files),
                    branch=target_branch,
                    commit_msg=gh_commit_msg or "⚡ Forge AI — update",
                    create_pr_to=pr_target,
                    enable_pages=gh_enable_pages,
                    log_fn=gh_log,
                )

                active_proj()["github_repo"] = repo_name
                active_proj()["github_branch"] = target_branch
                active_proj()["github_last_push"] = push_result
                if push_result.get("pages_url"):
                    active_proj()["github_pages_url"] = push_result["pages_url"]

                st.session_state["gh_log"] = gh_log_lines

            except Exception as e:
                st.error(f"Push failed: {e}")
                gh_log_lines.append(f"✗ Error: {e}")
                st.session_state["gh_log"] = gh_log_lines

        st.rerun()

    # ── Push results ─────────────────────────
    last_push = active_proj().get("github_last_push")
    if last_push:
        st.markdown('<div class="gh-card">', unsafe_allow_html=True)
        st.markdown("**Last sync result**")

        repo_url = last_push.get("repo_url", "")
        branch   = last_push.get("branch", "")
        pages    = last_push.get("pages_url", "")
        pr_url   = last_push.get("pr_url", "")
        changed  = last_push.get("changed", 0)
        skipped  = last_push.get("skipped", 0)

        badges = [
            f'<span class="gh-badge">✓ {changed} file{"s" if changed != 1 else ""} pushed</span>',
            f'<span class="gh-badge-purple">⊘ {skipped} unchanged</span>',
        ]
        st.markdown(" ".join(badges), unsafe_allow_html=True)

        links = []
        if repo_url:
            links.append(f'<a href="{repo_url}/tree/{branch}" target="_blank" '
                         f'style="color:#a78bfa;">📁 {repo_url.replace("https://github.com/","")}/{branch}</a>')
        if pr_url:
            links.append(f'<a href="{pr_url}" target="_blank" style="color:#f9a8d4;">📬 View Pull Request</a>')
        if pages:
            links.append(f'<a href="{pages}" target="_blank" style="color:#56d364;">🌐 GitHub Pages: {pages}</a>')

        for link in links:
            st.markdown(link, unsafe_allow_html=True)

        st.markdown("</div>", unsafe_allow_html=True)

    # ── Push log ─────────────────────────────
    gh_log_display = st.session_state.get("gh_log", [])
    if gh_log_display:
        with st.expander("Push log", expanded=True):
            st.markdown(
                '<div class="build-log">'
                + "<br>".join(f"› {l}" for l in gh_log_display)
                + "</div>",
                unsafe_allow_html=True,
            )

    st.divider()

    # ── Help box ─────────────────────────────
    with st.expander("📖 How to set up a GitHub token"):
        st.markdown("""
**Create a Fine-grained Personal Access Token:**

1. Go to **github.com → Settings → Developer settings → Personal access tokens → Fine-grained tokens**
2. Click **Generate new token**
3. Set an expiration (90 days recommended)
4. Under **Repository permissions**, grant:
   - **Contents**: Read and Write
   - **Pages**: Read and Write
   - **Pull requests**: Read and Write (if using PR workflow)
5. Copy the token and paste it above

The token is stored only in your browser session and never sent anywhere except the GitHub API.
        """)


# ──────────── TAB 5 : HISTORY ───────────────
with main_tabs[4]:
    history = active_proj()["history"]
    if not history:
        st.info("No conversation history yet.")
    else:
        for entry in reversed(history):
            role  = entry["role"]
            text  = entry["text"]
            icon  = "🧑" if role == "user" else "⚡"
            color = "#2a2a3e" if role == "user" else "#1a1a2e"
            align = "right" if role == "user" else "left"
            st.markdown(
                f"""<div style="background:{color};border-radius:8px;padding:10px 14px;
                    margin:6px 0;font-family:'IBM Plex Mono',monospace;font-size:13px;
                    color:#e8e6e3;text-align:{align};">
                {icon}&nbsp;&nbsp;{text}</div>""",
                unsafe_allow_html=True,
            )
