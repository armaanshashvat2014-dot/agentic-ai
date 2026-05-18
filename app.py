import os
import json
import tempfile
from pathlib import Path

import requests
import streamlit as st
from github import Github
from git import Repo

# ======================
# CONFIG
# ======================
st.set_page_config(page_title="Agentic AI", layout="wide")
st.title("🤖 Agentic AI")
st.caption("One-click autonomous coding agent")

AI_ENDPOINT = "https://raujzsawwpmixwlcgcgs.supabase.co/functions/v1/public-ai-api"
AI_KEY = os.getenv("COMPLEX_AI_KEY", "")


# ======================
# AI CLIENT
# ======================
class AIClient:
    def __init__(self, api_key):
        self.api_key = api_key

    def ask(self, prompt):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        }

        response = requests.post(
            AI_ENDPOINT,
            headers=headers,
            json=payload,
            timeout=120
        )

        data = response.json()

        # Flexible parsing
        if isinstance(data, dict):
            if "response" in data:
                return data["response"]

            if "content" in data:
                return data["content"]

            if "message" in data:
                return data["message"]

            if "choices" in data:
                return data["choices"][0]["message"]["content"]

        return str(data)


# ======================
# REPO AGENT
# ======================
class RepoAgent:
    def __init__(self, token):
        self.token = token

    def clone_repo(self, repo_name):
        temp_dir = tempfile.mkdtemp()

        clone_url = (
            f"https://{self.token}@github.com/{repo_name}.git"
        )

        Repo.clone_from(clone_url, temp_dir)
        return temp_dir

    def scan_repo(self, repo_path):
        files = []

        for path in Path(repo_path).rglob("*"):
            if path.is_file() and ".git" not in str(path):
                files.append(str(path.relative_to(repo_path)))

        return files[:200]

    def create_file(self, repo_path, file_path, content):
        path = Path(repo_path) / file_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def edit_file(self, repo_path, file_path, content):
        path = Path(repo_path) / file_path
        path.write_text(content, encoding="utf-8")

    def commit_push(self, repo_path):
        repo = Repo(repo_path)

        branch = "ai-update"

        try:
            repo.git.checkout("-b", branch)
        except:
            repo.git.checkout(branch)

        repo.git.add(all=True)

        if repo.is_dirty(untracked_files=True):
            repo.index.commit("AI Agent Update")
            repo.remote("origin").push(branch)

        return branch


# ======================
# AGENT
# ======================
def run_agent(ai, repo_agent, repo_path, task):
    files = repo_agent.scan_repo(repo_path)

    context = "\n".join(files)

    prompt = f"""
You are an autonomous coding AI.

Task:
{task}

Repository files:
{context}

Return ONLY valid JSON.

Example:
{{
  "actions": [
    {{
      "type": "create",
      "path": "index.html",
      "content": "<h1>Hello</h1>"
    }},
    {{
      "type": "edit",
      "path": "app.py",
      "content": "print('updated')"
    }}
  ]
}}
"""

    raw = ai.ask(prompt)

    try:
        parsed = json.loads(raw)
    except:
        st.error("AI did not return valid JSON")
        st.code(raw)
        return None

    preview_html = None

    for action in parsed.get("actions", []):
        action_type = action.get("type")
        path = action.get("path")
        content = action.get("content", "")

        if action_type == "create":
            repo_agent.create_file(
                repo_path,
                path,
                content
            )

        elif action_type == "edit":
            repo_agent.edit_file(
                repo_path,
                path,
                content
            )

        if path and path.endswith(".html"):
            preview_html = content

    return preview_html


# ======================
# UI
# ======================
with st.sidebar:
    github_token = st.text_input(
        "GitHub Token",
        type="password"
    )

if github_token:
    gh = Github(github_token)

    repos = [
        repo.full_name
        for repo in gh.get_user().get_repos()
    ]

    selected_repo = st.selectbox(
        "Repository",
        repos
    )

    task = st.text_area(
        "What should AI build?",
        placeholder="Build a beautiful leaderboard app"
    )

    if st.button("🚀 Build"):
        if not AI_KEY:
            st.error(
                "Add COMPLEX_AI_KEY in Streamlit secrets/environment variables"
            )
            st.stop()

        ai = AIClient(AI_KEY)
        repo_agent = RepoAgent(github_token)

        with st.spinner("Cloning repo..."):
            repo_path = repo_agent.clone_repo(selected_repo)

        with st.spinner("AI building..."):
            preview = run_agent(
                ai,
                repo_agent,
                repo_path,
                task
            )

        with st.spinner("Committing and pushing..."):
            branch = repo_agent.commit_push(repo_path)

        st.success("Done! 🚀")
        st.write(f"Branch: {branch}")

        if preview:
            st.subheader("🔍 Preview")
            st.components.v1.html(
                preview,
                height=600,
                scrolling=True
            )

else:
    st.info("Add GitHub Token")

# requirements.txt
# streamlit
# requests
# PyGithub
# GitPython
