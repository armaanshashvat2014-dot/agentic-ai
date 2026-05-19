import os
import re
import json
import tempfile
import uuid
import zipfile
import io
import base64
import hashlib
import time
from pathlib import Path
from urllib.parse import urljoin

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

AI_ENDPOINT  = "https://raujzsawwpmixwlcgcgs.supabase.co/functions/v1/public-ai-api"
AI_KEY       = os.getenv("COMPLEX_AI_KEY", "")
PERSIST_FILE = Path(tempfile.gettempdir()) / "forge_projects.json"


# ─────────────────────────────────────────────
# PERSISTENCE  (JSON file — survives reruns, lost on container restart)
# ─────────────────────────────────────────────
def _load_persisted() -> dict:
    try:
        if PERSIST_FILE.exists():
            return json.loads(PERSIST_FILE.read_text())
    except Exception:
        pass
    return {}


def persist_projects():
    """Save all projects to disk (excludes non-serialisable vfs_root path)."""
    try:
        safe = {}
        for pid, p in st.session_state["projects"].items():
            safe[pid] = {k: v for k, v in p.items() if k != "vfs_root"}
        PERSIST_FILE.write_text(json.dumps(safe, ensure_ascii=False))
    except Exception:
        pass


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
            r'<meta\s+(?:name|property)=["\']([^"\']+)["\'][^>]*content=["\']([^"\']*)["\']', raw, re.I
        ):
            result["meta"][m.group(1)] = m.group(2)

        styles = re.findall(r"<style[^>]*>(.*?)</style>", raw, re.I | re.S)
        result["inline_styles"] = "\n\n".join(styles)[:30_000]

        scripts = re.findall(r"<script(?![^>]*\bsrc\b)[^>]*>(.*?)</script>", raw, re.I | re.S)
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


def fetch_asset(url: str, max_bytes: int = 40_000) -> str:
    try:
        r = requests.get(url, headers=SCRAPE_HEADERS, timeout=10)
        r.raise_for_status()
        return r.text[:max_bytes]
    except Exception:
        return ""


def build_scrape_context(s: dict) -> str:
    if s.get("error"):
        return f"[Scrape error: {s['error']}]"
    parts = [
        "=== SCRAPED PAGE ===",
        f"URL: {s['url']}",
        f"Title: {s['title']}",
        f"\n--- Visible Text ---\n{s['text_content'][:5000]}",
    ]
    if s["inline_styles"]:
        parts.append(f"\n--- Inline CSS ---\n{s['inline_styles'][:10000]}")
    if s["inline_scripts"]:
        parts.append(f"\n--- Inline JS ---\n{s['inline_scripts'][:10000]}")
    if s["linked_css_urls"]:
        parts.append("\n--- Linked CSS ---")
        for u in s["linked_css_urls"][:3]:
            c = fetch_asset(u)
            if c:
                parts.append(f"/* {u} */\n{c[:8000]}")
    parts.append(f"\n--- Full HTML (first 40 KB) ---\n{s['html_raw'][:40_000]}")
    return "\n".join(parts)


# ─────────────────────────────────────────────
# VIRTUAL FILESYSTEM
# ─────────────────────────────────────────────
class VirtualFS:
    @property
    def _proj(self) -> dict:
        return st.session_state["projects"][st.session_state["active_project"]]

    @property
    def files(self) -> dict:
        return self._proj["vfs_files"]

    @property
    def root(self) -> str:
        return self._proj["vfs_root"]

    def write(self, path: str, content: str):
        self._proj["vfs_files"][path] = content
        disk = Path(self.root) / path
        disk.parent.mkdir(parents=True, exist_ok=True)
        disk.write_text(content, encoding="utf-8")

    def read(self, path: str) -> str | None:
        return self._proj["vfs_files"].get(path)

    def delete(self, path: str):
        self._proj["vfs_files"].pop(path, None)
        d = Path(self.root) / path
        if d.exists():
            d.unlink()

    def list_files(self) -> list[str]:
        return sorted(self._proj["vfs_files"].keys())

    def clear(self):
        self._proj["vfs_files"] = {}
        for f in Path(self.root).rglob("*"):
            if f.is_file():
                f.unlink()

    def get_entry_html(self) -> str | None:
        for c in ("index.html", "app.html", "main.html"):
            if c in self.files:
                return self.files[c]
        for p, c in self.files.items():
            if p.endswith(".html"):
                return c
        return None

    def inject_css_js(self, html: str) -> str:
        def sub_css(m):
            href = m.group(1)
            css = self.files.get(href) or self.files.get(href.lstrip("./"))
            return f"<style>{css}</style>" if css else m.group(0)

        def sub_js(m):
            src = m.group(1)
            js = self.files.get(src) or self.files.get(src.lstrip("./"))
            return f"<script>{js}</script>" if js else m.group(0)

        html = re.sub(r'<link[^>]*href=["\']([^"\']+\.css)["\'][^>]*/?>',  sub_css, html)
        html = re.sub(r'<script[^>]+src=["\']([^"\']+\.js)["\'][^>]*></script>', sub_js, html)
        return html

    def to_zip(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for p, c in self.files.items():
                zf.writestr(p, c)
        return buf.getvalue()

    def to_single_html(self) -> str:
        html = self.get_entry_html() or "<html><body><p>No HTML file found.</p></body></html>"
        return self.inject_css_js(html)


# ─────────────────────────────────────────────
# GITHUB CLIENT
# ─────────────────────────────────────────────
class GitHubClient:
    BASE = "https://api.github.com"

    def __init__(self, token: str):
        self.h = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }

    def _get(self, p):
        r = requests.get(f"{self.BASE}{p}", headers=self.h, timeout=20)
        r.raise_for_status(); return r.json()

    def _post(self, p, b):
        r = requests.post(f"{self.BASE}{p}", headers=self.h, json=b, timeout=30)
        r.raise_for_status(); return r.json()

    def _put(self, p, b):
        r = requests.put(f"{self.BASE}{p}", headers=self.h, json=b, timeout=30)
        r.raise_for_status(); return r.json()

    def whoami(self) -> str:
        return self._get("/user")["login"]

    def repo_exists(self, owner, repo) -> bool:
        try:
            self._get(f"/repos/{owner}/{repo}"); return True
        except Exception:
            return False

    def create_repo(self, name, private=False):
        return self._post("/user/repos", {
            "name": name, "private": private, "auto_init": False,
            "description": "Built with Forge AI ⚡",
        })

    def default_branch(self, owner, repo) -> str:
        return self._get(f"/repos/{owner}/{repo}").get("default_branch", "main")

    def branch_sha(self, owner, repo, branch) -> str | None:
        try:
            return self._get(f"/repos/{owner}/{repo}/git/refs/heads/{branch}")["object"]["sha"]
        except Exception:
            return None

    def file_sha(self, owner, repo, path, branch) -> str | None:
        try:
            return self._get(f"/repos/{owner}/{repo}/contents/{path}?ref={branch}").get("sha")
        except Exception:
            return None

    def upsert_file(self, owner, repo, path, content, branch, msg):
        b64 = base64.b64encode(content.encode()).decode()
        body = {"message": msg, "content": b64, "branch": branch}
        sha = self.file_sha(owner, repo, path, branch)
        if sha:
            body["sha"] = sha
        return self._put(f"/repos/{owner}/{repo}/contents/{path}", body)

    def init_readme(self, owner, repo, branch):
        self.upsert_file(owner, repo, "README.md",
                         f"# {repo}\n\nBuilt with Forge AI ⚡\n",
                         branch, "Initial commit")

    def create_branch(self, owner, repo, branch, sha):
        try:
            self._post(f"/repos/{owner}/{repo}/git/refs",
                       {"ref": f"refs/heads/{branch}", "sha": sha})
        except Exception:
            pass

    def enable_pages(self, owner, repo, branch) -> str | None:
        try:
            r = requests.post(
                f"{self.BASE}/repos/{owner}/{repo}/pages",
                headers=self.h,
                json={"source": {"branch": branch, "path": "/"}},
                timeout=20,
            )
            if r.status_code in (201, 409):
                info = requests.get(f"{self.BASE}/repos/{owner}/{repo}/pages",
                                    headers=self.h, timeout=10)
                if info.ok:
                    return info.json().get("html_url")
        except Exception:
            pass
        return None

    def create_pr(self, owner, repo, head, base, title, body) -> str:
        d = self._post(f"/repos/{owner}/{repo}/pulls",
                       {"title": title, "body": body, "head": head, "base": base})
        return d.get("html_url", "")

    def push_files(self, owner, repo, files, branch="main",
                   commit_msg="⚡ Forge AI", create_pr_to=None,
                   enable_pages=False, log_fn=None) -> dict:
        def log(m):
            if log_fn: log_fn(m)

        if not self.repo_exists(owner, repo):
            log(f"📦 Creating repo {repo}…")
            self.create_repo(repo)

        try:
            db = self.default_branch(owner, repo)
        except Exception:
            db = "main"

        sha = self.branch_sha(owner, repo, db)
        if sha is None:
            log("📄 Initialising repo…")
            self.init_readme(owner, repo, db)
            sha = self.branch_sha(owner, repo, db)

        target = branch
        if target != db:
            log(f"🌿 Creating branch `{target}`…")
            self.create_branch(owner, repo, target, sha)

        changed = skipped = 0
        for path, content in files.items():
            remote_sha = self.file_sha(owner, repo, path, target)
            local_sha = hashlib.sha1(
                f"blob {len(content.encode())}\0{content}".encode()
            ).hexdigest()
            if remote_sha == local_sha:
                skipped += 1
                continue
            log(f"{'✏️' if remote_sha else '➕'}  {path}…")
            try:
                self.upsert_file(owner, repo, path, content, target,
                                 f"{commit_msg}\n\nUpdated {path}")
                changed += 1
            except Exception as e:
                log(f"⚠️  {path}: {e}")

        pages_url = pr_url = None
        if enable_pages:
            log("🌐 Enabling Pages…")
            pages_url = self.enable_pages(owner, repo, target)
            if pages_url:
                log(f"✅ Pages: {pages_url}")

        if create_pr_to and target != create_pr_to and changed > 0:
            log(f"📬 Opening PR → `{create_pr_to}`…")
            pr_url = self.create_pr(owner, repo, target, create_pr_to,
                                    f"⚡ Forge AI — {commit_msg}",
                                    "Automated update from Forge AI.")
            if pr_url:
                log(f"✅ PR: {pr_url}")

        return {
            "branch": target, "pages_url": pages_url, "pr_url": pr_url,
            "repo_url": f"https://github.com/{owner}/{repo}",
            "changed": changed, "skipped": skipped,
        }


# ─────────────────────────────────────────────
# AI CLIENT
# ─────────────────────────────────────────────
class AIClient:
    def __init__(self, key):
        self.key = key

    def _parse(self, data) -> str:
        if isinstance(data, dict):
            if "response" in data: return data["response"]
            if "content"  in data:
                c = data["content"]
                return c[0].get("text", str(c)) if isinstance(c, list) else c
            if "message" in data:  return data["message"]
            if "choices"  in data: return data["choices"][0]["message"]["content"]
        return str(data)

    def ask(self, system: str, user: str, max_tokens: int = 4096) -> str:
        r = requests.post(
            AI_ENDPOINT,
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
            json={"messages": [{"role": "user", "content": f"{system}\n\n{user}"}]},
            timeout=180,
        )
        r.raise_for_status()
        return self._parse(r.json())

    def chat(self, conversation: list[dict]) -> str:
        msgs = [
            {"role": "user",      "content": CHAT_SYSTEM + "\n\nAcknowledge your role briefly."},
            {"role": "assistant", "content": "Got it. I'm Forge AI — your web app building assistant. What would you like to build?"},
        ] + conversation
        r = requests.post(
            AI_ENDPOINT,
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
            json={"messages": msgs},
            timeout=120,
        )
        r.raise_for_status()
        return self._parse(r.json())


# ─────────────────────────────────────────────
# PROMPTS
# ─────────────────────────────────────────────
AGENT_SYSTEM = """You are Forge — the world's most advanced autonomous web application engineer.
Your standard: every output must rival or exceed what a senior team at Vercel, Linear, or Figma would ship. No toy prototypes.

══════════════════════════════════════════════
OUTPUT CONTRACT
══════════════════════════════════════════════
Return ONLY a raw JSON object — zero prose, zero markdown fences.

{
  "summary": "one clear sentence describing what was built or changed",
  "tech_stack": ["html", "css", "js"],
  "actions": [
    { "type": "create", "path": "index.html", "content": "..." },
    { "type": "edit",   "path": "style.css",  "content": "..." },
    { "type": "delete", "path": "old.js" }
  ]
}

══════════════════════════════════════════════
ARCHITECTURE — ALWAYS FOLLOW
══════════════════════════════════════════════
FILE STRUCTURE: Always split into separate files:
  index.html   — semantic HTML5 shell only (no inline styles/scripts)
  style.css    — all CSS, design tokens, animations
  app.js       — app entry, imports from modules
  [feature].js — one file per logical domain (auth.js, api.js, kanban.js, etc.)

JAVASCRIPT RULES:
  - ES Modules everywhere (type="module", import/export)
  - Immutable state pattern: never mutate objects, use spread/Object.assign
  - Strict separation: pure functions → state layer → render layer → event layer
  - Every async call: loading state before → success/error after → always reset
  - Never use innerHTML with user data — use textContent, createElement, or tagged templates
  - Debounce inputs ≥300ms; throttle scroll/resize handlers
  - requestAnimationFrame for all canvas / animation loops

CSS RULES:
  - ALL values as CSS custom properties on :root (colors, spacing, radii, shadows, fonts)
  - Spacing scale: 4px unit (4 8 12 16 20 24 32 40 48 64 80 96 128)
  - Typography scale: 11 13 14 16 18 20 24 30 36 48 60px
  - Use CSS Grid for page layout; Flexbox for alignment within components
  - Dark mode via prefers-color-scheme AND a data-theme toggle
  - All transitions: 150–250ms cubic-bezier(0.4,0,0.2,1)
  - Hover, focus-visible, active states on EVERY interactive element
  - Backdrop-filter: blur() for glassmorphism panels where appropriate

══════════════════════════════════════════════
UI/UX STANDARDS — EVERY APP
══════════════════════════════════════════════
VISUAL QUALITY:
  - Choose a coherent color palette (not Bootstrap defaults) — deep saturated primaries + neutral grays
  - Real typography: import 2 Google Fonts (display + body). Never use system-ui alone.
  - Shadows: 3 levels (sm/md/lg) defined in tokens, used contextually
  - Border radius: consistent token (sm=4px, md=8px, lg=16px, xl=24px, full=9999px)
  - Icons: inline SVG from Lucide (esm.sh/lucide) or Heroicons — never emoji for UI chrome
  - Subtle background texture or gradient — never flat white/black
  - Component spacing feels intentional, not random

INTERACTION QUALITY:
  - Every button: hover (lift + color shift) + active (press down) + focus-visible (ring) + disabled (opacity)
  - Form inputs: floating labels OR clear placeholder + label combos; live validation; success state
  - Loading: skeleton screens (not spinners) for content; button spinner for actions
  - Errors: inline field errors + toast notifications for async failures
  - Empty states: illustrated message with a CTA (not just "No items")
  - Modals: focus trap + Escape to close + body scroll lock + backdrop click to close
  - Drag and drop: ghost preview + drop zone highlight + spring animation on drop

COMPLETENESS:
  - Navigation: header with logo + nav links + mobile hamburger menu
  - Footer with relevant links
  - At least 3–5 populated data items (realistic dummy data — NO lorem ipsum)
  - Responsive at 360 / 768 / 1024 / 1440px
  - Keyboard navigable with logical tab order

══════════════════════════════════════════════
CLONE MODE — when scrape context provided
══════════════════════════════════════════════
Step 1 — ANALYSE the scraped page:
  - Extract the EXACT color palette (primary, secondary, accent, neutrals, backgrounds)
  - Map the typographic system (font families, sizes, weights, line-heights)
  - Identify every major UI section and component
  - Note spacing rhythm, border styles, shadow depth, border radius patterns
  - Catalogue all interactive components (nav, cards, buttons, modals, forms)

Step 2 — REPRODUCE faithfully:
  - Match the visual language precisely — same feel, same spatial rhythm
  - Replicate all major sections with realistic dummy content
  - Use https://picsum.photos/[w]/[h]?random=[n] for image placeholders
  - Keep the same information architecture and navigation structure

Step 3 — UPGRADE these specific things:
  - Animations and micro-interactions (they rarely have enough)
  - Accessibility (add ARIA, keyboard nav, focus styles)
  - Performance (no jQuery, rewrite in vanilla)
  - Mobile responsiveness (fix anything that's broken)
  - Code quality (clean, commented, maintainable)
  - Remove: tracking pixels, cookie banners, analytics, ads, external fonts if slow

Step 4 — DELIVER a fully self-contained build with zero broken links.

══════════════════════════════════════════════
COMPONENT REFERENCE
══════════════════════════════════════════════

SIDEBAR NAVIGATION:
  <nav class="sidebar" role="navigation" aria-label="Main navigation">
    <div class="sidebar__logo">…</div>
    <ul class="nav-list" role="list">
      <li><a class="nav-item" href="#" aria-current="page">…</a></li>
    </ul>
  </nav>

CARD:
  <article class="card" tabindex="0">
    <div class="card__media"><img loading="lazy" …></div>
    <div class="card__body">
      <h3 class="card__title">…</h3>
      <p class="card__desc">…</p>
    </div>
    <footer class="card__actions">…</footer>
  </article>

MODAL (accessible):
  <div class="modal-overlay" role="dialog" aria-modal="true" aria-labelledby="modal-title" hidden>
    <div class="modal">
      <h2 id="modal-title">…</h2>
      <button class="btn-icon modal__close" aria-label="Close dialog">✕</button>
      <div class="modal__body">…</div>
    </div>
  </div>
  // JS: focusTrap, Escape listener, scroll-lock, aria-hidden on background

TOAST:
  function showToast(message, type = 'success', duration = 3500) {
    const el = document.createElement('div');
    el.className = `toast toast--${type}`;
    el.setAttribute('role', 'alert');
    el.setAttribute('aria-live', 'polite');
    el.textContent = message;
    document.getElementById('toast-container').appendChild(el);
    requestAnimationFrame(() => el.classList.add('toast--visible'));
    setTimeout(() => { el.classList.remove('toast--visible'); el.addEventListener('transitionend', () => el.remove()); }, duration);
  }

DATA TABLE with sort + filter + pagination:
  // State: { data, sortKey, sortDir, filter, page, perPage }
  // Pure function: filterSort(state) → displayRows
  // Render: renderTable(rows, state) → replaces tbody innerHTML via DOM

══════════════════════════════════════════════
LIBRARY ALLOWLIST (CDN only — no build step)
══════════════════════════════════════════════
Charts    → Chart.js  cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js
Icons     → Lucide    esm.sh/lucide@latest  (import { createIcons, … } from 'lucide')
Markdown  → marked    cdnjs.cloudflare.com/ajax/libs/marked/12.0.0/marked.min.js
Date pick → flatpickr cdnjs.cloudflare.com/ajax/libs/flatpickr/4.6.13/flatpickr.min.js
Rich text → Quill     cdnjs.cloudflare.com/ajax/libs/quill/2.0.0/quill.js
Anim      → GSAP (free tier) cdnjs.cloudflare.com/ajax/libs/gsap/3.12.5/gsap.min.js
3D        → Three.js  cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js

NEVER USE without explicit request: jQuery, Bootstrap, React, Vue, Angular, any NPM build.

══════════════════════════════════════════════
PRESERVATION LAW
══════════════════════════════════════════════
When editing an existing project you receive COMPLETE current file contents.
- Copy ALL existing content into "content", then apply ONLY the requested change.
- NEVER drop features, components, data, or styles unless explicitly asked to remove them.
- Only include files that actually change — omit unchanged files entirely.

══════════════════════════════════════════════
AI-POWERED APP PATTERN
══════════════════════════════════════════════
Endpoint: POST https://raujzsawwpmixwlcgcgs.supabase.co/functions/v1/public-ai-api
Headers : { "Authorization": "Bearer FORGE_AI_KEY_PLACEHOLDER", "Content-Type": "application/json" }
Body    : { "messages": [...conversationHistory] }

ALWAYS inject a strong persona system prompt as the FIRST two messages:
  { role: "user",      content: "You are [AppName]. [Persona rules]. NEVER do web searches..." }
  { role: "assistant", content: "Understood. I am [AppName], ready to [purpose]." }
  ...then append real conversation history...

Use FORGE_AI_KEY_PLACEHOLDER — it is injected at runtime. Never hardcode a real key."""

CHAT_SYSTEM = """You are Forge AI — a sharp, smart, senior-engineer assistant inside the Forge web app builder.

STRICT RULES:
1. Never perform web searches or return search results under any circumstances.
2. Only discuss: web development, app architecture, UI/UX, design patterns, how to use Forge.
3. Off-topic questions → politely redirect to building.
4. Keep answers concise and actionable — bullet points where helpful.
5. Clone requests → tell user to paste the URL in the URL field and hit ⚡ Build.
6. App idea → ask ONE smart clarifying question, then say "Hit ⚡ Build when ready!".
7. Always produce sharp, smart apps depending on the user's need. If you are unsure of the need, ask and then say "Hit ⚡ Build when ready to answer!".
8. When building, always ensure that you are alloting and building according to needs and upgrade on that. Make easy UX/UI and design patterns. Always go through an app, plan required codes and then build."""


# ─────────────────────────────────────────────
# AGENT RUNNER
# ─────────────────────────────────────────────
def run_agent(ai, vfs, task, existing_files=None, scrape_context=None) -> dict:
    parts = []
    if scrape_context:
        parts.append(f"SCRAPED PAGE — study every detail to clone and upgrade:\n\n{scrape_context}")
    if existing_files:
        snippets = [f"### {p}\n```\n{c}\n```" for p, c in existing_files.items()]
        parts.append("CURRENT PROJECT FILES (full content — preserve everything not mentioned):\n\n"
                     + "\n\n".join(snippets))

    ctx = ("\n\n" + "\n\n".join(parts)) if parts else ""

    if existing_files:
        user_msg = (f"TASK: {task}{ctx}\n\n"
                    "Return ONLY changed files with FULL new content. Preserve everything else.")
    else:
        user_msg = f"TASK: {task}{ctx}\n\nBuild from scratch. Return the complete project JSON."

    raw = ai.ask(AGENT_SYSTEM, user_msg, max_tokens=8192)
    raw = raw.strip()

    # Strip accidental fences
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
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
# PUBLISH
# ─────────────────────────────────────────────
def publish_netlify(vfs: VirtualFS) -> dict:
    z = vfs.to_zip()

    # Try raw ZIP POST
    try:
        r = requests.post(
            "https://api.netlify.com/api/v1/sites",
            headers={"Content-Type": "application/zip"},
            data=z, timeout=60,
        )
        if r.status_code in (200, 201):
            d = r.json()
            sub = d.get("subdomain") or d.get("id", "app")
            return {"url": f"https://{sub}.netlify.app", "method": "Netlify"}
    except Exception:
        pass

    # Try multipart
    try:
        r = requests.post(
            "https://api.netlify.com/api/v1/sites",
            files={"zip": ("project.zip", z, "application/zip")},
            timeout=60,
        )
        if r.status_code in (200, 201):
            d = r.json()
            sub = d.get("subdomain") or d.get("id", "app")
            return {"url": f"https://{sub}.netlify.app", "method": "Netlify"}
    except Exception:
        pass

    # Fallback: data URI
    html = vfs.to_single_html()
    b64 = base64.b64encode(html.encode()).decode()
    return {"url": f"data:text/html;base64,{b64}", "method": "data-uri", "html": html}


# ─────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────
def active_proj() -> dict:
    return st.session_state["projects"][st.session_state["active_project"]]


def _new_proj_data(name="untitled") -> tuple[str, dict]:
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
        "github_repo": "",
        "github_branch": "main",
        "github_pages_url": None,
        "github_last_push": None,
        "created_at": time.strftime("%b %d %H:%M"),
    }


def init_state():
    # Load persisted projects on first run
    if "projects" not in st.session_state:
        saved = _load_persisted()
        if saved:
            st.session_state["projects"] = saved
            # Rebuild vfs_root (temp dirs are gone after restart)
            for pid, p in st.session_state["projects"].items():
                p["vfs_root"] = tempfile.mkdtemp(prefix=f"forge_{pid}_")
                p.setdefault("chat_history", [])
                p.setdefault("build_log", [])
                p.setdefault("history", [])
                p.setdefault("last_preview_html", None)
                p.setdefault("published_url", None)
                p.setdefault("published_method", None)
                p.setdefault("scrape_cache", {})
                p.setdefault("github_repo", "")
                p.setdefault("github_branch", "main")
                p.setdefault("github_pages_url", None)
                p.setdefault("github_last_push", None)
                p.setdefault("created_at", "")
                # Re-write VFS files to disk
                for fpath, fcontent in p.get("vfs_files", {}).items():
                    disk = Path(p["vfs_root"]) / fpath
                    disk.parent.mkdir(parents=True, exist_ok=True)
                    disk.write_text(fcontent, encoding="utf-8")
        else:
            pid, pdata = _new_proj_data()
            st.session_state["projects"] = {pid: pdata}
            st.session_state["active_project"] = pid

    if "active_project" not in st.session_state:
        st.session_state["active_project"] = list(st.session_state["projects"].keys())[0]

    # Ensure active project exists
    if st.session_state["active_project"] not in st.session_state["projects"]:
        st.session_state["active_project"] = list(st.session_state["projects"].keys())[0]

    # Ensure vfs_root on disk for all projects
    for pid, p in st.session_state["projects"].items():
        if "vfs_root" not in p or not Path(p["vfs_root"]).exists():
            p["vfs_root"] = tempfile.mkdtemp(prefix=f"forge_{pid}_")

    st.session_state.setdefault("selected_file", None)
    st.session_state.setdefault("last_scrape_url", "")
    st.session_state.setdefault("github_token", "")
    st.session_state.setdefault("gh_log", [])


def new_project():
    pid, pdata = _new_proj_data()
    st.session_state["projects"][pid] = pdata
    st.session_state["active_project"] = pid
    st.session_state["selected_file"] = None
    st.session_state["last_scrape_url"] = ""
    st.session_state["gh_log"] = []
    persist_projects()


def switch_project(pid: str):
    st.session_state["active_project"] = pid
    st.session_state["selected_file"] = None
    st.session_state["last_scrape_url"] = active_proj().get("last_scrape_url_saved", "")
    st.session_state["gh_log"] = []


init_state()
vfs = VirtualFS()


# ─────────────────────────────────────────────
# STYLES
# ─────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Syne:wght@400;700;800&display=swap');

html, body, [data-testid="stAppViewContainer"] { background:#0a0a0f !important; color:#e8e6e3 !important; }
[data-testid="stSidebar"] { background:#0d0d14 !important; border-right:1px solid #1e1e2e !important; }
h1,h2,h3 { font-family:'Syne',sans-serif !important; letter-spacing:-0.02em; }
code,pre,.stCode { font-family:'IBM Plex Mono',monospace !important; font-size:12px !important; }

.stTextInput input, .stTextArea textarea, .stSelectbox select {
    background:#12121c !important; border:1px solid #2a2a3e !important;
    color:#e8e6e3 !important; border-radius:6px !important; font-family:'IBM Plex Mono',monospace !important;
}
.stTextInput input:focus, .stTextArea textarea:focus {
    border-color:#7c6af7 !important; box-shadow:0 0 0 2px rgba(124,106,247,0.2) !important;
}
.stButton > button {
    background:transparent !important; border:1px solid #2a2a3e !important;
    color:#a0a0c0 !important; border-radius:6px !important;
    font-family:'IBM Plex Mono',monospace !important; transition:all 0.2s ease !important;
}
.stButton > button[kind="primary"] {
    background:linear-gradient(135deg,#7c6af7,#a78bfa) !important;
    color:#fff !important; border:none !important;
    font-family:'Syne',sans-serif !important; font-weight:700 !important;
}
.stButton > button:hover { transform:translateY(-1px) !important; box-shadow:0 4px 20px rgba(124,106,247,0.3) !important; }
.streamlit-expanderHeader { background:#12121c !important; border:1px solid #1e1e2e !important; border-radius:6px !important; font-family:'IBM Plex Mono',monospace !important; color:#a0a0c0 !important; }
.stAlert { border-radius:6px !important; font-family:'IBM Plex Mono',monospace !important; font-size:13px !important; }
.stTabs [role="tablist"] { gap:4px; border-bottom:1px solid #1e1e2e; }
.stTabs [role="tab"] { background:transparent !important; color:#606080 !important; border:none !important; font-family:'IBM Plex Mono',monospace !important; font-size:12px !important; padding:6px 14px !important; }
.stTabs [role="tab"][aria-selected="true"] { color:#a78bfa !important; border-bottom:2px solid #7c6af7 !important; }
.build-log { background:#0d0d14; border:1px solid #1e1e2e; border-radius:8px; padding:12px 16px; font-family:'IBM Plex Mono',monospace; font-size:12px; color:#7c6af7; max-height:200px; overflow-y:auto; }
.scrape-badge { display:inline-block; background:#1a1a2e; border:1px solid #7c6af7; border-radius:4px; padding:3px 8px; font-family:'IBM Plex Mono',monospace; font-size:11px; color:#a78bfa; margin-bottom:6px; }
.gh-card { background:#12121c; border:1px solid #2a2a3e; border-radius:10px; padding:16px 20px; margin:8px 0; font-family:'IBM Plex Mono',monospace; font-size:12px; }
.gh-badge { display:inline-block; background:#1a2a1a; border:1px solid #2ea043; border-radius:4px; padding:3px 8px; font-size:11px; color:#56d364; margin:2px 3px; font-family:'IBM Plex Mono',monospace; }
.gh-badge-purple { display:inline-block; background:#1a1a2e; border:1px solid #7c6af7; border-radius:4px; padding:3px 8px; font-size:11px; color:#a78bfa; margin:2px 3px; font-family:'IBM Plex Mono',monospace; }
.proj-item { font-family:'IBM Plex Mono',monospace; font-size:11px; color:#606080; padding:2px 0; }
hr { border-color:#1e1e2e !important; }
.stCaption { color:#606080 !important; }
</style>
""", unsafe_allow_html=True)


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
        ts = pdata.get("created_at", "")
        pub = pdata.get("published_url")
        pub_method = pdata.get("published_method", "")
        pages = pdata.get("github_pages_url")

        col_p, col_del = st.columns([5, 1])
        with col_p:
            disp = f"{'▶ ' if is_active else ''}{label}"
            if st.button(disp, key=f"proj_{pid}", use_container_width=True,
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
                    persist_projects()
                    st.rerun()

        if ts:
            st.markdown(f'<div class="proj-item">🕐 {ts}</div>', unsafe_allow_html=True)

        if pub and pub_method != "data-uri":
            st.markdown(
                f'<a href="{pub}" target="_blank" style="font-family:IBM Plex Mono,monospace;'
                f'font-size:10px;color:#7c6af7;text-decoration:none;">🌐 {pub.replace("https://","")[:40]}</a>',
                unsafe_allow_html=True,
            )
        if pages:
            st.markdown(
                f'<a href="{pages}" target="_blank" style="font-family:IBM Plex Mono,monospace;'
                f'font-size:10px;color:#56d364;text-decoration:none;">🐙 {pages.replace("https://","")[:40]}</a>',
                unsafe_allow_html=True,
            )

    st.divider()

    # Project rename
    proj_name = st.text_input(
        "Rename project",
        value=active_proj()["name"],
        label_visibility="collapsed",
        placeholder="project-name",
    )
    if proj_name != active_proj()["name"]:
        active_proj()["name"] = proj_name
        persist_projects()
        st.rerun()

    # File tree
    st.markdown("**Files**")
    file_list = vfs.list_files()
    if not file_list:
        st.caption("No files yet.")
    else:
        for f in file_list:
            c1, c2 = st.columns([5, 1])
            with c1:
                if st.button(f"📄 {f}", key=f"file_{f}", use_container_width=True):
                    st.session_state["selected_file"] = f
            with c2:
                if st.button("✕", key=f"del_{f}"):
                    vfs.delete(f)
                    persist_projects()
                    if st.session_state["selected_file"] == f:
                        st.session_state["selected_file"] = None
                    st.rerun()

    st.divider()

    if st.button("🗑 Clear project", use_container_width=True):
        vfs.clear()
        for k in ("history", "chat_history", "build_log"):
            active_proj()[k] = []
        for k in ("last_preview_html", "published_url", "published_method",
                  "github_pages_url", "github_last_push"):
            active_proj()[k] = None
        active_proj()["scrape_cache"] = {}
        st.session_state["selected_file"] = None
        st.session_state["last_scrape_url"] = ""
        st.session_state["gh_log"] = []
        persist_projects()
        st.rerun()


# ─────────────────────────────────────────────
# MAIN TABS
# ─────────────────────────────────────────────
main_tabs = st.tabs(["🏗 Build", "👁 Preview", "📂 Editor", "🐙 GitHub", "📜 History"])


# ─── TAB 1 : BUILD ───────────────────────────
with main_tabs[0]:

    # Chat history
    if not active_proj()["chat_history"]:
        st.markdown(
            '<div style="text-align:center;padding:40px 0 20px;'
            'font-family:IBM Plex Mono,monospace;color:#606080;font-size:13px;">'
            '⚡ Describe what to build, paste a URL to clone, or ask anything about web dev.'
            '</div>',
            unsafe_allow_html=True,
        )

    for msg in active_proj()["chat_history"]:
        is_user = msg["role"] == "user"
        bg    = "#2a2a3e" if is_user else "#12121c"
        align = "flex-end" if is_user else "flex-start"
        icon  = "🧑" if is_user else "⚡"
        st.markdown(
            f'<div style="display:flex;justify-content:{align};margin:6px 0;">'
            f'<div style="background:{bg};border-radius:10px;padding:10px 14px;'
            f'max-width:82%;font-family:IBM Plex Mono,monospace;font-size:13px;'
            f'color:#e8e6e3;line-height:1.6;white-space:pre-wrap;">'
            f'{icon}&nbsp; {msg["content"]}'
            f'</div></div>',
            unsafe_allow_html=True,
        )

    st.divider()

    # URL field
    url_col, clr_col = st.columns([9, 1])
    with url_col:
        scrape_url_val = st.text_input(
            "🔗 URL to clone",
            value=st.session_state.get("last_scrape_url", ""),
            placeholder="https://example.com — paste a URL to clone/draw inspiration from",
            key="scrape_url_field",
        )
    with clr_col:
        st.markdown("<div style='margin-top:28px'>", unsafe_allow_html=True)
        if st.button("✕", key="clr_url"):
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
            placeholder="Clone this with dark mode… Build a kanban with drag-and-drop… Add a login modal…",
            key="prompt_input", label_visibility="collapsed",
        )
    with col2:
        build_clicked = st.button("⚡ Build", type="primary", use_container_width=True)
    with col3:
        mode = st.radio("Mode", ["✨ New", "✏️ Edit"], horizontal=False,
                        index=0 if not vfs.list_files() else 1)

    BUILD_VERBS = [
        "build","create","make","add","generate","write","update","change","fix","edit",
        "remove","delete","refactor","style","implement","convert","improve","redesign",
        "dark mode","light mode","responsive","mobile","feature","clone","copy","replicate",
        "upgrade","scrape","port","animate","add feature","integrate","connect","deploy",
    ]

    def is_build_intent(t: str) -> bool:
        return any(v in t.lower() for v in BUILD_VERBS)

    if build_clicked:
        if not AI_KEY:
            st.error("⚠️  Set the `COMPLEX_AI_KEY` environment variable.")
        elif not prompt.strip() and not scrape_url_val.strip():
            st.warning("Enter a message or a URL to clone.")
        else:
            ai = AIClient(AI_KEY)
            user_text = prompt.strip() or f"Clone and upgrade: {scrape_url_val.strip()}"
            active_proj()["chat_history"].append({"role": "user", "content": user_text})

            url_given = scrape_url_val.strip().startswith("http")

            if is_build_intent(user_text) or url_given or "✨ New" in mode:
                # Scrape if URL given
                scrape_ctx = None
                if url_given:
                    cache_key = scrape_url_val.strip()
                    cached = active_proj()["scrape_cache"].get(cache_key)
                    if cached:
                        scrape_result = cached
                        active_proj()["build_log"].append("📋 Using cached scrape")
                    else:
                        with st.spinner(f"🔍 Scraping {cache_key[:50]}…"):
                            scrape_result = scrape_url(cache_key)
                            active_proj()["scrape_cache"][cache_key] = scrape_result
                        msg = (f"⚠️ Partial scrape ({scrape_result['error']})"
                               if scrape_result.get("error")
                               else f"✅ Scraped: {scrape_result['title'] or cache_key}")
                        active_proj()["build_log"].append(msg)
                    scrape_ctx = build_scrape_context(scrape_result)
                    st.session_state["last_scrape_url"] = cache_key
                    active_proj()["last_scrape_url_saved"] = cache_key

                existing = dict(vfs.files) if "✏️ Edit" in mode and vfs.list_files() else None

                with st.spinner("⚡ Forge is building…"):
                    try:
                        result = run_agent(ai, vfs, user_text,
                                           existing_files=existing,
                                           scrape_context=scrape_ctx)
                    except json.JSONDecodeError as e:
                        err = f"AI returned invalid JSON — try rephrasing. ({e})"
                        active_proj()["chat_history"].append({"role": "assistant", "content": err})
                        persist_projects()
                        st.rerun()
                    except Exception as e:
                        err = f"Error: {e}"
                        active_proj()["chat_history"].append({"role": "assistant", "content": err})
                        persist_projects()
                        st.rerun()

                summary  = result.get("summary", "Done.")
                actions  = result.get("actions", [])
                stack    = result.get("tech_stack", [])

                log = [f"✅ {summary}", ""]
                for a in actions:
                    icon = {"create": "➕", "edit": "✏️", "delete": "🗑"}.get(a.get("type"), "•")
                    log.append(f"{icon}  {a.get('path','?')}")
                if stack:
                    log.append(f"\n🔧 Stack: {', '.join(stack)}")
                active_proj()["build_log"].extend(log)
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
                        push_res = gh.push_files(
                            owner, repo_name, dict(vfs.files),
                            branch=active_proj().get("github_branch", "main"),
                            commit_msg=f"⚡ {summary}",
                            enable_pages=True,
                        )
                        if push_res.get("pages_url"):
                            active_proj()["github_pages_url"] = push_res["pages_url"]
                        active_proj()["github_last_push"] = push_res
                        active_proj()["build_log"].append(
                            f"🐙 Auto-synced ({push_res['changed']} files)"
                        )
                    except Exception as e:
                        active_proj()["build_log"].append(f"⚠️ GitHub sync: {e}")

                reply = f"✅ {summary} — switch to **Preview** to see it!"
                if active_proj().get("github_pages_url"):
                    reply += f"\n🌐 Also live on [GitHub Pages]({active_proj()['github_pages_url']})"

            else:
                # Chat path
                with st.spinner("Thinking…"):
                    try:
                        reply = ai.chat([
                            {"role": m["role"], "content": m["content"]}
                            for m in active_proj()["chat_history"]
                        ])
                    except Exception as e:
                        reply = f"Error: {e}"

            active_proj()["chat_history"].append({"role": "assistant", "content": reply})
            persist_projects()
            st.rerun()

    if active_proj()["build_log"]:
        with st.expander("Build Log", expanded=False):
            st.markdown(
                '<div class="build-log">'
                + "<br>".join(f"› {l}" for l in active_proj()["build_log"][-80:])
                + "</div>",
                unsafe_allow_html=True,
            )


# ─── TAB 2 : PREVIEW ─────────────────────────
with main_tabs[1]:
    preview_html = active_proj().get("last_preview_html")

    if not preview_html:
        st.info("Build something first — the live preview will appear here.")
    else:
        proj       = active_proj()
        pub_url    = proj.get("published_url")
        pub_method = proj.get("published_method", "")
        pages_url  = proj.get("github_pages_url")

        c1, c2, c3, c4 = st.columns([2, 2, 2, 4])

        with c1:
            if st.button("🌐 Publish", type="primary", use_container_width=True,
                         help="Deploy to Netlify (free random domain)"):
                if not vfs.list_files():
                    st.error("Nothing to publish.")
                else:
                    with st.spinner("Publishing to Netlify…"):
                        try:
                            res = publish_netlify(vfs)
                            proj["published_url"]    = res["url"]
                            proj["published_method"] = res["method"]
                            persist_projects()
                            if res["method"] == "data-uri":
                                st.warning("Netlify unavailable — packaged as portable link.")
                            else:
                                st.success(f"Live at [{res['url']}]({res['url']})")
                                st.balloons()
                        except Exception as e:
                            st.error(f"Publish failed: {e}")

        with c2:
            st.download_button("⬇️ HTML", data=preview_html,
                               file_name="index.html", mime="text/html",
                               use_container_width=True)
        with c3:
            if vfs.list_files():
                st.download_button("📦 ZIP", data=vfs.to_zip(),
                                   file_name="forge-project.zip",
                                   mime="application/zip", use_container_width=True)
        with c4:
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
                    f'🐙 <a href="{pages_url}" target="_blank" style="color:#56d364;">GitHub Pages</a>'
                )
            if links:
                st.markdown(" &nbsp;·&nbsp; ".join(links), unsafe_allow_html=True)

        st.divider()
        st.components.v1.html(preview_html, height=700, scrolling=True)


# ─── TAB 3 : EDITOR ──────────────────────────
with main_tabs[2]:
    files = vfs.list_files()
    if not files:
        st.info("No files yet.")
    else:
        sel = st.selectbox(
            "File", files,
            index=files.index(st.session_state["selected_file"])
            if st.session_state["selected_file"] in files else 0,
            key="editor_sel",
        )
        st.session_state["selected_file"] = sel

        content = vfs.read(sel) or ""
        edited = st.text_area(f"Editing: `{sel}`", value=content,
                              height=520, key=f"ed_{sel}")

        cs, cr = st.columns(2)
        with cs:
            if st.button("💾 Save", use_container_width=True):
                vfs.write(sel, edited)
                h = vfs.get_entry_html()
                if h:
                    active_proj()["last_preview_html"] = vfs.inject_css_js(h)
                persist_projects()
                st.success("Saved!")
                st.rerun()
        with cr:
            if st.button("↩ Revert", use_container_width=True):
                st.rerun()

        st.divider()
        st.markdown("**New file**")
        new_path = st.text_input("Path (e.g. components/modal.js)", key="new_fp")
        if st.button("➕ Create") and new_path.strip():
            vfs.write(new_path.strip(), "")
            st.session_state["selected_file"] = new_path.strip()
            persist_projects()
            st.rerun()


# ─── TAB 4 : GITHUB ──────────────────────────
with main_tabs[3]:
    st.markdown("### 🐙 GitHub Sync")
    st.caption("Push to GitHub and optionally host free on GitHub Pages.")

    gh_token = st.text_input(
        "GitHub Personal Access Token",
        type="password",
        value=st.session_state.get("github_token", ""),
        placeholder="ghp_xxxx",
        help="Settings → Developer settings → Personal access tokens → Fine-grained. Needs: Contents + Pages write.",
        key="gh_token_in",
    )
    if gh_token != st.session_state.get("github_token", ""):
        st.session_state["github_token"] = gh_token

    if gh_token:
        try:
            _gh_check = GitHubClient(gh_token)
            gh_user = _gh_check.whoami()
            st.markdown(f'<span class="gh-badge">✓ @{gh_user}</span>', unsafe_allow_html=True)
        except Exception:
            st.markdown(
                '<span style="color:#f87171;font-family:IBM Plex Mono,monospace;font-size:12px;">'
                '✗ Invalid token</span>', unsafe_allow_html=True,
            )
            gh_user = None
    else:
        gh_user = None

    st.divider()

    cr, cb = st.columns([3, 2])
    with cr:
        gh_repo_in = st.text_input(
            "Repository name",
            value=active_proj().get("github_repo", ""),
            placeholder="my-forge-app",
        )
        if gh_repo_in != active_proj().get("github_repo", ""):
            active_proj()["github_repo"] = gh_repo_in

    with cb:
        gh_branch_in = st.text_input(
            "Branch",
            value=active_proj().get("github_branch", "main"),
            placeholder="main",
        )
        if gh_branch_in != active_proj().get("github_branch", "main"):
            active_proj()["github_branch"] = gh_branch_in

    cp, cpages = st.columns(2)
    with cp:
        gh_private = st.checkbox("Private repo", value=False)
    with cpages:
        gh_pages_on = st.checkbox("Enable GitHub Pages", value=True)

    cpr, ccm = st.columns(2)
    with cpr:
        gh_pr = st.checkbox("Open Pull Request", value=False)
    with ccm:
        gh_msg = st.text_input("Commit message", value="⚡ Forge AI — update")

    st.divider()

    push_btn = st.button(
        "🚀 Push to GitHub", type="primary",
        use_container_width=True,
        disabled=not (gh_token and gh_repo_in and vfs.list_files()),
    )
    if not gh_token:    st.caption("↑ Enter your GitHub token.")
    elif not gh_repo_in: st.caption("↑ Enter a repository name.")
    elif not vfs.list_files(): st.caption("↑ Build something first.")

    if push_btn and gh_token and gh_repo_in and vfs.list_files():
        gh_log_lines: list[str] = []
        with st.spinner("Syncing with GitHub…"):
            try:
                _gh2 = GitHubClient(gh_token)
                owner = _gh2.whoami()
                repo_name = gh_repo_in.strip().split("/")[-1]
                target = gh_branch_in.strip() or "main"
                pr_to = None
                if gh_pr and target == "main":
                    target = "forge-ai"
                    pr_to = "main"
                elif gh_pr:
                    pr_to = gh_branch_in.strip()

                push_res = _gh2.push_files(
                    owner=owner, repo=repo_name, files=dict(vfs.files),
                    branch=target, commit_msg=gh_msg or "⚡ Forge AI — update",
                    create_pr_to=pr_to, enable_pages=gh_pages_on,
                    log_fn=lambda m: gh_log_lines.append(m),
                )
                active_proj()["github_repo"] = repo_name
                active_proj()["github_branch"] = target
                active_proj()["github_last_push"] = push_res
                if push_res.get("pages_url"):
                    active_proj()["github_pages_url"] = push_res["pages_url"]
                st.session_state["gh_log"] = gh_log_lines
                persist_projects()
            except Exception as e:
                gh_log_lines.append(f"✗ {e}")
                st.session_state["gh_log"] = gh_log_lines
                st.error(f"Push failed: {e}")
        st.rerun()

    # Results
    last_push = active_proj().get("github_last_push")
    if last_push:
        st.markdown('<div class="gh-card">', unsafe_allow_html=True)
        st.markdown("**Last sync**")
        changed = last_push.get("changed", 0)
        skipped = last_push.get("skipped", 0)
        st.markdown(
            f'<span class="gh-badge">✓ {changed} file{"s" if changed!=1 else ""} pushed</span>'
            f'<span class="gh-badge-purple">⊘ {skipped} unchanged</span>',
            unsafe_allow_html=True,
        )
        repo_url = last_push.get("repo_url", "")
        branch   = last_push.get("branch", "")
        pages    = last_push.get("pages_url", "")
        pr_url   = last_push.get("pr_url", "")
        for link_html in [
            (repo_url and f'<a href="{repo_url}/tree/{branch}" target="_blank" style="color:#a78bfa;">📁 {repo_url.replace("https://github.com/","")}/{branch}</a>'),
            (pr_url   and f'<a href="{pr_url}" target="_blank" style="color:#f9a8d4;">📬 Pull Request</a>'),
            (pages    and f'<a href="{pages}" target="_blank" style="color:#56d364;">🌐 {pages}</a>'),
        ]:
            if link_html:
                st.markdown(link_html, unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

    if st.session_state.get("gh_log"):
        with st.expander("Push log", expanded=True):
            st.markdown(
                '<div class="build-log">'
                + "<br>".join(f"› {l}" for l in st.session_state["gh_log"])
                + "</div>",
                unsafe_allow_html=True,
            )

    with st.expander("📖 Token setup guide"):
        st.markdown("""
**Fine-grained token (recommended):**
1. github.com → Settings → Developer settings → Personal access tokens → Fine-grained
2. Set expiry (90 days)
3. Repository permissions: **Contents** Read+Write, **Pages** Read+Write, **Pull requests** Read+Write
4. Paste above

Token is stored in your browser session only — never sent anywhere except GitHub API.
        """)


# ─── TAB 5 : HISTORY ─────────────────────────
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
                f'<div style="background:{color};border-radius:8px;padding:10px 14px;'
                f'margin:6px 0;font-family:IBM Plex Mono,monospace;font-size:13px;'
                f'color:#e8e6e3;text-align:{align};">'
                f'{icon}&nbsp;&nbsp;{text}</div>',
                unsafe_allow_html=True,
            )
