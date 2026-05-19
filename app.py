import os
import json
import time
import base64
import hashlib
import tempfile
import threading
import http.server
import socketserver
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
    """In-memory virtual project filesystem, optionally persisted to a temp dir."""

    def __init__(self):
        if "vfs_files" not in st.session_state:
            st.session_state["vfs_files"] = {}          # path -> content (str)
        if "vfs_root" not in st.session_state:
            st.session_state["vfs_root"] = tempfile.mkdtemp(prefix="forge_")

    # ── read/write ──────────────────────────────
    @property
    def files(self):
        return st.session_state["vfs_files"]

    @property
    def root(self):
        return st.session_state["vfs_root"]

    def write(self, path: str, content: str):
        self.files[path] = content
        disk_path = Path(self.root) / path
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        disk_path.write_text(content, encoding="utf-8")

    def read(self, path: str) -> str | None:
        return self.files.get(path)

    def delete(self, path: str):
        self.files.pop(path, None)
        disk_path = Path(self.root) / path
        if disk_path.exists():
            disk_path.unlink()

    def list_files(self) -> list[str]:
        return sorted(self.files.keys())

    def clear(self):
        st.session_state["vfs_files"] = {}
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


# ─────────────────────────────────────────────
# AGENT CORE
# ─────────────────────────────────────────────
SYSTEM_PROMPT = """You are Forge, an elite autonomous frontend coding AI.

Your job: turn a user prompt into a complete, beautiful, WORKING web app.

Rules:
1. Always return ONLY a valid JSON object — no markdown fences, no extra text.
2. The JSON must have exactly this shape:
{
  "summary": "one-line description of what you built / changed",
  "actions": [
    { "type": "create", "path": "index.html", "content": "..." },
    { "type": "edit",   "path": "style.css",  "content": "..." },
    { "type": "delete", "path": "old.js" }
  ]
}
3. Types: "create" (new file), "edit" (overwrite), "delete" (remove).
4. Build production-quality, visually stunning apps. Use Tailwind CDN, Google Fonts, or vanilla CSS — whatever fits best.
5. Make apps that actually work end-to-end: real interactivity, real data, real UX.
6. For multi-file projects, always produce a self-contained index.html that links to the other files.
7. When editing an existing project, receive the current file tree and produce ONLY the files that change.
"""


def run_agent(ai: AIClient, vfs: VirtualFS, task: str,
              existing_files: dict | None = None) -> dict:
    """
    Run the AI agent.  Returns {"summary": str, "actions": list}.
    """
    # Build user message
    if existing_files:
        file_summary = "\n".join(
            f"[{path}] ({len(content)} chars)"
            for path, content in existing_files.items()
        )
        # Include content of small files for context
        snippets = []
        for path, content in existing_files.items():
            if len(content) < 2000:
                snippets.append(f"### {path}\n```\n{content}\n```")
        file_detail = "\n".join(snippets) if snippets else "(files too large to include)"

        user_msg = (
            f"TASK: {task}\n\n"
            f"CURRENT PROJECT FILES:\n{file_summary}\n\n"
            f"FILE CONTENTS (small files):\n{file_detail}\n\n"
            "Return the JSON with ONLY the files that need to change."
        )
    else:
        user_msg = f"TASK: {task}\n\nBuild this from scratch. Return the complete project JSON."

    raw = ai.ask(SYSTEM_PROMPT, user_msg, max_tokens=8192)

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
def init_state():
    defaults = {
        "vfs": None,
        "history": [],        # list of {"role": "user"|"agent", "text": str}
        "build_log": [],
        "selected_file": None,
        "project_name": "untitled",
        "last_preview_html": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v
    if st.session_state["vfs"] is None:
        st.session_state["vfs"] = VirtualFS()


init_state()
vfs: VirtualFS = st.session_state["vfs"]


# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚡ Forge AI")
    st.caption("Build apps at the speed of thought")
    st.divider()

    # File tree
    st.markdown("**Project Files**")
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
                    if st.session_state["selected_file"] == f:
                        st.session_state["selected_file"] = None
                    st.rerun()

    st.divider()

    # New / Clear project
    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("🗑 Clear", use_container_width=True):
            vfs.clear()
            st.session_state["history"] = []
            st.session_state["build_log"] = []
            st.session_state["selected_file"] = None
            st.session_state["last_preview_html"] = None
            st.rerun()
    with col_b:
        project_name = st.text_input(
            "Project name",
            value=st.session_state["project_name"],
            label_visibility="collapsed",
            placeholder="project-name",
        )
        st.session_state["project_name"] = project_name

    st.divider()

    # GitHub sync (optional)
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


# ──────────── TAB 1 : BUILD ─────────────────
with main_tabs[0]:
    st.markdown("### What should Forge build?")

    prompt = st.text_area(
        "Describe your app",
        height=120,
        placeholder=(
            "e.g. A Pomodoro timer with dark mode, animated progress ring, "
            "and session history stored in localStorage."
        ),
        key="prompt_input",
        label_visibility="collapsed",
    )

    col1, col2 = st.columns([2, 1])
    with col1:
        build_clicked = st.button("⚡ Build / Update", type="primary",
                                  use_container_width=True)
    with col2:
        mode = st.radio("Mode", ["Create from scratch", "Edit existing"],
                        horizontal=True,
                        index=0 if not vfs.list_files() else 1)

    # ── RUN AGENT ──────────────────────────────
    if build_clicked:
        if not AI_KEY:
            st.error("⚠️  Set the `COMPLEX_AI_KEY` environment variable to use Forge AI.")
        elif not prompt.strip():
            st.warning("Please enter a prompt.")
        else:
            ai = AIClient(AI_KEY)

            existing = (
                dict(vfs.files)
                if mode == "Edit existing" and vfs.list_files()
                else None
            )

            log_placeholder = st.empty()
            status_msgs = [
                "🤖  Prompting Forge AI…",
                "✏️  Generating code…",
                "📁  Writing files to virtual FS…",
            ]

            with st.spinner("Forge is building…"):
                log_placeholder.markdown(
                    '<div class="build-log">'
                    + "<br>".join(f"› {m}" for m in status_msgs[:1])
                    + "</div>",
                    unsafe_allow_html=True,
                )
                try:
                    result = run_agent(ai, vfs, prompt.strip(), existing)
                except json.JSONDecodeError as e:
                    st.error(f"AI returned invalid JSON: {e}")
                    st.stop()
                except requests.HTTPError as e:
                    st.error(f"API error: {e}")
                    st.stop()
                except Exception as e:
                    st.error(f"Unexpected error: {e}")
                    st.stop()

            summary = result.get("summary", "Done.")
            actions = result.get("actions", [])

            # Update build log
            log_lines = [f"✅ {summary}", ""]
            for a in actions:
                icon = {"create": "➕", "edit": "✏️", "delete": "🗑"}.get(a.get("type"), "•")
                log_lines.append(f"{icon}  {a.get('path','?')}")

            st.session_state["build_log"].extend(log_lines)
            st.session_state["history"].append({"role": "user",   "text": prompt.strip()})
            st.session_state["history"].append({"role": "agent",  "text": summary})

            # Cache preview HTML
            html = vfs.get_entry_html()
            if html:
                st.session_state["last_preview_html"] = vfs.inject_css_js(html)

            log_placeholder.markdown(
                '<div class="build-log">'
                + "<br>".join(f"› {l}" for l in log_lines)
                + "</div>",
                unsafe_allow_html=True,
            )

            st.success(f"✅ {summary}")
            st.info("Switch to the **Preview** tab to see your app!")

    # Build log (persistent)
    if st.session_state["build_log"]:
        with st.expander("Build Log", expanded=False):
            st.markdown(
                '<div class="build-log">'
                + "<br>".join(f"› {l}" for l in st.session_state["build_log"][-60:])
                + "</div>",
                unsafe_allow_html=True,
            )


# ──────────── TAB 2 : PREVIEW ───────────────
with main_tabs[1]:
    preview_html = st.session_state.get("last_preview_html")

    if not preview_html:
        st.info("Build something first and the live preview will appear here.")
    else:
        # Bump a cache-buster so the iframe reloads on every rerun
        cache_bust = hashlib.md5(preview_html.encode()).hexdigest()[:8]

        st.markdown(
            f"**Live Preview** &nbsp;·&nbsp; "
            f"<span style='font-family:monospace;color:#606080;font-size:12px'>"
            f"hash:{cache_bust}</span>",
            unsafe_allow_html=True,
        )

        # Render directly via srcdoc (works everywhere, no server needed)
        encoded = base64.b64encode(preview_html.encode()).decode()
        iframe_src = f"data:text/html;base64,{encoded}"

        st.components.v1.iframe(iframe_src, height=680, scrolling=True)

        # Download button
        st.download_button(
            "⬇️ Download index.html",
            data=preview_html,
            file_name="index.html",
            mime="text/html",
        )


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
                    st.session_state["last_preview_html"] = vfs.inject_css_js(html)
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
    history = st.session_state["history"]
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
