"""Pipeline routes for commit media generation."""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from firebase_schema import commit_id
from services import enqueue_commit_pipeline, get_video, list_videos

pipeline_bp = Blueprint("pipeline", __name__, url_prefix="/api")
logger = logging.getLogger(__name__)


@pipeline_bp.route("/pipeline/commit", methods=["POST"])
def trigger_commit_pipeline():
    """
    Queue commit media generation.

    Body:
      { "commit_id": "<repo_sha7>", "languages": ["en","es"], "force": false }
    OR:
      { "owner": "octocat", "repo": "hello", "sha": "<sha>", "languages": [...], "force": false }
    """
    data = request.get_json() or {}
    force = bool(data.get("force", False))
    languages = data.get("languages")
    logger.info(
        "Pipeline trigger request force=%s has_commit_id=%s languages=%s",
        force,
        bool(data.get("commit_id")),
        languages,
    )
    if languages is not None and not isinstance(languages, list):
        logger.warning("Pipeline trigger rejected: languages is not array")
        return jsonify({"error": "languages must be an array of language codes"}), 400

    commit_doc_id = data.get("commit_id")
    if not commit_doc_id:
        owner = data.get("owner")
        repo = data.get("repo") or data.get("name")
        sha = data.get("sha")
        if not all([owner, repo, sha]):
            logger.warning("Pipeline trigger rejected: missing commit_id or owner/repo/sha")
            return jsonify(
                {
                    "error": "Provide commit_id or owner/repo/sha",
                    "example": {
                        "commit_id": "octocat_hello-world_abc1234",
                        "languages": ["en", "es"],
                        "force": False,
                    },
                }
            ), 400
        commit_doc_id = commit_id(f"{owner}/{repo}", sha)
        logger.info("Pipeline trigger resolved commit_id=%s from owner/repo/sha", commit_doc_id)

    try:
        result = enqueue_commit_pipeline(
            commit_id=commit_doc_id,
            languages=languages,
            force=force,
        )
    except ValueError as exc:
        logger.warning("Pipeline trigger failed commit_id=%s error=%s", commit_doc_id, exc)
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        logger.exception("Pipeline trigger unexpected failure commit_id=%s", commit_doc_id)
        return jsonify({"error": str(exc)}), 500

    video = get_video(result["video_id"])
    logger.info(
        "Pipeline trigger result commit_id=%s video_id=%s queued=%s skipped=%s",
        commit_doc_id,
        result.get("video_id"),
        result.get("queued"),
        result.get("skipped"),
    )
    return jsonify({"ok": True, **result, "video": video}), 202 if result.get("queued") else 200


@pipeline_bp.route("/videos/<video_doc_id>", methods=["GET"])
def get_video_status(video_doc_id: str):
    """Get status and generated artifact metadata for one video pipeline job."""
    video = get_video(video_doc_id)
    if not video:
        logger.info("Video status requested but not found video_id=%s", video_doc_id)
        return jsonify({"error": "Video not found"}), 404
    logger.info("Video status requested video_id=%s status=%s", video_doc_id, video.get("status"))
    return jsonify(video)


@pipeline_bp.route("/repos/<owner>/<repo>/videos", methods=["GET"])
def list_repo_videos(owner: str, repo: str):
    """List generated videos for a repository."""
    limit = min(int(request.args.get("limit", 20)), 100)
    status_filter = request.args.get("status")
    repo_full_name = f"{owner}/{repo}"
    logger.info(
        "Video list requested repo=%s limit=%s status_filter=%s",
        repo_full_name,
        limit,
        status_filter,
    )
    try:
        videos = list_videos(repo_full_name=repo_full_name, limit=limit, status_filter=status_filter)
        return jsonify({"videos": videos, "count": len(videos), "repo_full_name": repo_full_name})
    except Exception as exc:
        logger.exception("Video list failed repo=%s", repo_full_name)
        return jsonify({"error": str(exc)}), 500
