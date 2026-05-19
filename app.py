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
    """
    Fetch a URL and return a structured digest for the AI.
    """
    result = {
        "url": url, "title": "", "html_raw": "", "inline_styles": "",
        "inline_scripts": "", "linked_css_urls": [], "linked_js_urls": [],
        "text_content": "", "meta": {}, "error": None,
    }
    try:
        resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=timeout,
                            allow_redirects=True)
        resp.raise_for_status()
        raw = resp.text

        # title
        m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
        result["title"] = m.group(1).strip() if m else ""

        # meta tags
        for m in re.finditer(
            r'<meta\s+(?:name|property)=["\']([^"\']+)["\'][^>]*content=["\']([^"\']*)["\']',
            raw, re.I
        ):
            result["meta"][m.group(1)] = m.group(2)

        # inline <style> blocks
        styles = re.findall(r"<style[^>]*>(.*?)</style>", raw, re.I | re.S)
        result["inline_styles"] = "\n\n".join(styles)[:30_000]

        # inline <script> blocks (no src=)
        scripts = re.findall(
            r"<script(?![^>]*\bsrc\b)[^>]*>(.*?)</script>", raw, re.I | re.S
        )
        result["inline_scripts"] = "\n\n".join(scripts)[:30_000]

        # linked CSS / JS
        css_links = re.findall(
            r'<link[^>]+rel=["\']stylesheet["\'][^>]*href=["\']([^"\']+)["\']',
            raw, re.I
        )
        result["linked_css_urls"] = [urljoin(url, h) for h in css_links[:8]]

        js_links = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', raw, re.I)
        result["linked_js_urls"] = [urljoin(url, s) for s in js_links[:8]]

        # visible text
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        result["text_content"] = text[:15_000]

        # raw HTML (capped)
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
# LIVE PREVIEW SERVER
# ─────────────────────────────────────────────
_server_lock = threading.Lock()

def ensure_preview_server(vfs: VirtualFS):
    if st.session_state.get("preview_server_started"):
        return
    root = vfs.root

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=root, **kwargs)
        def log_message(self, *args):
            pass

    with _server_lock:
        if not st.session_state.get("preview_server_started"):
            try:
                httpd = socketserver.TCPServer(("", PREVIEW_PORT), Handler)
                httpd.allow_reuse_address = True
                threading.Thread(target=httpd.serve_forever, daemon=True).start()
                st.session_state["preview_server_started"] = True
                st.session_state["preview_httpd"] = httpd
            except OSError:
                st.session_state["preview_server_started"] = True


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
# AGENT PROMPTS
# ─────────────────────────────────────────────
AGENT_SYSTEM_PROMPT = """IMPORTANT: You are Forge, an elite autonomous web app builder and UI engineer. You output ONLY raw JSON — no prose, no markdown, no explanations.

OUTPUT FORMAT — return this exact JSON shape and nothing else:
{
  "summary": "one-line description of what you built or changed",
  "actions": [
    { "type": "create", "path": "index.html", "content": "..." },
    { "type": "edit",   "path": "style.css",  "content": "..." },
    { "type": "delete", "path": "old.js" }
  ]
}

CORE RULES:
- "create" = new file, "edit" = full replacement, "delete" = remove file.
- PRESERVATION: You receive COMPLETE current file contents. Copy ALL existing content, then apply only the requested change. NEVER remove features, functions, styles, or text unless explicitly asked.
- Only include files that actually change. Unchanged files: omit entirely.
- Build production-quality, visually stunning, fully functional apps.
- Prefer vanilla HTML/CSS/JS + Tailwind CDN + Google Fonts for portability.
- Apps must be fully interactive and functional end-to-end.
- Return JSON only. No prose. No markdown. No web searches.

WHEN GIVEN A SCRAPED PAGE CONTEXT:
- You are being asked to CLONE and UPGRADE the provided page.
- Study its layout, color palette, typography, spacing, component structure, and interaction patterns.
- Reproduce the LOOK AND FEEL faithfully, then upgrade with:
  • Better animations and micro-interactions
  • Cleaner, more modern code (no legacy hacks, no jQuery unless needed)
  • Mobile responsiveness if missing
  • Accessibility improvements (aria labels, keyboard nav, focus styles)
  • Any additional features the user explicitly requested
- Extract components intelligently: nav, hero, cards, footer → separate logical sections in code.
- Do NOT hotlink images from the scraped domain — use https://picsum.photos/{width}/{height} as placeholders.
- Strip tracking scripts, cookie banners, ads, and third-party analytics entirely.
- Always produce a self-contained result with NO external dependencies except CDN links.

MULTI-FILE STRATEGY:
- Complex apps: split into index.html + style.css + app.js
- Simple one-pagers: keep everything in index.html
- Always ensure index.html is the entry point.

AI-POWERED APP PATTERN (when building AI chat features):
  POST https://raujzsawwpmixwlcgcgs.supabase.co/functions/v1/public-ai-api
  Headers: { "Authorization": "Bearer FORGE_AI_KEY_PLACEHOLDER", "Content-Type": "application/json" }
  Body messages: [systemTurn, assistantAck, ...history, userTurn]
  Always use FORGE_AI_KEY_PLACEHOLDER as the Bearer token — it is injected at runtime."""

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
# PUBLISH  (multi-strategy, always succeeds)
# ─────────────────────────────────────────────
def publish_app(vfs: VirtualFS) -> dict:
    """
    Strategy 1: Netlify anonymous deploy (application/zip POST)
    Strategy 2: Netlify anonymous deploy (multipart form)
    Strategy 3: data: URI fallback — always works, no external service needed
    Returns {"url": str, "method": str, "html"?: str}
    """
    zip_bytes = vfs.to_zip_bytes()

    # ── Strategy 1: Netlify raw zip POST ──────────────
    try:
        resp = requests.post(
            "https://api.netlify.com/api/v1/sites",
            headers={"Content-Type": "application/zip"},
            data=zip_bytes,
            timeout=60,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            sub = data.get("subdomain") or data.get("id", "app")
            return {"url": f"https://{sub}.netlify.app", "method": "Netlify"}
    except Exception:
        pass

    # ── Strategy 2: Netlify multipart ─────────────────
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

    # ── Strategy 3: data: URI (always works) ──────────
    single_html = vfs.to_single_html()
    b64 = base64.b64encode(single_html.encode("utf-8")).decode("ascii")
    data_uri = f"data:text/html;base64,{b64}"
    return {"url": data_uri, "method": "data-uri", "html": single_html}


# ─────────────────────────────────────────────
# GITHUB SYNC  (optional)
# ─────────────────────────────────────────────
def push_to_github(vfs: VirtualFS, token: str, repo_name: str) -> str:
    from git import Repo as GitRepo
    import shutil

    tmp = tempfile.mkdtemp(prefix="forge_gh_")
    clone_url = f"https://{token}@github.com/{repo_name}.git"
    git_repo = GitRepo.clone_from(clone_url, tmp)

    branch = "forge-ai"
    try:
        git_repo.git.checkout("-b", branch)
    except Exception:
        git_repo.git.checkout(branch)

    for path, content in vfs.files.items():
        dest = Path(tmp) / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")

    git_repo.git.add(all=True)
    if git_repo.is_dirty(untracked_files=True):
        git_repo.index.commit("⚡ Forge AI — automated update")
        origin = git_repo.remote("origin")
        origin.set_url(clone_url)
        origin.push(refspec=f"{branch}:{branch}")

    shutil.rmtree(tmp, ignore_errors=True)
    return branch


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

    st.session_state.setdefault("selected_file", None)
    st.session_state.setdefault("last_scrape_url", "")


def save_project():
    pass  # all writes go directly to active_proj()


def new_project():
    save_project()
    pid, pdata = _make_project()
    st.session_state["projects"][pid] = pdata
    st.session_state["active_project"] = pid
    st.session_state["selected_file"] = None
    st.session_state["last_scrape_url"] = ""


def switch_project(pid: str):
    save_project()
    st.session_state["active_project"] = pid
    st.session_state["selected_file"] = None
    st.session_state["last_scrape_url"] = ""


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
        st.session_state["selected_file"] = None
        st.session_state["last_scrape_url"] = ""
        st.rerun()

    st.divider()

    with st.expander("☁️ GitHub Sync (optional)"):
        github_token = st.text_input("GitHub Token", type="password", key="gh_token")
        github_repo  = st.text_input("Repo (owner/name)", key="gh_repo", placeholder="you/my-repo")
        if st.button("Push to GitHub", use_container_width=True):
            if not github_token or not github_repo:
                st.error("Token and repo required.")
            elif not vfs.list_files():
                st.error("Nothing to push — build first.")
            else:
                with st.spinner("Pushing…"):
                    try:
                        branch = push_to_github(vfs, github_token, github_repo)
                        st.success(f"Pushed → branch `{branch}`")
                    except Exception as e:
                        st.error(f"Push failed: {e}")


# ─────────────────────────────────────────────
# MAIN AREA
# ─────────────────────────────────────────────
main_tabs = st.tabs(["🏗 Build", "👁 Preview", "📂 Editor", "📜 History"])


# ──────────── TAB 1 : BUILD + CHAT ─────────────────
with main_tabs[0]:

    # ── Chat history ─────────────────────────────────
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

    # ── URL input ─────────────────────────────────────
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

    # ── Prompt + controls ─────────────────────────────
    col1, col2, col3 = st.columns([6, 2, 2])
    with col1:
        prompt = st.text_area(
            "Message",
            height=80,
            placeholder="Clone this site with dark mode… or Build a kanban board with drag-and-drop…",
            key="prompt_input",
            label_visibility="collapsed",
        )
    with col2:
        build_clicked = st.button("⚡ Build", type="primary", use_container_width=True)
    with col3:
        mode = st.radio(
            "Mode", ["✨ New", "✏️ Edit"],
            horizontal=False,
            index=0 if not vfs.list_files() else 1,
        )

    # ── Intent detection ─────────────────────────────
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

    # ── RUN ──────────────────────────────────────────
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

                # ── Scrape if URL provided ─────────────
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

                # ── Build ──────────────────────────────
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

                log_lines = [f"✅ {summary}", ""]
                for a in actions:
                    icon = {"create": "➕", "edit": "✏️", "delete": "🗑"}.get(a.get("type"), "•")
                    log_lines.append(f"{icon}  {a.get('path','?')}")
                active_proj()["build_log"].extend(log_lines)
                active_proj()["history"].append({"role": "user",  "text": user_text})
                active_proj()["history"].append({"role": "agent", "text": summary})

                html = vfs.get_entry_html()
                if html:
                    active_proj()["last_preview_html"] = vfs.inject_css_js(html)

                save_project()
                active_proj()["chat_history"].append(
                    {"role": "assistant", "content": f"✅ {summary} — switch to Preview tab to see it!"}
                )

            else:
                # ── Chat path ──────────────────────────
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

    # ── Build log ─────────────────────────────────────
    if active_proj()["build_log"]:
        with st.expander("Build Log", expanded=False):
            st.markdown(
                '<div class="build-log">'
                + "<br>".join(f"› {l}" for l in active_proj()["build_log"][-60:])
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

        # ── Top bar ──────────────────────────────────
        col_pub, col_dl, col_zip, col_info = st.columns([2, 2, 2, 4])

        with col_pub:
            if st.button("🌐 Publish", type="primary", use_container_width=True,
                         help="Deploy — uses Netlify if available, falls back to a portable data URI"):
                if not vfs.list_files():
                    st.error("Nothing to publish.")
                else:
                    with st.spinner("Publishing…"):
                        try:
                            result_pub = publish_app(vfs)
                            proj["published_url"]    = result_pub["url"]
                            proj["published_method"] = result_pub["method"]
                            save_project()
                            if result_pub["method"] == "data-uri":
                                st.warning(
                                    "Netlify unavailable — your app is packaged as a portable link. "
                                    "Click **Open App** on the right to launch it, or use **Download ZIP** to self-host."
                                )
                            else:
                                st.success(f"Live at [{result_pub['url']}]({result_pub['url']})")
                                st.balloons()
                        except Exception as e:
                            st.error(f"Publish failed: {e}")

        with col_dl:
            st.download_button(
                "⬇️ HTML",
                data=preview_html,
                file_name="index.html",
                mime="text/html",
                use_container_width=True,
            )

        with col_zip:
            if vfs.list_files():
                st.download_button(
                    "📦 ZIP",
                    data=vfs.to_zip_bytes(),
                    file_name="forge-project.zip",
                    mime="application/zip",
                    use_container_width=True,
                    help="All project files as a ZIP — host anywhere",
                )

        with col_info:
            if pub_url:
                if pub_method == "data-uri":
                    st.markdown(
                        f'<a href="{pub_url}" target="_blank" '
                        f'style="display:inline-block;background:linear-gradient(135deg,#7c6af7,#a78bfa);'
                        f'color:#fff;text-decoration:none;border-radius:6px;padding:6px 14px;'
                        f'font-family:Syne,sans-serif;font-weight:700;font-size:13px;">🚀 Open App</a>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f'🌐 <a href="{pub_url}" target="_blank" style="color:#a78bfa;">{pub_url}</a>',
                        unsafe_allow_html=True,
                    )

        st.divider()

        # ── Preview iframe ────────────────────────────
        st.components.v1.html(preview_html, height=700, scrolling=True)


# ──────────── TAB 3 : EDITOR ────────────────
with main_tabs[2]:
    files = vfs.list_files()
    if not files:
        st.info("No files yet.")
    else:
        selected = st.selectbox(
            "File",
            files,
            index=(
                files.index(st.session_state["selected_file"])
                if st.session_state["selected_file"] in files else 0
            ),
            key="editor_file_select",
        )
        st.session_state["selected_file"] = selected

        content = vfs.read(selected) or ""

        edited = st.text_area(
            f"Editing: `{selected}`",
            value=content,
            height=500,
            key=f"editor_{selected}",
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


# ──────────── TAB 4 : HISTORY ───────────────
with main_tabs[3]:
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

