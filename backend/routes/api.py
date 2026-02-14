"""API routes - versioned under /api."""

from flask import Blueprint, jsonify

api_bp = Blueprint("api", __name__, url_prefix="/api")


@api_bp.route("/")
def index():
    """API root - lists available endpoints."""
    return jsonify(
        {
            "version": "v1",
            "endpoints": {
                "status": "/api/status",
                "repos": "GET /api/repos",
                "repos_add": "POST /api/repos/add (repo_url + webhook_secret)",
                "repos_register": "POST /api/repos/register",
                "repos_commits": "GET /api/repos/<owner>/<repo>/commits",
                "sync_commit": "POST /api/sync/commit",
                "sync_pr": "POST /api/sync/pr",
            },
            "webhook": "POST /webhook/github (multi-repo)",
        }
    )


@api_bp.route("/status")
def status():
    """API status check."""
    return jsonify({"status": "operational"})
