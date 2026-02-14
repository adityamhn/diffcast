"""Asynchronous commit pipeline orchestrator."""

from __future__ import annotations

import logging
import os
import shutil
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from threading import Lock
from typing import Any

from services.firebase_service import (
    build_video_doc_id,
    get_commit_by_id,
    get_video,
    update_video_status,
    upsert_video_doc,
)
from services.media_generation_service import generate_commit_media_assets, parse_target_languages
from services.storage_service import upload_file

MAX_WORKERS = max(1, int(os.environ.get("PIPELINE_MAX_WORKERS", "2")))
_EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="diffcast-pipeline")
_FUTURES: dict[str, Future] = {}
_LOCK = Lock()
logger = logging.getLogger(__name__)


def _base_video_doc(
    video_doc_id: str,
    commit_doc: dict[str, Any],
    languages_requested: list[str],
) -> dict[str, Any]:
    return {
        "video_id": video_doc_id,
        "commit_id": commit_doc["id"],
        "repo_full_name": commit_doc["repo_full_name"],
        "sha": commit_doc["sha"],
        "sha_short": commit_doc["sha_short"],
        "status": "queued",
        "stage": "script",
        "error": None,
        "languages_requested": languages_requested,
        "base_video_url": None,
        "tracks": {},
        "script": None,
        "updated_at": datetime.utcnow(),
    }


def _upload_pipeline_assets(
    repo_id: str,
    sha_short: str,
    generation_result: dict[str, Any],
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    logger.info("Uploading base video repo_id=%s sha_short=%s", repo_id, sha_short)
    base_video_upload = upload_file(
        local_path=generation_result["base_video_path"],
        destination_path=f"videos/{repo_id}/{sha_short}/base.mp4",
        content_type="video/mp4",
    )

    uploaded_tracks: dict[str, dict[str, Any]] = {}
    for language, track in generation_result["tracks"].items():
        logger.info(
            "Uploading language track repo_id=%s sha_short=%s language=%s track_status=%s",
            repo_id,
            sha_short,
            language,
            track.get("status"),
        )
        if track.get("status") != "completed":
            uploaded_tracks[language] = {
                "status": "failed",
                "error": track.get("error"),
                "voice_script": track.get("voice_script"),
                "duration_sec": track.get("duration_sec"),
                "audio_url": None,
                "captions_url": None,
                "final_video_url": None,
                "final_video_meta": None,
            }
            continue

        audio_upload = upload_file(
            local_path=track["audio_path"],
            destination_path=f"videos/{repo_id}/{sha_short}/tracks/{language}/voice.mp3",
            content_type="audio/mpeg",
        )
        captions_upload = upload_file(
            local_path=track["captions_path"],
            destination_path=f"videos/{repo_id}/{sha_short}/tracks/{language}/captions.srt",
            content_type="application/x-subrip",
        )
        final_video_upload = upload_file(
            local_path=track["final_video_path"],
            destination_path=f"videos/{repo_id}/{sha_short}/tracks/{language}/final.mp4",
            content_type="video/mp4",
        )
        logger.info(
            "Uploaded language track repo_id=%s sha_short=%s language=%s",
            repo_id,
            sha_short,
            language,
        )
        uploaded_tracks[language] = {
            "status": "completed",
            "error": None,
            "voice_script": track.get("voice_script"),
            "duration_sec": track.get("duration_sec"),
            "audio_url": audio_upload["url"],
            "captions_url": captions_upload["url"],
            "final_video_url": final_video_upload["url"],
            "final_video_meta": track.get("final_video_meta"),
        }

    return base_video_upload, uploaded_tracks


def _run_commit_pipeline(
    commit_doc_id: str,
    video_doc_id: str,
    languages_requested: list[str],
) -> None:
    logger.info(
        "Pipeline job started video_id=%s commit_id=%s languages=%s",
        video_doc_id,
        commit_doc_id,
        languages_requested,
    )
    commit_doc = get_commit_by_id(commit_doc_id)
    if not commit_doc:
        logger.error("Pipeline job failed early: commit not found commit_id=%s", commit_doc_id)
        update_video_status(
            video_doc_id=video_doc_id,
            status="failed",
            stage="error",
            error=f"Commit not found: {commit_doc_id}",
        )
        return

    generation_result: dict[str, Any] | None = None
    try:
        logger.info("Pipeline stage update video_id=%s stage=script", video_doc_id)
        update_video_status(
            video_doc_id=video_doc_id,
            status="running",
            stage="script",
            error=None,
            extra_fields={"languages_requested": languages_requested},
        )
        generation_result = generate_commit_media_assets(
            commit_doc=commit_doc,
            languages=languages_requested,
        )
        logger.info(
            "Generation completed video_id=%s scenes=%s tracks=%s",
            video_doc_id,
            len(generation_result["script"].get("scenes", [])),
            len(generation_result.get("tracks", {})),
        )
        logger.info("Pipeline stage update video_id=%s stage=upload", video_doc_id)
        update_video_status(
            video_doc_id=video_doc_id,
            status="running",
            stage="upload",
            error=None,
            extra_fields={"script": generation_result["script"]},
        )

        repo_id = commit_doc["repo_id"]
        sha_short = commit_doc["sha_short"]
        base_upload, uploaded_tracks = _upload_pipeline_assets(
            repo_id=repo_id,
            sha_short=sha_short,
            generation_result=generation_result,
        )

        successful_tracks = [
            language
            for language, track in uploaded_tracks.items()
            if track.get("status") == "completed"
        ]
        final_status = "completed" if successful_tracks else "failed"
        final_stage = "done" if successful_tracks else "error"
        final_error = None if successful_tracks else "No language tracks were generated successfully"
        logger.info(
            "Pipeline finalized video_id=%s status=%s successful_tracks=%s total_tracks=%s",
            video_doc_id,
            final_status,
            len(successful_tracks),
            len(uploaded_tracks),
        )

        update_video_status(
            video_doc_id=video_doc_id,
            status=final_status,
            stage=final_stage,
            error=final_error,
            extra_fields={
                "base_video_url": base_upload["url"],
                "tracks": uploaded_tracks,
                "script": generation_result["script"],
                "video_meta": generation_result.get("base_video_meta"),
            },
        )
    except Exception as exc:
        logger.exception(
            "Pipeline execution failed video_id=%s commit_id=%s",
            video_doc_id,
            commit_doc_id,
        )
        update_video_status(
            video_doc_id=video_doc_id,
            status="failed",
            stage="error",
            error=str(exc),
            extra_fields={
                "tracks": generation_result.get("tracks", {}) if generation_result else {},
                "script": generation_result.get("script") if generation_result else None,
            },
        )
    finally:
        with _LOCK:
            _FUTURES.pop(video_doc_id, None)
        logger.info("Pipeline future cleaned video_id=%s", video_doc_id)
        if generation_result and generation_result.get("workspace_dir"):
            shutil.rmtree(generation_result["workspace_dir"], ignore_errors=True)
            logger.info("Pipeline workspace cleaned video_id=%s", video_doc_id)


def enqueue_commit_pipeline(
    commit_id: str,
    languages: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Queue commit pipeline job if not already running/completed."""
    logger.info("Enqueue requested commit_id=%s force=%s languages=%s", commit_id, force, languages)
    commit_doc = get_commit_by_id(commit_id)
    if not commit_doc:
        logger.warning("Enqueue rejected: commit not found commit_id=%s", commit_id)
        raise ValueError(f"Commit not found: {commit_id}")

    languages_requested = parse_target_languages(languages)
    video_doc_id = build_video_doc_id(commit_doc["repo_full_name"], commit_doc["sha"])
    existing = get_video(video_doc_id)
    if existing and existing.get("status") in {"running", "completed"} and not force:
        logger.info(
            "Enqueue skipped video_id=%s existing_status=%s",
            video_doc_id,
            existing.get("status"),
        )
        return {
            "queued": False,
            "skipped": True,
            "reason": "already_running_or_completed",
            "video_id": video_doc_id,
            "commit_id": commit_id,
            "status": existing.get("status"),
        }

    upsert_video_doc(
        video_doc_id=video_doc_id,
        payload=_base_video_doc(
            video_doc_id=video_doc_id,
            commit_doc=commit_doc,
            languages_requested=languages_requested,
        ),
    )

    with _LOCK:
        running_future = _FUTURES.get(video_doc_id)
        if running_future and not running_future.done() and not force:
            logger.info("Enqueue skipped video_id=%s reason=already_queued", video_doc_id)
            return {
                "queued": False,
                "skipped": True,
                "reason": "already_queued",
                "video_id": video_doc_id,
                "commit_id": commit_id,
                "status": "running",
            }

        future = _EXECUTOR.submit(
            _run_commit_pipeline,
            commit_id,
            video_doc_id,
            languages_requested,
        )
        _FUTURES[video_doc_id] = future
        logger.info(
            "Enqueue accepted video_id=%s commit_id=%s workers=%s",
            video_doc_id,
            commit_id,
            MAX_WORKERS,
        )

    return {
        "queued": True,
        "skipped": False,
        "video_id": video_doc_id,
        "commit_id": commit_id,
        "status": "queued",
        "languages_requested": languages_requested,
    }
