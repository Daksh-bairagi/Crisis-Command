from fastmcp import FastMCP
import json
import re
import requests
import sys
import os
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT_DIR)

from logger import get_mcp_logger

log = get_mcp_logger("github_mcp_server")
load_dotenv(os.path.join(ROOT_DIR, ".env"))

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")

# Parse owner/repo from GITHUB_REPO env var
if "/" in GITHUB_REPO:
    _owner, _repo = GITHUB_REPO.split("/", 1)
else:
    if GITHUB_REPO:  # set but malformed
        log.warning("GITHUB_REPO is set but missing '/': %s — falling back to demo mode", GITHUB_REPO)
    _owner, _repo = "", ""

GITHUB_API_BASE = "https://api.github.com"

mcp = FastMCP("github-server")


def _github_headers() -> dict:
    """Return headers for GitHub API requests."""
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }


def _credentials_available() -> bool:
    """Return True if both GITHUB_TOKEN and GITHUB_REPO are configured."""
    return bool(GITHUB_TOKEN and _owner and _repo)


# ---------------------------------------------------------------------------
# Demo data
# ---------------------------------------------------------------------------

_DEMO_DEPLOYMENT_INFO = {
    "success": True,
    "deployment_id": "deploy-447",
    "pr_title": "Optimize DB connection pool for cost reduction",
    "pr_number": 234,
    "author": "john.smith",
    "commit_sha": "a3f2b1c",
    "commit_message": "feat: reduce DB_POOL_SIZE to lower connection costs (#234)",
    "files_changed": ["config/payments.yaml", "helm/payments/values.yaml"],
    "diff_summary": (
        "Reduced DB_POOL_SIZE from 50 to 10 in payments service config. "
        "Updated Helm values accordingly."
    ),
    "demo_mode": True,
}

_DEMO_RECENT_COMMITS = {
    "success": True,
    "service_name": "payments-service",
    "hours_back": 4,
    "commits": [
        {
            "sha": "a3f2b1c",
            "message": "feat: reduce DB_POOL_SIZE to lower connection costs (#234)",
            "author": "john.smith",
            "timestamp": (datetime.now(timezone.utc) - timedelta(hours=1, minutes=23)).isoformat(),
            "files_changed": ["config/payments.yaml", "helm/payments/values.yaml"],
        },
        {
            "sha": "d9e4c5f",
            "message": "chore: bump payments-service image to v2.14.1",
            "author": "ci-bot",
            "timestamp": (datetime.now(timezone.utc) - timedelta(hours=2, minutes=45)).isoformat(),
            "files_changed": ["helm/payments/Chart.yaml"],
        },
        {
            "sha": "b7a1d2e",
            "message": "fix: correct readiness probe path for payments health endpoint",
            "author": "alice.chen",
            "timestamp": (datetime.now(timezone.utc) - timedelta(hours=3, minutes=10)).isoformat(),
            "files_changed": ["helm/payments/values.yaml", "k8s/payments-deployment.yaml"],
        },
    ],
    "demo_mode": True,
}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_deployment_info(deployment_id: str) -> dict:
    """
    Retrieve details about a specific deployment from GitHub, including the PR title,
    author, files changed, and a diff summary.

    Use this tool when an alert references a deployment ID (e.g., 'last_deployment:
    deploy-447') and you need to identify what code changed in that deployment — the
    root cause of a production incident often lives in the changed files.

    Args:
        deployment_id: Deployment identifier string (e.g., 'deploy-447').

    Returns:
        {
            "success": bool,
            "deployment_id": str,
            "pr_title": str,
            "pr_number": int,
            "author": str,
            "commit_sha": str,
            "commit_message": str,
            "files_changed": list[str],
            "diff_summary": str,
            "demo_mode": bool   # True when running without real credentials
        }
        On failure: {"success": False, "error": str}
    """
    if not _credentials_available():
        log.info(f"Demo mode: returning demo deployment info for {deployment_id}")
        result = dict(_DEMO_DEPLOYMENT_INFO)
        result["deployment_id"] = deployment_id
        return result

    try:
        log.info(f"Fetching deployment info for {deployment_id} from {_owner}/{_repo}")

        headers = _github_headers()
        # Search recent commits for one whose message references the deployment_id
        commits_url = f"{GITHUB_API_BASE}/repos/{_owner}/{_repo}/commits"
        resp = requests.get(commits_url, headers=headers, params={"per_page": 30}, timeout=10)
        resp.raise_for_status()
        commits = resp.json()

        target_commit = None
        for commit in commits:
            msg = commit.get("commit", {}).get("message", "")
            ref = commit.get("ref", "") or ""
            if deployment_id in msg or deployment_id in ref:
                target_commit = commit
                break

        if target_commit is None:
            # Fall back to the most recent commit with a note
            log.warning(f"No commit found matching {deployment_id}; using most recent")
            target_commit = commits[0] if commits else None

        if target_commit is None:
            return {"success": False, "error": f"No commits found in {_owner}/{_repo}"}

        sha = target_commit["sha"]

        # Fetch full commit detail to get files changed
        detail_resp = requests.get(
            f"{GITHUB_API_BASE}/repos/{_owner}/{_repo}/commits/{sha}",
            headers=headers,
            timeout=10,
        )
        detail_resp.raise_for_status()
        detail = detail_resp.json()

        files = detail.get("files", [])
        files_changed = [f["filename"] for f in files]

        # Build a simple diff summary from patch snippets
        patch_lines = []
        for f in files[:5]:  # cap at 5 files to keep the summary readable
            patch = f.get("patch", "")
            if patch:
                first_lines = "\n".join(patch.splitlines()[:6])
                patch_lines.append(f"--- {f['filename']} ---\n{first_lines}")
        diff_summary = "\n\n".join(patch_lines) if patch_lines else "No patch data available."

        commit_data = detail.get("commit", {})
        author = (
            detail.get("author", {}) or {}
        ).get("login") or commit_data.get("author", {}).get("name", "unknown")

        # Try to extract PR number from commit message (GitHub convention: #NNN)
        pr_match = re.search(r"#(\d+)", commit_data.get("message", ""))
        pr_number = int(pr_match.group(1)) if pr_match else None

        # Fetch PR title if we have a PR number
        pr_title = commit_data.get("message", "").splitlines()[0]
        if pr_number:
            try:
                pr_resp = requests.get(
                    f"{GITHUB_API_BASE}/repos/{_owner}/{_repo}/pulls/{pr_number}",
                    headers=headers,
                    timeout=10,
                )
                if pr_resp.ok:
                    pr_title = pr_resp.json().get("title", pr_title)
            except Exception:
                pass  # Non-fatal; keep the commit message first line as title

        log.info(f"Deployment info retrieved: sha={sha}, files={len(files_changed)}")

        return {
            "success": True,
            "deployment_id": deployment_id,
            "pr_title": pr_title,
            "pr_number": pr_number,
            "author": author,
            "commit_sha": sha[:7],
            "commit_message": commit_data.get("message", ""),
            "files_changed": files_changed,
            "diff_summary": diff_summary,
            "demo_mode": False,
        }

    except Exception as e:
        log.error(f"Error fetching deployment info for {deployment_id}: {e}")
        return {"success": False, "error": str(e)}


@mcp.tool()
def get_recent_commits(service_name: str, hours_back: int = 4) -> dict:
    """
    Return commits to a specific service's directory in the last N hours.

    Use this tool to understand recent code activity around a service that is
    experiencing incidents. Helps correlate alert timing with specific commits
    or deployments that may have introduced a regression.

    Args:
        service_name: Name of the service (e.g., 'payments-service'). Used to
                      filter commits by path prefix in the repository.
        hours_back:   How many hours back to look (default 4).

    Returns:
        {
            "success": bool,
            "service_name": str,
            "hours_back": int,
            "commits": [
                {
                    "sha": str,
                    "message": str,
                    "author": str,
                    "timestamp": str (ISO 8601),
                    "files_changed": list[str]
                },
                ...
            ],
            "demo_mode": bool   # True when running without real credentials
        }
        On failure: {"success": False, "error": str}
    """
    if not _credentials_available():
        log.info(f"Demo mode: returning demo recent commits for {service_name}")
        # Deep-copy via JSON and replace hardcoded "payments" with the actual service name
        result = json.loads(
            json.dumps(_DEMO_RECENT_COMMITS).replace("payments", service_name.replace("-service", "").replace("_service", ""))
        )
        result["service_name"] = service_name
        result["hours_back"] = hours_back
        return result

    try:
        log.info(f"Fetching recent commits for service '{service_name}' ({hours_back}h) from {_owner}/{_repo}")

        since_dt = datetime.now(timezone.utc) - timedelta(hours=hours_back)
        since_iso = since_dt.isoformat().replace("+00:00", "Z")

        headers = _github_headers()
        commits_url = f"{GITHUB_API_BASE}/repos/{_owner}/{_repo}/commits"
        resp = requests.get(
            commits_url,
            headers=headers,
            params={"since": since_iso, "per_page": 30},
            timeout=10,
        )
        resp.raise_for_status()
        commits_raw = resp.json()

        # Filter by service path if service_name looks like a directory segment
        # Common conventions: services/payments-service, payments-service/, etc.
        service_path_hints = [
            service_name.lower(),
            service_name.lower().replace("-", "_"),
            service_name.lower().replace("_", "-"),
        ]

        result_commits = []
        for c in commits_raw[:10]:
            sha = c["sha"]
            commit_data = c.get("commit", {})
            author = (
                c.get("author", {}) or {}
            ).get("login") or commit_data.get("author", {}).get("name", "unknown")
            timestamp = commit_data.get("author", {}).get("date", "")
            message = commit_data.get("message", "")

            # Fetch full commit to get file list
            try:
                detail_resp = requests.get(
                    f"{GITHUB_API_BASE}/repos/{_owner}/{_repo}/commits/{sha}",
                    headers=headers,
                    timeout=10,
                )
                detail_resp.raise_for_status()
                files = detail_resp.json().get("files", [])
                files_changed = [f["filename"] for f in files]
            except Exception:
                files_changed = []

            # Include commit if any changed file touches the service directory
            # (or include all if service name not resolvable to a path)
            touches_service = any(
                any(hint in fname.lower() for hint in service_path_hints)
                for fname in files_changed
            ) if files_changed else True

            if touches_service:
                result_commits.append(
                    {
                        "sha": sha[:7],
                        "message": message,
                        "author": author,
                        "timestamp": timestamp,
                        "files_changed": files_changed,
                    }
                )

        log.info(f"Found {len(result_commits)} commits for service '{service_name}' in last {hours_back}h")

        return {
            "success": True,
            "service_name": service_name,
            "hours_back": hours_back,
            "commits": result_commits,
            "demo_mode": False,
        }

    except Exception as e:
        log.error(f"Error fetching recent commits for {service_name}: {e}")
        return {"success": False, "error": str(e)}


log.info("GitHub MCP ready — %s mode | repo: %s", "live" if _credentials_available() else "DEMO", GITHUB_REPO or "not set")

if __name__ == "__main__":
    log.info("GitHub MCP server starting on stdio transport")
    mcp.run()
