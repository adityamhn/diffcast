"""Repo management and listing - multi-repo support."""

import re

from flask import Blueprint, request, jsonify

from services import list_repos, list_commits, get_repo, register_repo


def _parse_github_url(url: str) -> tuple[str, str] | None:
    """Extract owner/repo from GitHub URL. Returns (owner, repo) or None."""
    if not url or not url.strip():
        return None
    url = url.strip()
    # Handle git@github.com:owner/repo.git
    m = re.match(r"(?:git@|https?://)github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if m:
        return m.group(1), m.group(2).rstrip("/")
    # Handle github.com/owner/repo
    m = re.match(r"github\.com/([^/]+)/([^/]+)", url)
    if m:
        return m.group(1), m.group(2).rstrip("/")
    return None

repos_bp = Blueprint("repos", __name__, url_prefix="/api/repos")


def _sanitize_repo(repo: dict) -> dict:
    """Remove sensitive fields from repo for API response."""
    out = dict(repo)
    out.pop("webhook_secret", None)
    return out


@repos_bp.route("", methods=["GET"])
@repos_bp.route("/", methods=["GET"])
def list_all():
    """List all registered repos (any repo we've received webhooks from or explicitly registered)."""
    try:
        repos = list_repos()
        return jsonify({
            "repos": [_sanitize_repo(r) for r in repos],
            "count": len(repos),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@repos_bp.route("/add", methods=["POST"])
def add_by_url():
    """
    Register a repo by GitHub URL and webhook secret.
    Body: { "repo_url": "https://github.com/owner/repo", "webhook_secret": "your-secret" }
    """
    data = request.get_json() or {}
    repo_url = data.get("repo_url") or data.get("url")
    webhook_secret = data.get("webhook_secret")
    if not repo_url:
        return jsonify({"error": "Missing repo_url"}), 400
    if not webhook_secret:
        return jsonify({"error": "Missing webhook_secret"}), 400
    parsed = _parse_github_url(repo_url)
    if not parsed:
        return jsonify({"error": "Invalid GitHub URL. Use e.g. https://github.com/owner/repo"}), 400
    owner, name = parsed
    try:
        result = register_repo(owner=owner, name=name, webhook_secret=webhook_secret)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@repos_bp.route("/register", methods=["POST"])
def register():
    """
    Register a repo for webhook processing.
    Body: { "owner": "octocat", "repo": "hello-world" }
    Returns webhook_secret to use when configuring the GitHub webhook.
    """
    data = request.get_json() or {}
    owner = data.get("owner")
    name = data.get("repo") or data.get("name")
    webhook_secret = data.get("webhook_secret")  # Optional: provide your own
    if not owner or not name:
        return jsonify({"error": "Missing owner or repo"}), 400
    try:
        result = register_repo(owner=owner, name=name, webhook_secret=webhook_secret)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@repos_bp.route("/<path:repo_path>/commits", methods=["GET"])
def repo_commits(repo_path: str):
    """
    List commits for a repo.
    repo_path: owner/repo (e.g. octocat/hello-world)
    Query: ?limit=50
    """
    if "/" not in repo_path:
        return jsonify({"error": "Use owner/repo format (e.g. octocat/hello-world)"}), 400
    limit = min(int(request.args.get("limit", 50)), 100)
    try:
        commits = list_commits(repo_path, limit=limit)
        return jsonify({"commits": commits, "count": len(commits)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@repos_bp.route("/<path:repo_path>", methods=["GET"])
def repo_detail(repo_path: str):
    """Get repo details by owner/repo."""
    if "/" not in repo_path:
        return jsonify({"error": "Use owner/repo format (e.g. octocat/hello-world)"}), 400
    repo = get_repo(repo_path)
    if not repo:
        return jsonify({"error": "Repo not found"}), 404
    return jsonify(repo)
