"""Pipeline routes for commit media generation."""

from __future__ import annotations

import logging
import tempfile
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request

from firebase_schema import commit_id
from services.storage_service import upload_file
from services.media_generation_service import (
    generate_browser_use_goal,
    generate_scene_script,
    generate_shot_plan,
)
from services import (
    enqueue_commit_pipeline,
    enqueue_feature_demo_pipeline,
    get_commit_by_id,
    get_commit_diff,
    get_video,
    list_videos,
    update_commit_goal,
)
from services.snapshot_service import extract_snapshots
from services.video_stitch_service import probe_video

pipeline_bp = Blueprint("pipeline", __name__, url_prefix="/api")
logger = logging.getLogger(__name__)


@pipeline_bp.route("/pipeline/commit", methods=["POST"])
def trigger_commit_pipeline():
    """
    Queue commit script preparation (stage 1 of cinematic pipeline).

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

@pipeline_bp.route("/pipeline/browser-use-goal", methods=["POST"])
def trigger_browser_use_goal():
    """
    Generate a browser use goal from commit diff.
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
    owner = data.get("owner")
    repo = data.get("repo") or data.get("name")
    sha = data.get("sha")
    if not commit_doc_id:
        if not all([owner, repo, sha]):
            logger.warning(
                "Pipeline trigger rejected: missing commit_id or owner/repo/sha"
            )
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
        logger.info(
            "Pipeline trigger resolved commit_id=%s from owner/repo/sha", commit_doc_id
        )

    commit_doc = get_commit_by_id(commit_doc_id)
    commit_exists_in_firestore = commit_doc is not None
    if not commit_doc and owner and repo and sha:
        try:
            _raw_diff, files, _commit_meta = get_commit_diff(
                str(owner), str(repo), str(sha)
            )
        except Exception as exc:
            logger.exception("Browser use goal: failed to fetch commit from GitHub")
            return jsonify({"error": str(exc)}), 502
        full_name = f"{owner}/{repo}"
        commit_doc = {
            "id": commit_doc_id,
            "repo_full_name": full_name,
            "files": files,
        }
    if not commit_doc:
        logger.warning("Browser use goal: commit not found commit_id=%s", commit_doc_id)
        return jsonify(
            {"error": f"Commit not found: {commit_doc_id}. Provide owner/repo/sha to fetch from GitHub"}
        ), 404

    try:
        result = generate_browser_use_goal(commit_doc)
        logger.info("Browser use goal generated commit_id=%s", commit_doc_id)
        logger.info("Browser use goal=%s", result)
    except Exception as exc:
        logger.exception("Browser use goal generation failed commit_id=%s", commit_doc_id)
        return jsonify({"error": str(exc)}), 500

    if commit_exists_in_firestore:
        update_commit_goal(commit_doc_id, result)

    return jsonify({"ok": True, "browser_use_goal": result}), 200


@pipeline_bp.route("/pipeline/feature-demo", methods=["POST"])
def trigger_feature_demo_pipeline():
    """
    Queue feature demo pipeline: generate goal from commit -> record via browser-use -> upload -> save to commit.

    Body:
      { "commit_id": "<repo_sha7>", "force": false }
    OR:
      { "owner": "octocat", "repo": "hello", "sha": "<sha>", "force": false }

    Requires repo to have website_url set (PATCH /api/repos/owner/repo with {"website_url": "https://..."}).
    """
    data = request.get_json() or {}
    force = bool(data.get("force", False))
    commit_doc_id = data.get("commit_id")
    owner = data.get("owner")
    repo = data.get("repo") or data.get("name")
    sha = data.get("sha")
    if not commit_doc_id:
        if not all([owner, repo, sha]):
            return jsonify(
                {
                    "error": "Provide commit_id or owner/repo/sha",
                    "example": {"commit_id": "octocat_hello-world_abc1234", "force": False},
                }
            ), 400
        commit_doc_id = commit_id(f"{owner}/{repo}", sha)

    try:
        result = enqueue_feature_demo_pipeline(commit_id=commit_doc_id, force=force)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        logger.exception("Feature demo enqueue failed commit_id=%s", commit_doc_id)
        return jsonify({"error": str(exc)}), 500

    return jsonify({"ok": True, **result}), 202 if result.get("queued") else 200


@pipeline_bp.route("/pipeline/ingest-base-video", methods=["POST"])
def ingest_base_video():
    """Legacy endpoint - now redirects to unified pipeline.

    The unified pipeline auto-generates demo videos, so manual source video
    ingestion is no longer needed. Use POST /pipeline/commit instead.
    """
    return jsonify({
        "error": "This endpoint is deprecated. Use POST /pipeline/commit instead.",
        "message": "The unified pipeline now auto-generates demo videos from commits. "
                   "Simply call POST /pipeline/commit with your commit_id.",
        "example": {
            "commit_id": "octocat_hello-world_abc1234",
            "languages": ["en", "es"],
            "force": False,
        },
    }), 410  # HTTP 410 Gone


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


# =============================================================================
# TEST ENDPOINTS - For testing individual pipeline phases
# =============================================================================


@pipeline_bp.route("/pipeline/test/goal", methods=["POST"])
def test_goal_phase():
    """TEST: Generate browser-use goal from commit diff (Phase 1)."""
    data = request.get_json() or {}
    commit_doc_id = _resolve_commit_id(data)
    if isinstance(commit_doc_id, tuple):
        return commit_doc_id  # Error response

    commit_doc = get_commit_by_id(commit_doc_id)
    if not commit_doc:
        return jsonify({"error": f"Commit not found: {commit_doc_id}"}), 404

    try:
        goal = generate_browser_use_goal(commit_doc)
        update_commit_goal(commit_doc_id, goal)
        return jsonify({
            "ok": True,
            "phase": "goal",
            "commit_id": commit_doc_id,
            "goal": goal,
            "goal_length": len(goal),
        })
    except Exception as exc:
        logger.exception("Test goal phase failed commit_id=%s", commit_doc_id)
        return jsonify({"error": str(exc)}), 500


@pipeline_bp.route("/pipeline/test/demo", methods=["POST"])
def test_demo_phase():
    """TEST: Record demo video using browser-use (Phase 2)."""
    data = request.get_json() or {}
    commit_doc_id = _resolve_commit_id(data)
    if isinstance(commit_doc_id, tuple):
        return commit_doc_id  # Error response

    try:
        result = enqueue_feature_demo_pipeline(commit_id=commit_doc_id, force=True)
        return jsonify({
            "ok": True,
            "phase": "demo",
            "commit_id": commit_doc_id,
            **result,
        })
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        logger.exception("Test demo phase failed commit_id=%s", commit_doc_id)
        return jsonify({"error": str(exc)}), 500


@pipeline_bp.route("/pipeline/test/script", methods=["POST"])
def test_script_phase():
    """TEST: Generate scene script and shot plan (Phase 3)."""
    data = request.get_json() or {}
    commit_doc_id = _resolve_commit_id(data)
    if isinstance(commit_doc_id, tuple):
        return commit_doc_id  # Error response

    commit_doc = get_commit_by_id(commit_doc_id)
    if not commit_doc:
        return jsonify({"error": f"Commit not found: {commit_doc_id}"}), 404

    demo_duration = data.get("demo_duration_sec", 10.0)

    try:
        script = generate_scene_script(commit_doc)
        shot_plan = generate_shot_plan(
            script=script,
            target_duration_sec=28,
            demo_video_duration_sec=demo_duration,
        )
        return jsonify({
            "ok": True,
            "phase": "script",
            "commit_id": commit_doc_id,
            "script": script,
            "shot_plan": shot_plan,
            "clip_prompts_count": len(shot_plan.get("clip_prompts", [])),
            "timeline_segments": len(shot_plan.get("timeline", [])),
        })
    except Exception as exc:
        logger.exception("Test script phase failed commit_id=%s", commit_doc_id)
        return jsonify({"error": str(exc)}), 500


@pipeline_bp.route("/pipeline/test/snapshots", methods=["POST"])
def test_snapshots_phase():
    """TEST: Extract snapshots from a video file (Phase 4).

    Body:
      { "video_url": "https://..." } - URL of video to extract snapshots from
    """
    data = request.get_json() or {}
    video_url = data.get("video_url")

    if not video_url:
        return jsonify({
            "error": "video_url is required",
            "example": {"video_url": "https://storage.googleapis.com/.../demo.mp4"},
        }), 400

    try:
        import requests as http_requests
        import shutil

        # Download video to temp file
        workspace = Path(tempfile.mkdtemp(prefix="diffcast-test-snapshots-"))
        video_path = workspace / "input.mp4"

        logger.info("Downloading video for snapshot test url=%s", video_url)
        response = http_requests.get(video_url, timeout=60)
        response.raise_for_status()
        video_path.write_bytes(response.content)

        # Probe video
        video_meta = probe_video(video_path)

        # Extract snapshots
        snapshots_dir = workspace / "snapshots"
        snapshot_paths = extract_snapshots(
            video_path=video_path,
            output_dir=snapshots_dir,
            num_snapshots=2,
            strategy="uniform",
        )

        # Get snapshot info
        snapshot_info = []
        for i, path in enumerate(snapshot_paths):
            snapshot_info.append({
                "index": i,
                "filename": path.name,
                "size_bytes": path.stat().st_size,
            })

        # Cleanup
        shutil.rmtree(workspace, ignore_errors=True)

        return jsonify({
            "ok": True,
            "phase": "snapshots",
            "video_url": video_url,
            "video_duration_sec": video_meta["duration_sec"],
            "snapshots_extracted": len(snapshot_paths),
            "snapshots": snapshot_info,
        })
    except Exception as exc:
        logger.exception("Test snapshots phase failed")
        return jsonify({"error": str(exc)}), 500


@pipeline_bp.route("/pipeline/test/veo", methods=["POST"])
def test_veo_phase():
    """TEST: Generate a single Veo clip (Phase 5).

    Body:
      {
        "prompt": "Detailed Veo prompt...",
        "reference_image_url": "https://..." (optional),
        "duration_sec": 6
      }
    """
    data = request.get_json() or {}
    prompt = data.get("prompt")
    reference_image_url = data.get("reference_image_url")
    duration_sec = data.get("duration_sec", 6)

    if not prompt:
        return jsonify({
            "error": "prompt is required",
            "example": {
                "prompt": "Photorealistic product demo of a web app...",
                "reference_image_url": "https://... (optional)",
                "duration_sec": 6,
            },
        }), 400

    try:
        from services.gemini_video_service import generate_veo_clip
        import requests as http_requests
        import shutil

        workspace = Path(tempfile.mkdtemp(prefix="diffcast-test-veo-"))
        reference_path = None

        # Download reference image if provided
        if reference_image_url:
            logger.info("Downloading reference image url=%s", reference_image_url)
            response = http_requests.get(reference_image_url, timeout=30)
            response.raise_for_status()
            reference_path = workspace / "reference.png"
            reference_path.write_bytes(response.content)

        # Generate Veo clip
        output_path = workspace / "veo_test.mp4"
        result = generate_veo_clip(
            prompt=prompt,
            output_path=output_path,
            duration_sec=duration_sec,
            reference_image_path=reference_path,
        )

        # Get video metadata
        video_meta = probe_video(result["path"])

        # Upload to blob storage
        dest_path = f"test/veo/veo_{uuid.uuid4().hex}.mp4"
        upload_result = upload_file(
            local_path=result["path"],
            destination_path=dest_path,
            content_type="video/mp4",
        )
        video_url = upload_result["url"]
        logger.info("Test Veo clip uploaded url=%s path=%s", video_url, dest_path)
        print(f"Test Veo clip URL: {video_url}")

        # Cleanup temp workspace
        shutil.rmtree(workspace, ignore_errors=True)

        return jsonify({
            "ok": True,
            "phase": "veo",
            "prompt_length": len(prompt),
            "has_reference_image": result.get("has_reference_image", False),
            "model": result.get("model"),
            "duration_sec": video_meta["duration_sec"],
            "width": video_meta["width"],
            "height": video_meta["height"],
            "video_url": video_url,
            "storage_path": dest_path,
        })
    except Exception as exc:
        logger.exception("Test veo phase failed")
        return jsonify({"error": str(exc)}), 500


@pipeline_bp.route("/pipeline/test/stitch", methods=["POST"])
def test_stitch_phase():
    """TEST: Assemble videos (Phase 6).

    Body:
      {
        "opener_url": "https://...",
        "demo_url": "https://...",
        "closing_urls": ["https://..."]  # 1 clip (Veo conclusion, 6s)
      }
    """
    data = request.get_json() or {}
    opener_url = data.get("opener_url")
    demo_url = data.get("demo_url")
    closing_urls = data.get("closing_urls", [])

    if not opener_url or not demo_url:
        return jsonify({
            "error": "opener_url and demo_url are required",
            "example": {
                "opener_url": "https://.../veo_opener.mp4",
                "demo_url": "https://.../demo.mp4",
                "closing_urls": ["https://.../veo_conclusion.mp4"],
            },
        }), 400

    try:
        from services.video_stitch_service import assemble_feature_video
        import requests as http_requests
        import shutil

        workspace = Path(tempfile.mkdtemp(prefix="diffcast-test-stitch-"))

        def download_video(url, name):
            path = workspace / name
            response = http_requests.get(url, timeout=60)
            response.raise_for_status()
            path.write_bytes(response.content)
            return path

        # Download all videos
        logger.info("Downloading videos for stitch test")
        opener_path = download_video(opener_url, "opener.mp4")
        demo_path = download_video(demo_url, "demo.mp4")
        closing_paths = [
            download_video(url, f"closing_{i}.mp4")
            for i, url in enumerate(closing_urls)
        ]

        # Assemble
        output_path = workspace / "assembled.mp4"
        result = assemble_feature_video(
            opener_clip=opener_path,
            demo_video=demo_path,
            closing_clips=closing_paths,
            output_path=output_path,
        )

        # Cleanup
        shutil.rmtree(workspace, ignore_errors=True)

        return jsonify({
            "ok": True,
            "phase": "stitch",
            "input_videos": 1 + 1 + len(closing_paths),
            "output_duration_sec": result["duration_sec"],
            "output_width": result["width"],
            "output_height": result["height"],
        })
    except Exception as exc:
        logger.exception("Test stitch phase failed")
        return jsonify({"error": str(exc)}), 500


def _resolve_commit_id(data: dict) -> str | tuple:
    """Helper to resolve commit_id from request data."""
    commit_doc_id = data.get("commit_id")
    if not commit_doc_id:
        owner = data.get("owner")
        repo = data.get("repo") or data.get("name")
        sha = data.get("sha")
        if not all([owner, repo, sha]):
            return jsonify({
                "error": "Provide commit_id or owner/repo/sha",
                "example": {"owner": "octocat", "repo": "hello", "sha": "abc1234"},
            }), 400
        commit_doc_id = commit_id(f"{owner}/{repo}", sha)
    return commit_doc_id
