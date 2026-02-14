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
                "repos_videos": "GET /api/repos/<owner>/<repo>/videos",
                "sync_commit": "POST /api/sync/commit",
                "sync_pr": "POST /api/sync/pr",
                "pipeline_commit": "POST /api/pipeline/commit",
                "pipeline_ingest_base_video": "POST /api/pipeline/ingest-base-video",
                "video_status": "GET /api/videos/<video_id>",
            },
            "webhook": "POST /webhook/github (multi-repo)",
        }
    )


@api_bp.route("/status")
def status():
    """API status check."""
    return jsonify({"status": "operational"})
