"""Repo management and listing - multi-repo support."""

from flask import Blueprint, request, jsonify

from services import list_repos, list_commits, get_repo, register_repo

repos_bp = Blueprint("repos", __name__, url_prefix="/api/repos")


@repos_bp.route("", methods=["GET"])
@repos_bp.route("/", methods=["GET"])
def list_all():
    """List all registered repos (any repo we've received webhooks from or explicitly registered)."""
    try:
        repos = list_repos()
        return jsonify({"repos": repos, "count": len(repos)})
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
