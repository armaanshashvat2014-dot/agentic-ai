import os
import tempfile
import subprocess
from pathlib import Path

import requests
import streamlit as st
from github import Github
from git import Repo

# =========================
# CONFIG
# =========================
st.set_page_config(page_title="Agentic AI", layout="wide")
st.title("🤖 Agentic AI — Coding + Research Agent")

AI_ENDPOINT = "https://raujzsawwpmixwlcgcgs.supabase.co/functions/v1/public-ai-api"


# =========================
# AI CLIENT
# =========================
class AIClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def ask(self, prompt: str):
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

        try:
            response = requests.post(
                AI_ENDPOINT,
                headers=headers,
                json=payload,
                timeout=120
            )
            response.raise_for_status()
            data = response.json()

            # Flexible response parsing
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

        except Exception as e:
            return f"AI Error: {e}"


# =========================
# GITHUB
# =========================
class GitHubManager:
    def __init__(self, token):
        self.github = Github(token)

    def repos(self):
        return [repo.full_name for repo in self.github.get_user().get_repos()]

    def create_pr(self, repo_name, branch, title):
        repo = self.github.get_repo(repo_name)

        pr = repo.create_pull(
            title=title,
            body="AI-generated update",
            head=branch,
            base=repo.default_branch
        )

        return pr.html_url


# =========================
# REPO AGENT
# =========================
class RepoAgent:
    def __init__(self, github_token):
        self.github_token = github_token

    def clone_repo(self, repo_name):
        temp_dir = tempfile.mkdtemp()

        clone_url = (
            f"https://{self.github_token}@github.com/{repo_name}.git"
        )

        Repo.clone_from(clone_url, temp_dir)
        return temp_dir

    def scan_files(self, repo_path):
        files = []

        for path in Path(repo_path).rglob("*"):
            if path.is_file() and ".git" not in str(path):
                files.append(str(path.relative_to(repo_path)))

        return files

    def read_file(self, repo_path, file_path):
        try:
            return (Path(repo_path) / file_path).read_text(
                encoding="utf-8"
            )
        except:
            return ""

    def create_file(self, repo_path, file_path, content):
        path = Path(repo_path) / file_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def edit_file(self, repo_path, file_path, content):
        path = Path(repo_path) / file_path
        path.write_text(content, encoding="utf-8")

    def delete_file(self, repo_path, file_path):
        path = Path(repo_path) / file_path
        if path.exists():
            path.unlink()

    def create_branch(self, repo_path, branch_name):
        repo = Repo(repo_path)

        try:
            repo.git.checkout("-b", branch_name)
        except:
            repo.git.checkout(branch_name)

    def commit_push(self, repo_path, message):
        repo = Repo(repo_path)

        repo.git.add(all=True)

        if repo.is_dirty(untracked_files=True):
            repo.index.commit(message)
            repo.remote("origin").push()
            return "Pushed successfully"

        return "No changes to push"

    def run_python(self, repo_path):
        try:
            result = subprocess.run(
                ["python", "app.py"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=60
            )

            return (
                result.stdout + "\n" + result.stderr
            )
        except Exception as e:
            return str(e)


# =========================
# CODING AGENT
# =========================
def coding_agent(ai, repo_agent, repo_path, task):
    files = repo_agent.scan_files(repo_path)

    repo_context = "\n".join(files[:200])

    prompt = f"""
You are an autonomous coding and research AI.

Repository files:
{repo_context}

Task:
{task}

Rules:
- Think carefully
- Build production-level code
- Create files if needed
- Modify existing files if needed
- Return ONLY valid JSON

Format:
{{
  "actions": [
    {{
      "type": "create",
      "path": "filename.py",
      "content": "code"
    }},
    {{
      "type": "edit",
      "path": "main.py",
      "content": "updated code"
    }}
  ]
}}
"""

    return ai.ask(prompt)


# =========================
# STREAMLIT SIDEBAR
# =========================
with st.sidebar:
    st.header("🔑 Settings")

    github_token = st.text_input(
        "GitHub Token",
        type="password"
    )

    ai_key = st.text_input(
        "Complex AI API Key",
        type="password",
        help="Starts with mle_"
    )


# =========================
# MAIN
# =========================
if github_token and ai_key:

    gh = GitHubManager(github_token)
    ai = AIClient(ai_key)
    repo_agent = RepoAgent(github_token)

    try:
        repo_names = gh.repos()
    except Exception as e:
        st.error(f"GitHub error: {e}")
        st.stop()

    selected_repo = st.selectbox(
        "Select Repository",
        repo_names
    )

    task = st.text_area(
        "What should the AI build?",
        placeholder="Build a login system with XP leaderboard"
    )

    col1, col2, col3 = st.columns(3)

    with col1:
        clone_btn = st.button("📥 Clone Repo")

    with col2:
        analyze_btn = st.button("🧠 Analyze")

    with col3:
        build_btn = st.button("🚀 Run Agent")

    if clone_btn:
        with st.spinner("Cloning repo..."):
            repo_path = repo_agent.clone_repo(selected_repo)
            st.session_state["repo_path"] = repo_path

        st.success("Repo cloned")

    if analyze_btn:
        if "repo_path" not in st.session_state:
            st.error("Clone repo first")
        else:
            files = repo_agent.scan_files(
                st.session_state["repo_path"]
            )

            st.subheader("Files")
            st.code("\n".join(files[:300]))

    if build_btn:
        if "repo_path" not in st.session_state:
            st.error("Clone repo first")
            st.stop()

        repo_path = st.session_state["repo_path"]

        branch_name = "ai-agent-update"
        repo_agent.create_branch(
            repo_path,
            branch_name
        )

        with st.spinner("AI thinking..."):
            result = coding_agent(
                ai,
                repo_agent,
                repo_path,
                task
            )

        st.subheader("AI Response")
        st.code(result)

        st.info(
            "Next step: make the AI output JSON actions so we can auto-create/edit files."
        )

        if st.button("📤 Commit + Push"):
            status = repo_agent.commit_push(
                repo_path,
                "AI Agent Update"
            )

            st.success(status)

            try:
                pr_url = gh.create_pr(
                    selected_repo,
                    branch_name,
                    "AI Agent Update"
                )

                st.success("PR Created")
                st.write(pr_url)
            except Exception as e:
                st.error(e)

else:
    st.info(
        "Add GitHub token and Complex AI key"
    )

# =========================
# INSTALL
# pip install streamlit requests pygithub gitpython
# RUN
# streamlit run app.py
# =========================
