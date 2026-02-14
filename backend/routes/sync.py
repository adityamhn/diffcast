"""Manual sync API - for testing without webhooks."""

import os

from flask import Blueprint, request, jsonify

from firebase_schema import CommitDoc, repo_id
from services import get_commit_diff, get_compare_diff, store_commit, store_repo

sync_bp = Blueprint("sync", __name__, url_prefix="/api/sync")


def _parse_ts(ts):
    from dateutil import parser as date_parser
    try:
        return date_parser.parse(ts) if ts else None
    except Exception:
        return None


@sync_bp.route("/commit", methods=["POST"])
def sync_commit():
    """
    Manually sync a single commit.
    Body: { "owner": "octocat", "repo": "hello-world", "sha": "abc1234" }
    """
    data = request.get_json() or {}
    owner = data.get("owner")
    repo = data.get("repo")
    sha = data.get("sha")
    if not all([owner, repo, sha]):
        return jsonify({"error": "Missing owner, repo, or sha"}), 400

    if not os.environ.get("GITHUB_TOKEN"):
        return jsonify({"error": "GITHUB_TOKEN required for sync"}), 503

    try:
        raw_diff, files = get_commit_diff(owner, repo, sha)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    full_name = f"{owner}/{repo}"
    store_repo(full_name=full_name, owner=owner, name=repo)

    # Fetch commit details for author/timestamp
    import requests
    r = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}",
        headers={"Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN')}", "Accept": "application/vnd.github.v3+json"},
        timeout=30,
    )
    r.raise_for_status()
    c = r.json()
    commit_data = c.get("commit", {})
    author_data = commit_data.get("author", {})
    author_info = c.get("author") or {}

    doc = CommitDoc(
        sha=c["sha"],
        sha_short=c["sha"][:7],
        repo_id=repo_id(full_name),
        repo_full_name=full_name,
        message=commit_data.get("message", ""),
        author={
            "name": author_data.get("name", author_info.get("login", "unknown")),
            "email": author_data.get("email", ""),
            "avatar_url": author_info.get("avatar_url", ""),
        },
        timestamp=_parse_ts(author_data.get("date")) or __import__("datetime").datetime.utcnow(),
        branch="unknown",
        pr_number=None,
        pr_url=None,
        pr_title=None,
        files=[
            {"path": f.get("filename", ""), "status": f.get("status", "modified"), "additions": f.get("additions", 0), "deletions": f.get("deletions", 0), "patch": f.get("patch")}
            for f in files
        ],
    )
    cid = store_commit(doc)
    return jsonify({"ok": True, "commit_id": cid, "sha": sha})


@sync_bp.route("/pr", methods=["POST"])
def sync_pr():
    """
    Manually sync all commits from a PR.
    Body: { "owner": "octocat", "repo": "hello-world", "pr_number": 42 }
    """
    data = request.get_json() or {}
    owner = data.get("owner")
    repo = data.get("repo")
    pr_number = data.get("pr_number")
    if not all([owner, repo, pr_number]):
        return jsonify({"error": "Missing owner, repo, or pr_number"}), 400

    if not os.environ.get("GITHUB_TOKEN"):
        return jsonify({"error": "GITHUB_TOKEN required for sync"}), 503

    import requests
    r = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
        headers={"Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN')}", "Accept": "application/vnd.github.v3+json"},
        timeout=30,
    )
    r.raise_for_status()
    pr = r.json()
    base_sha = pr.get("base", {}).get("sha")
    head_sha = pr.get("head", {}).get("sha")
    pr_url = pr.get("html_url", "")
    pr_title = pr.get("title", "")
    branch = pr.get("head", {}).get("ref", "")

    full_name = f"{owner}/{repo}"
    store_repo(full_name=full_name, owner=owner, name=repo)

    try:
        commits = get_compare_diff(owner, repo, base_sha, head_sha)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    stored = []
    for c in commits:
        doc = CommitDoc(
            sha=c["sha"],
            sha_short=c["sha_short"],
            repo_id=repo_id(full_name),
            repo_full_name=full_name,
            message=c["message"],
            author=c["author"],
            timestamp=_parse_ts(c.get("timestamp")) or __import__("datetime").datetime.utcnow(),
            branch=branch,
            pr_number=pr_number,
            pr_url=pr_url,
            pr_title=pr_title,
            files=[
                {"path": f.get("path", ""), "status": f.get("status", "modified"), "additions": f.get("additions", 0), "deletions": f.get("deletions", 0), "patch": f.get("patch")}
                for f in c.get("files", [])
            ],
        )
        cid = store_commit(doc)
        stored.append({"commit_id": cid, "sha": c["sha_short"]})

    return jsonify({"ok": True, "commits_stored": len(stored), "commits": stored})
