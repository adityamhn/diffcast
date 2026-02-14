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


def get_commit_diff(owner: str, repo: str, sha: str) -> tuple[str, list[dict]]:
    """
    Fetch single commit diff and file changes.
    Returns (raw_diff_string, files_list).
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
    diff_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}"
    diff_r = requests.get(
        diff_url,
        headers={**_headers(), "Accept": "application/vnd.github.v3.diff"},
        timeout=30,
    )
    diff_r.raise_for_status()
    raw_diff = diff_r.text

    return raw_diff, files


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
        raw_diff, files = get_commit_diff(owner, repo, sha)
        commit_data = c.get("commit", {})
        author = commit_data.get("author", {})
        author_info = c.get("author") or {}
        commits.append({
            "sha": sha,
            "sha_short": sha[:7],
            "message": commit_data.get("message", ""),
            "author": {
                "name": author.get("name", author_info.get("login", "unknown")),
                "email": author.get("email", ""),
                "avatar_url": author_info.get("avatar_url", ""),
            },
            "timestamp": author.get("date"),
            "files": files,
            "raw_diff": raw_diff,
        })
    return commits
