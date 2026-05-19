import os
import json
import time
import base64
import hashlib
import tempfile
import threading
import http.server
import socketserver
import uuid
import zipfile
import io
from pathlib import Path

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

PREVIEW_PORT = 8765   # local HTTP server for live preview


# ─────────────────────────────────────────────
# VIRTUAL FILESYSTEM  (in-memory + disk-backed)
# ─────────────────────────────────────────────
class VirtualFS:
    """
    Virtual filesystem that reads/writes DIRECTLY into the active project dict.
    No intermediate session_state copies — eliminates the stale-reference bug.
    """

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

    # ── preview helpers ─────────────────────────
    def get_entry_html(self) -> str | None:
        """Return the best entry-point HTML file."""
        for candidate in ("index.html", "app.html", "main.html"):
            if candidate in self.files:
                return self.files[candidate]
        for path, content in self.files.items():
            if path.endswith(".html"):
                return content
        return None

    def inject_css_js(self, html: str) -> str:
        """Inline all referenced local CSS and JS files into the HTML."""
        import re

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


# ─────────────────────────────────────────────
# LIVE PREVIEW SERVER  (one per session via thread)
# ─────────────────────────────────────────────
_server_lock = threading.Lock()

def ensure_preview_server(vfs: VirtualFS):
    """Start a simple file-serving HTTP server if not already running."""
    if st.session_state.get("preview_server_started"):
        return

    root = vfs.root

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=root, **kwargs)

        def log_message(self, *args):
            pass   # suppress server logs

    with _server_lock:
        if not st.session_state.get("preview_server_started"):
            try:
                httpd = socketserver.TCPServer(("", PREVIEW_PORT), Handler)
                httpd.allow_reuse_address = True
                t = threading.Thread(target=httpd.serve_forever, daemon=True)
                t.start()
                st.session_state["preview_server_started"] = True
                st.session_state["preview_httpd"] = httpd
            except OSError:
                # Port already in use – that's fine, server already running
                st.session_state["preview_server_started"] = True


# ─────────────────────────────────────────────
# AI CLIENT
# ─────────────────────────────────────────────
class AIClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def ask(self, system: str, user: str, max_tokens: int = 4096) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "messages": [
                {"role": "user", "content": f"{system}\n\n{user}"}
            ]
        }
        resp = requests.post(AI_ENDPOINT, headers=headers,
                             json=payload, timeout=180)
        resp.raise_for_status()
        data = resp.json()

        # Flexible response parsing (matches original AIClient)
        if isinstance(data, dict):
            if "response" in data:
                return data["response"]
            if "content" in data:
                cnt = data["content"]
                if isinstance(cnt, list):
                    return cnt[0].get("text", str(cnt))
                return cnt
            if "message" in data:
                return data["message"]
            if "choices" in data:
                return data["choices"][0]["message"]["content"]
        return str(data)

    def chat(self, conversation: list[dict]) -> str:
        """Send a multi-turn chat conversation, prepending the chat system prompt."""
        # Prepend system prompt as a strong first user turn + assistant ack
        full_messages = [
            {"role": "user",      "content": CHAT_SYSTEM_PROMPT + "\n\nAcknowledge these rules briefly."},
            {"role": "assistant", "content": "Understood. I am Forge AI, your web app building assistant. I only help with coding, building, and web development. What would you like to build?"},
        ] + conversation

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {"messages": full_messages}
        resp = requests.post(AI_ENDPOINT, headers=headers,
                             json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, dict):
            if "response" in data:
                return data["response"]
            if "content" in data:
                cnt = data["content"]
                if isinstance(cnt, list):
                    return cnt[0].get("text", str(cnt))
                return cnt
            if "message" in data:
                return data["message"]
            if "choices" in data:
                return data["choices"][0]["message"]["content"]
        return str(data)


# ─────────────────────────────────────────────
# AGENT CORE
# ─────────────────────────────────────────────
# System prompts for different modes
AGENT_SYSTEM_PROMPT = """IMPORTANT: You are Forge, an autonomous web app builder. You output ONLY raw JSON — no prose, no markdown, no explanations, no web searches.

TASK: Make exactly the change the user requests. Nothing more.

OUTPUT FORMAT — return this exact JSON shape and nothing else:
{
  "summary": "one-line description of what you changed",
  "actions": [
    { "type": "create", "path": "index.html", "content": "..." },
    { "type": "edit",   "path": "style.css",  "content": "..." },
    { "type": "delete", "path": "old.js" }
  ]
}

RULES:
- "create" = new file, "edit" = full replacement content, "delete" = remove.
- PRESERVATION: You receive the COMPLETE current file contents. Copy ALL existing content into "content", then apply only the requested change on top. NEVER remove features, functions, styles, or text unless explicitly asked.
- Only include files that actually change. Unchanged files: omit entirely.
- Build production-quality, visually stunning apps. Tailwind CDN, Google Fonts, or vanilla CSS.
- Apps must be fully interactive and functional end-to-end.
- Do NOT search the web. Do NOT return prose. Return JSON only.

CRITICAL RULE FOR AI-POWERED APPS:
If the app you are building contains a chat interface or calls an AI API, you MUST follow this pattern exactly:

The app calls this endpoint:
  POST https://raujzsawwpmixwlcgcgs.supabase.co/functions/v1/public-ai-api
  Headers: { "Authorization": "Bearer FORGE_AI_KEY_PLACEHOLDER", "Content-Type": "application/json" }

The request body MUST always include a strong system prompt prepended as the first user message, followed by an assistant acknowledgement, then the real conversation. Example:

  const SYSTEM_PROMPT = "You are [AppName] AI. You are a helpful assistant for [purpose]. STRICT RULES: 1) Never perform web searches or return search results. 2) Only answer questions relevant to [purpose]. 3) If asked off-topic questions, politely redirect to [purpose]. 4) Keep answers concise and helpful.";

  const messages = [
    { role: "user", content: SYSTEM_PROMPT + "\\n\\nAcknowledge your role briefly." },
    { role: "assistant", content: "Understood. I am [AppName] AI, ready to help with [purpose]." },
    ...conversationHistory,
    { role: "user", content: userInput }
  ];

The key FORGE_AI_KEY_PLACEHOLDER will be injected at runtime — always use exactly that string as the Bearer token.
Never use fetch with no system prompt. Always anchor the AI's persona this way."""

CHAT_SYSTEM_PROMPT = """You are Forge AI, a friendly assistant built into a web app builder called Forge.
Your personality: helpful, concise, focused on coding and building web apps.

STRICT RULES:
- You are NOT a search engine. NEVER perform web searches or return search results.
- ONLY answer questions related to: web development, app ideas, how to use Forge, coding help, UI/UX advice.
- If someone asks something off-topic (news, general knowledge, trivia, etc.), politely redirect them: explain you are a coding assistant and ask what they'd like to build.
- Keep answers short and practical. Use plain text, no markdown headers.
- If the user describes an app idea, ask one clarifying question to help them refine it, then encourage them to hit Build."""

# Keep backward-compat alias
SYSTEM_PROMPT = AGENT_SYSTEM_PROMPT


def run_agent(ai: AIClient, vfs: VirtualFS, task: str,
              existing_files: dict | None = None) -> dict:
    """
    Run the AI agent.  Returns {"summary": str, "actions": list}.
    """
    if existing_files:
        # Always send FULL content of every file so AI can preserve it correctly
        snippets = []
        for path, content in existing_files.items():
            snippets.append(f"### {path}\n```\n{content}\n```")
        file_detail = "\n\n".join(snippets)

        user_msg = (
            f"TASK: {task}\n\n"
            f"CURRENT PROJECT FILES (complete content — preserve everything not mentioned):\n\n{file_detail}\n\n"
            "Return ONLY the files that need to change. "
            "For each changed file, include its FULL new content (existing content + your edits). "
            "Do NOT include files that are unchanged."
        )
    else:
        user_msg = f"TASK: {task}\n\nBuild this from scratch. Return the complete project JSON."

    raw = ai.ask(AGENT_SYSTEM_PROMPT, user_msg, max_tokens=8192)

    # Strip accidental markdown fences
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    parsed = json.loads(raw)

    # Apply actions to VFS
    for action in parsed.get("actions", []):
        t = action.get("type")
        path = action.get("path", "").lstrip("/")
        content = action.get("content", "")
        # Inject the real API key into generated apps that use the placeholder
        if isinstance(content, str):
            content = content.replace("FORGE_AI_KEY_PLACEHOLDER", AI_KEY)
        if t in ("create", "edit"):
            vfs.write(path, content)
        elif t == "delete":
            vfs.delete(path)

    return parsed


# ─────────────────────────────────────────────
# GITHUB SYNC  (optional, deferred)
# ─────────────────────────────────────────────
def push_to_github(vfs: VirtualFS, token: str, repo_name: str) -> str:
    """Push the virtual FS to a GitHub branch.  Returns branch name."""
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

    # Copy VFS files into clone
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

h1, h2, h3 {
    font-family: 'Syne', sans-serif !important;
    letter-spacing: -0.02em;
}

code, pre, .stCode {
    font-family: 'IBM Plex Mono', monospace !important;
    font-size: 12px !important;
}

/* Input fields */
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

/* Primary buttons */
.stButton > button[kind="primary"], .stButton > button:first-child {
    background: linear-gradient(135deg, #7c6af7, #a78bfa) !important;
    color: #fff !important;
    border: none !important;
    border-radius: 6px !important;
    font-family: 'Syne', sans-serif !important;
    font-weight: 700 !important;
    letter-spacing: 0.04em !important;
    padding: 0.5rem 1.5rem !important;
    transition: all 0.2s ease !important;
}
.stButton > button:hover {
    transform: translateY(-1px) !important;
    box-shadow: 0 4px 20px rgba(124,106,247,0.4) !important;
}

/* Secondary / outline buttons */
.stButton > button[kind="secondary"] {
    background: transparent !important;
    border: 1px solid #2a2a3e !important;
    color: #a0a0c0 !important;
    border-radius: 6px !important;
    font-family: 'IBM Plex Mono', monospace !important;
}

/* Expander */
.streamlit-expanderHeader {
    background: #12121c !important;
    border: 1px solid #1e1e2e !important;
    border-radius: 6px !important;
    font-family: 'IBM Plex Mono', monospace !important;
    color: #a0a0c0 !important;
}

/* Success / info / warning */
.stAlert {
    border-radius: 6px !important;
    font-family: 'IBM Plex Mono', monospace !important;
    font-size: 13px !important;
}

/* Tab bar */
.stTabs [role="tablist"] {
    gap: 4px;
    border-bottom: 1px solid #1e1e2e;
}
.stTabs [role="tab"] {
    background: transparent !important;
    color: #606080 !important;
    border: none !important;
    font-family: 'IBM Plex Mono', monospace !important;
    font-size: 12px !important;
    padding: 6px 14px !important;
}
.stTabs [role="tab"][aria-selected="true"] {
    color: #a78bfa !important;
    border-bottom: 2px solid #7c6af7 !important;
}

/* File tree items */
.file-item {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    color: #a0a0c0;
    padding: 4px 8px;
    border-radius: 4px;
    cursor: pointer;
    transition: background 0.15s;
}
.file-item:hover { background: #1e1e2e; color: #e8e6e3; }

/* Build log */
.build-log {
    background: #0d0d14;
    border: 1px solid #1e1e2e;
    border-radius: 8px;
    padding: 12px 16px;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    color: #7c6af7;
    max-height: 200px;
    overflow-y: auto;
}

/* Divider */
hr { border-color: #1e1e2e !important; }

/* Caption */
.stCaption { color: #606080 !important; }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────
# SESSION STATE INIT
# ─────────────────────────────────────────────
def active_proj() -> dict:
    """Return the active project dict directly — single source of truth."""
    ap = st.session_state["active_project"]
    return st.session_state["projects"][ap]


def init_state():
    if "projects" not in st.session_state:
        first_id = str(uuid.uuid4())[:8]
        st.session_state["projects"] = {
            first_id: {
                "name": "untitled",
                "chat_history": [],
                "build_log": [],
                "history": [],
                "last_preview_html": None,
                "vfs_files": {},
                "vfs_root": tempfile.mkdtemp(prefix=f"forge_{first_id}_"),
                "published_url": None,
            }
        }
        st.session_state["active_project"] = first_id

    if "active_project" not in st.session_state:
        st.session_state["active_project"] = list(st.session_state["projects"].keys())[0]

    # Ensure every project has a vfs_root on disk
    for pid, pdata in st.session_state["projects"].items():
        if "vfs_root" not in pdata or not Path(pdata["vfs_root"]).exists():
            pdata["vfs_root"] = tempfile.mkdtemp(prefix=f"forge_{pid}_")

    if "selected_file" not in st.session_state:
        st.session_state["selected_file"] = None
    # VirtualFS is now stateless — no need to store it in session_state


def save_project():
    """No-op: all writes go directly to active_proj() dict now."""
    pass


def new_project():
    """Create a brand-new project and switch to it."""
    save_project()
    pid = str(uuid.uuid4())[:8]
    st.session_state["projects"][pid] = {
        "name": "untitled",
        "chat_history": [],
        "build_log": [],
        "history": [],
        "last_preview_html": None,
        "vfs_files": {},
        "published_url": None,
    }
    st.session_state["active_project"] = pid
    st.session_state["selected_file"] = None
    st.session_state["_last_ap"] = None   # force VFS re-init


def switch_project(pid: str):
    save_project()
    st.session_state["active_project"] = pid
    st.session_state["selected_file"] = None
    st.session_state["_last_ap"] = None


init_state()
vfs = VirtualFS()  # stateless — all data lives in active_proj()


# ─────────────────────────────────────────────
# PUBLISH HELPER  (Netlify Drop — no login needed)
# ─────────────────────────────────────────────
def publish_to_netlify(vfs: VirtualFS) -> str:
    """
    Zip all VFS files and deploy to Netlify Drop.
    Returns the live URL.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in vfs.files.items():
            zf.writestr(path, content)
    buf.seek(0)

    resp = requests.post(
        "https://api.netlify.com/api/v1/sites",
        headers={"Content-Type": "application/zip"},
        data=buf.read(),
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return f"https://{data['subdomain']}.netlify.app"


# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚡ Forge AI")
    st.caption("Build apps at the speed of thought")

    # ── New Chat button ───────────────────────
    if st.button("✦ New Chat", type="primary", use_container_width=True):
        new_project()
        st.rerun()

    st.divider()

    # ── Project list ──────────────────────────
    st.markdown("**Chats**")
    ap = st.session_state["active_project"]
    for pid, pdata in st.session_state["projects"].items():
        is_active = pid == ap
        label = pdata["name"]
        pub = pdata.get("published_url")
        display = f"{'▶ ' if is_active else ''}{label}"
        col_p, col_del = st.columns([5, 1])
        with col_p:
            btn_style = "primary" if is_active else "secondary"
            if st.button(display, key=f"proj_{pid}", use_container_width=True,
                         type=btn_style if is_active else "secondary"):
                if not is_active:
                    switch_project(pid)
                    st.rerun()
        with col_del:
            if len(st.session_state["projects"]) > 1:
                if st.button("✕", key=f"projdel_{pid}"):
                    save_project()
                    del st.session_state["projects"][pid]
                    remaining = list(st.session_state["projects"].keys())
                    st.session_state["active_project"] = remaining[0]
                    st.session_state["_last_ap"] = None
                    st.rerun()
        if pub:
            st.markdown(
                f'<a href="{pub}" target="_blank" style="font-family:IBM Plex Mono,monospace;'
                f'font-size:10px;color:#7c6af7;text-decoration:none;">🌐 {pub.replace("https://","")}</a>',
                unsafe_allow_html=True,
            )

    st.divider()

    # ── Active project name ───────────────────
    proj_name = st.text_input(
        "Project name",
        value=st.session_state["projects"][ap]["name"],
        label_visibility="collapsed",
        placeholder="project-name",
    )
    if proj_name != st.session_state["projects"][ap]["name"]:
        st.session_state["projects"][ap]["name"] = proj_name
        st.rerun()

    # ── File tree ─────────────────────────────
    st.markdown("**Files**")
    file_list = vfs.list_files()
    if not file_list:
        st.caption("No files yet — build something!")
    else:
        for f in file_list:
            col1, col2 = st.columns([5, 1])
            with col1:
                if st.button(f"📄 {f}", key=f"file_{f}",
                             use_container_width=True):
                    st.session_state["selected_file"] = f
            with col2:
                if st.button("✕", key=f"del_{f}"):
                    vfs.delete(f)
                    save_project()
                    if st.session_state["selected_file"] == f:
                        st.session_state["selected_file"] = None
                    st.rerun()

    st.divider()

    # ── Clear current project ─────────────────
    if st.button("🗑 Clear project", use_container_width=True):
        vfs.clear()
        active_proj()["history"] = []
        active_proj()["chat_history"] = []
        active_proj()["build_log"] = []
        active_proj()["last_preview_html"] = None
        active_proj()["published_url"] = None
        st.session_state["selected_file"] = None
        st.rerun()

    st.divider()

    # ── GitHub sync (optional) ────────────────
    with st.expander("☁️ GitHub Sync (optional)"):
        github_token = st.text_input("GitHub Token", type="password",
                                     key="gh_token")
        github_repo  = st.text_input("Repo (owner/name)", key="gh_repo",
                                     placeholder="you/my-repo")
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

    # ── Chat history display ──────────────────────────
    chat_container = st.container()
    with chat_container:
        if not active_proj()["chat_history"]:
            st.markdown(
                '''<div style="text-align:center;padding:40px 0 20px;
                    font-family:IBM Plex Mono,monospace;color:#606080;font-size:13px;">
                    ⚡ Tell Forge what to build — or ask anything about web dev.
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

    # ── Input row ────────────────────────────────────
    col1, col2, col3 = st.columns([6, 2, 2])
    with col1:
        prompt = st.text_area(
            "Message",
            height=80,
            placeholder="Build a todo app… or ask a question…",
            key="prompt_input",
            label_visibility="collapsed",
        )
    with col2:
        build_clicked = st.button("⚡ Build", type="primary",
                                  use_container_width=True)
    with col3:
        mode = st.radio("Mode", ["✨ New", "✏️ Edit"],
                        horizontal=False,
                        index=0 if not vfs.list_files() else 1)

    # ── Detect intent: build vs chat ────────────────
    BUILD_VERBS = [
        "build", "create", "make", "add", "generate", "write", "update",
        "change", "fix", "edit", "remove", "delete", "refactor", "style",
        "implement", "deploy", "convert", "migrate", "improve", "redesign",
        "dark mode", "light mode", "responsive", "mobile", "feature",
    ]

    def is_build_intent(text: str) -> bool:
        t = text.lower()
        return any(v in t for v in BUILD_VERBS)

    # ── RUN ─────────────────────────────────────────
    if build_clicked:
        if not AI_KEY:
            st.error("⚠️  Set the `COMPLEX_AI_KEY` environment variable to use Forge AI.")
        elif not prompt.strip():
            st.warning("Please enter a message.")
        else:
            ai = AIClient(AI_KEY)
            user_text = prompt.strip()

            # Add user message to chat
            active_proj()["chat_history"].append(
                {"role": "user", "content": user_text}
            )

            if is_build_intent(user_text) or "✨ New" in mode:
                # ── BUILD PATH ──
                existing = (
                    dict(vfs.files)
                    if "✏️ Edit" in mode and vfs.list_files()
                    else None
                )

                with st.spinner("⚡ Forge is building…"):
                    try:
                        result = run_agent(ai, vfs, user_text, existing)
                    except json.JSONDecodeError as e:
                        err = f"AI returned invalid JSON — try rephrasing. ({e})"
                        active_proj()["chat_history"].append(
                            {"role": "assistant", "content": err}
                        )
                        st.rerun()
                    except Exception as e:
                        err = f"Error: {e}"
                        active_proj()["chat_history"].append(
                            {"role": "assistant", "content": err}
                        )
                        st.rerun()

                summary = result.get("summary", "Done.")
                actions = result.get("actions", [])

                # Build log
                log_lines = [f"✅ {summary}", ""]
                for a in actions:
                    icon = {"create": "➕", "edit": "✏️", "delete": "🗑"}.get(a.get("type"), "•")
                    log_lines.append(f"{icon}  {a.get('path','?')}")
                active_proj()["build_log"].extend(log_lines)
                active_proj()["history"].append({"role": "user",  "text": user_text})
                active_proj()["history"].append({"role": "agent", "text": summary})

                # Refresh preview
               html = vfs.get_entry_html()

if html:
    rendered_html = vfs.inject_css_js(html)

    # save preview into project
    active_proj()["last_preview_html"] = rendered_html

    # force refresh flag
    st.session_state["preview_ready"] = True

save_project()

reply = "✅ Build complete — preview updated!"

active_proj()["chat_history"].append(
    {
        "role": "assistant",
        "content": reply
    }
)

            else:
                # ── CHAT PATH ──
                # Build conversation list for the AI (role: user/assistant only)
                conversation = [
                    {"role": m["role"], "content": m["content"]}
                    for m in active_proj()["chat_history"]
                ]
                with st.spinner("Thinking…"):
                    try:
                        reply = ai.chat(conversation)
                    except Exception as e:
                        reply = f"Sorry, I hit an error: {e}"

                active_proj()["chat_history"].append(
                    {"role": "assistant", "content": reply}
                )

            st.rerun()

    # Build log (persistent)
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
    preview_html = st.session_state.get("last_preview_html")

    if not preview_html:
        st.info("Build something first and the live preview will appear here.")
    else:
        ap   = st.session_state["active_project"]
        proj = st.session_state["projects"][ap]

        # ── Top bar: publish + download ──────────────
        pub_url = proj.get("published_url")
        col_pub, col_dl, col_info = st.columns([2, 2, 4])

        with col_pub:
            if st.button("🌐 Publish", type="primary", use_container_width=True,
                         help="Deploy to a free random Netlify URL instantly"):
                if not vfs.list_files():
                    st.error("Nothing to publish.")
                else:
                    with st.spinner("Publishing to Netlify…"):
                        try:
                            url = publish_to_netlify(vfs)
                            proj["published_url"] = url
                            save_project()
                            st.success(f"Live at [{url}]({url})")
                            st.balloons()
                        except Exception as e:
                            st.error(f"Publish failed: {e}")
        with col_dl:
            st.download_button(
                "⬇️ Download",
                data=preview_html,
                file_name="index.html",
                mime="text/html",
                use_container_width=True,
            )
        with col_info:
            if pub_url:
                st.markdown(
                    f'🌐 Published: <a href="{pub_url}" target="_blank" '
                    f'style="color:#a78bfa;">{pub_url}</a>',
                    unsafe_allow_html=True,
                )

        st.divider()

       # ── Preview ───────────────────────────
preview_html = active_proj().get("last_preview_html")

if preview_html:
    st.components.v1.html(
        preview_html,
        height=700,
        scrolling=True
    )
else:
    st.warning("No preview available yet.")


# ──────────── TAB 3 : EDITOR ────────────────
with main_tabs[2]:
    files = vfs.list_files()
    if not files:
        st.info("No files yet.")
    else:
        # File selector
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
        ext = Path(selected).suffix.lower()
        lang_map = {
            ".html": "html", ".css": "css", ".js": "javascript",
            ".ts": "typescript", ".py": "python", ".json": "json",
            ".md": "markdown",
        }
        lang = lang_map.get(ext, "text")

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
                # Refresh preview
                html = vfs.get_entry_html()
                if html:
                    active_proj()["last_preview_html"] = vfs.inject_css_js(html)
                st.success("Saved!")
                st.rerun()
        with col_revert:
            if st.button("↩ Revert", use_container_width=True):
                st.rerun()   # just re-render from VFS

        # New file creation
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
                f"""
                <div style="
                    background:{color};
                    border-radius:8px;
                    padding:10px 14px;
                    margin:6px 0;
                    font-family:'IBM Plex Mono',monospace;
                    font-size:13px;
                    color:#e8e6e3;
                    text-align:{align};
                ">
                {icon}&nbsp;&nbsp;{text}
                </div>
                """,
                unsafe_allow_html=True,
            )
