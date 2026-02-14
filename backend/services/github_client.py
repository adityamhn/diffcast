"""GitHub API client for fetching diffs and commit data."""

import os
from typing import Optional

import requests


def _headers() -> dict:
    token = os.environ.get("GITHUB_TOKEN")
    h = {"Accept": "application/vnd.github.v3+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _get(url: str) -> requests.Response:
    return requests.get(url, headers=_headers(), timeout=30)


def get_pr_diff(owner: str, repo: str, pr_number: int) -> tuple[str, list[dict]]:
    """
    Fetch PR diff and file changes.
    Returns (raw_diff_string, files_list).
    """
    diff_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    r = _get(diff_url)
    r.raise_for_status()
    data = r.json()

    # Fetch diff (different Accept header)
    diff_r = requests.get(
        diff_url,
        headers={**_headers(), "Accept": "application/vnd.github.v3.diff"},
        timeout=30,
    )
    diff_r.raise_for_status()
    raw_diff = diff_r.text

    # File-level changes
    files_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files"
    files_r = _get(files_url)
    files_r.raise_for_status()
    files = files_r.json()

    return raw_diff, files


def get_commit_diff(owner: str, repo: str, sha: str) -> tuple[str, list[dict], dict]:
    """
    Fetch single commit diff and file changes.
    Returns (raw_diff_string, files_list, commit_meta).
    commit_meta has: message, author { name, email, avatar_url }, timestamp
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}"
    r = _get(url)
    r.raise_for_status()
    data = r.json()

    files = []
    for f in data.get("files", []):
        files.append({
            "path": f["filename"],
            "status": f.get("status", "modified"),
            "additions": f.get("additions", 0),
            "deletions": f.get("deletions", 0),
            "patch": f.get("patch"),
        })

    # Raw diff
    diff_r = requests.get(
        url,
        headers={**_headers(), "Accept": "application/vnd.github.v3.diff"},
        timeout=30,
    )
    diff_r.raise_for_status()
    raw_diff = diff_r.text

    # Extract author from API response (has avatar_url; webhook doesn't)
    commit_data = data.get("commit", {})
    author_data = commit_data.get("author", {})
    author_info = data.get("author") or {}
    commit_meta = {
        "sha": data.get("sha", sha),
        "message": commit_data.get("message", ""),
        "author": {
            "name": author_data.get("name", author_info.get("login", "unknown")),
            "email": author_data.get("email", ""),
            "avatar_url": author_info.get("avatar_url", ""),
        },
        "timestamp": author_data.get("date"),
    }

    return raw_diff, files, commit_meta


def get_compare_diff(owner: str, repo: str, base: str, head: str) -> list[dict]:
    """
    Fetch diff between two refs (e.g. base...head).
    Returns list of commits with their data.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/compare/{base}...{head}"
    r = _get(url)
    r.raise_for_status()
    data = r.json()

    commits = []
    for c in data.get("commits", []):
        sha = c["sha"]
        raw_diff, files, commit_meta = get_commit_diff(owner, repo, sha)
        commits.append({
            "sha": sha,
            "sha_short": sha[:7],
            "message": commit_meta["message"],
            "author": commit_meta["author"],
            "timestamp": commit_meta["timestamp"],
            "files": files,
            "raw_diff": raw_diff,
        })
    return commits
