"""Asynchronous pipeline orchestration for script prep + source-video finalization."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlparse

import requests

from services.caption_service import build_srt_from_timeline
from services.firebase_service import (
    build_video_doc_id,
    get_commit_by_id,
    get_repo,
    get_video,
    update_commit_feature_demo,
    update_video_status,
    upsert_video_doc,
)
from services.media_generation_service import (
    generate_browser_use_goal,
    parse_target_languages,
)
from services.feature_video_recorder import record_feature_demo_sync
from services.gemini_tts_service import synthesize_with_gemini_tts
from services.gemini_video_service import GeminiVideoError, generate_veo_clip
from services.media_generation_service import (
    generate_narration_script,
    generate_scene_script,
    generate_shot_plan,
)
from services.storage_service import upload_file
from services.video_stitch_service import (
    burn_captions,
    concat_videos,
    mix_with_narration,
    normalize_video,
    probe_video,
    render_title_card,
    trim_video,
)

MAX_WORKERS = max(1, int(os.environ.get("PIPELINE_MAX_WORKERS", "2")))
_EXECUTOR = ThreadPoolExecutor(
    max_workers=MAX_WORKERS, thread_name_prefix="diffcast-pipeline"
)
_FUTURES: dict[str, Future] = {}
_FEATURE_DEMO_FUTURES: dict[str, Future] = {}
_LOCK = Lock()
_FEATURE_DEMO_LOCK = Lock()
logger = logging.getLogger(__name__)


def _allowed_local_roots() -> list[Path]:
    raw = os.environ.get("PIPELINE_ALLOWED_LOCAL_VIDEO_ROOTS", "").strip()
    values = [item.strip() for item in raw.split(",") if item.strip()]
    if not values:
        backend_root = Path(__file__).resolve().parents[1]
        values = [str(backend_root / "tests" / "videos")]
    return [Path(v).expanduser().resolve() for v in values]


def _is_allowed_local_path(candidate: Path) -> bool:
    resolved = candidate.resolve()
    for root in _allowed_local_roots():
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _source_key(source_video: dict[str, Any]) -> str:
    kind = str(source_video.get("kind", "")).strip()
    uri = str(source_video.get("uri", "")).strip()
    return f"{kind}:{uri}"


def _resolve_source_video_to_local(
    source_video: dict[str, Any],
    workspace_dir: str | Path,
) -> Path:
    kind = str(source_video.get("kind", "")).strip().lower()
    uri = str(source_video.get("uri", "")).strip()
    if not kind or not uri:
        raise ValueError("source_video.kind and source_video.uri are required")

    work = Path(workspace_dir)
    work.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(uri)
    ext = Path(parsed.path).suffix or ".mp4"
    out = work / f"source{ext}"

    if kind == "local_path":
        candidate = Path(uri).expanduser()
        if not candidate.is_absolute():
            backend_root = Path(__file__).resolve().parents[1]
            repo_root = backend_root.parent
            relative_candidate = candidate
            candidates = [
                (backend_root / relative_candidate).resolve(),
                (repo_root / relative_candidate).resolve(),
            ]
            existing = next((path for path in candidates if path.exists()), None)
            candidate = existing or candidates[0]
        if not candidate.exists():
            raise FileNotFoundError(f"source video not found: {candidate}")
        if not _is_allowed_local_path(candidate):
            raise ValueError(
                f"source video path is not within allowed roots: {candidate}"
            )
        shutil.copyfile(candidate, out)
        return out

    if kind == "https_url":
        response = requests.get(uri, timeout=60)
        response.raise_for_status()
        out.write_bytes(response.content)
        return out

    if kind == "gs_uri":
        raise ValueError(
            "gs_uri ingestion is not implemented yet; use local_path or https_url"
        )

    raise ValueError(f"unsupported source_video.kind: {kind}")


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
        "script": None,
        "enhancement_plan": None,
        "source_video": None,
        "base_video_url": None,
        "enhanced_video_url": None,
        "fallback_used": False,
        "tracks": {},
        "updated_at": datetime.utcnow(),
    }


def _run_prepare_pipeline(
    commit_doc_id: str,
    video_doc_id: str,
    languages_requested: list[str],
) -> None:
    logger.info(
        "Prepare pipeline started commit_id=%s video_id=%s languages=%s",
        commit_doc_id,
        video_doc_id,
        languages_requested,
    )
    commit_doc = get_commit_by_id(commit_doc_id)
    if not commit_doc:
        logger.error(
            "Prepare pipeline commit missing commit_id=%s video_id=%s",
            commit_doc_id,
            video_doc_id,
        )
        update_video_status(
            video_doc_id=video_doc_id,
            status="failed",
            stage="error",
            error="Commit not found",
        )
        return

    current_stage = "script"
    try:
        logger.info("Prepare stage=%s video_id=%s", current_stage, video_doc_id)
        update_video_status(
            video_doc_id=video_doc_id,
            status="running",
            stage="script",
            error=None,
            extra_fields={"languages_requested": languages_requested},
        )

        script = generate_scene_script(commit_doc)
        target_duration = int(os.environ.get("PIPELINE_TARGET_DURATION_SEC", "26"))
        shot_plan = generate_shot_plan(
            script=script, target_duration_sec=target_duration
        )

        update_video_status(
            video_doc_id=video_doc_id,
            status="awaiting_input",
            stage="awaiting_source_video",
            error=None,
            extra_fields={
                "script": script,
                "enhancement_plan": shot_plan,
            },
        )
        logger.info("Prepare pipeline awaiting input video_id=%s", video_doc_id)
    except Exception as exc:
        logger.exception(
            "Prepare pipeline failed commit_id=%s video_id=%s stage=%s languages=%s",
            commit_doc_id,
            video_doc_id,
            current_stage,
            languages_requested,
        )
        update_video_status(
            video_doc_id=video_doc_id,
            status="failed",
            stage="error",
            error=str(exc),
        )
    finally:
        with _LOCK:
            _FUTURES.pop(video_doc_id, None)


def _build_veo_prompts(shot_plan: dict[str, Any]) -> list[tuple[str, float]]:
    prompts: list[tuple[str, float]] = []
    opener_prompt = str(shot_plan.get("opener_prompt", "")).strip()
    outro_prompt = str(shot_plan.get("outro_prompt", "")).strip()
    transitions = (
        shot_plan.get("transition_prompts")
        if isinstance(shot_plan.get("transition_prompts"), list)
        else []
    )

    if opener_prompt:
        prompts.append((opener_prompt, 4.0))
    for item in transitions[:2]:
        if str(item).strip():
            prompts.append((str(item).strip(), 4.0))
    if outro_prompt:
        prompts.append((outro_prompt, 4.0))
    return prompts[:4]


def _upload_finalize_assets(
    repo_id: str,
    sha_short: str,
    source_video_path: str,
    normalized_source_path: str,
    enhanced_video_path: str,
    track_payloads: dict[str, dict[str, Any]],
) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, dict[str, Any]]]:
    source_upload = upload_file(
        local_path=source_video_path,
        destination_path=f"videos/{repo_id}/{sha_short}/source.mp4",
        content_type="video/mp4",
    )
    base_upload = upload_file(
        local_path=normalized_source_path,
        destination_path=f"videos/{repo_id}/{sha_short}/base.mp4",
        content_type="video/mp4",
    )
    enhanced_upload = upload_file(
        local_path=enhanced_video_path,
        destination_path=f"videos/{repo_id}/{sha_short}/enhanced.mp4",
        content_type="video/mp4",
    )

    tracks: dict[str, dict[str, Any]] = {}
    for language, payload in track_payloads.items():
        if payload.get("status") != "completed":
            tracks[language] = payload
            continue

        audio_upload = upload_file(
            local_path=payload["audio_path"],
            destination_path=f"videos/{repo_id}/{sha_short}/tracks/{language}/voice.wav",
            content_type="audio/wav",
        )
        srt_upload = upload_file(
            local_path=payload["captions_path"],
            destination_path=f"videos/{repo_id}/{sha_short}/tracks/{language}/captions.srt",
            content_type="application/x-subrip",
        )
        final_upload = upload_file(
            local_path=payload["final_video_path"],
            destination_path=f"videos/{repo_id}/{sha_short}/tracks/{language}/final.mp4",
            content_type="video/mp4",
        )
        tracks[language] = {
            "status": "completed",
            "error": None,
            "voice_script": payload.get("voice_script"),
            "duration_sec": payload.get("duration_sec"),
            "voice_provider": "gemini_tts",
            "caption_mode": "burned_plus_srt",
            "mix_meta": payload.get("mix_meta"),
            "audio_url": audio_upload["url"],
            "captions_url": srt_upload["url"],
            "final_video_url": final_upload["url"],
            "final_video_meta": payload.get("final_video_meta"),
        }

    return source_upload, base_upload, enhanced_upload, tracks


def _ensure_ingest_prerequisites(
    commit_doc_id: str,
    video_doc_id: str,
    commit_doc: dict[str, Any],
    existing: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Ensure ingest has script and enhancement_plan; backfill if needed."""
    script = existing.get("script")
    shot_plan = existing.get("enhancement_plan")
    if script and shot_plan:
        return script, shot_plan

    logger.warning(
        "Ingest pipeline missing prerequisites; attempting backfill commit_id=%s video_id=%s has_script=%s has_plan=%s",
        commit_doc_id,
        video_doc_id,
        bool(script),
        bool(shot_plan),
    )
    try:
        if not script:
            script = generate_scene_script(commit_doc)
        if not shot_plan:
            target_duration = int(os.environ.get("PIPELINE_TARGET_DURATION_SEC", "26"))
            shot_plan = generate_shot_plan(
                script=script, target_duration_sec=target_duration
            )

        update_video_status(
            video_doc_id=video_doc_id,
            status="running",
            stage="script",
            error=None,
            extra_fields={
                "script": script,
                "enhancement_plan": shot_plan,
            },
        )
        logger.info(
            "Ingest prerequisites backfilled commit_id=%s video_id=%s",
            commit_doc_id,
            video_doc_id,
        )
        return script, shot_plan
    except Exception as exc:
        logger.exception(
            "Ingest prerequisite backfill failed commit_id=%s video_id=%s has_script=%s has_plan=%s",
            commit_doc_id,
            video_doc_id,
            bool(existing.get("script")),
            bool(existing.get("enhancement_plan")),
        )
        update_video_status(
            video_doc_id=video_doc_id,
            status="failed",
            stage="error",
            error=str(exc),
        )
        return None


def _run_ingest_pipeline(
    commit_doc_id: str,
    video_doc_id: str,
    source_video: dict[str, Any],
    languages_requested: list[str],
    style_profile: str,
) -> None:
    logger.info(
        "Ingest pipeline started commit_id=%s video_id=%s source_kind=%s languages=%s style_profile=%s",
        commit_doc_id,
        video_doc_id,
        source_video.get("kind"),
        languages_requested,
        style_profile,
    )
    commit_doc = get_commit_by_id(commit_doc_id)
    if not commit_doc:
        logger.error(
            "Ingest pipeline commit missing commit_id=%s video_id=%s",
            commit_doc_id,
            video_doc_id,
        )
        update_video_status(
            video_doc_id=video_doc_id,
            status="failed",
            stage="error",
            error="Commit not found",
        )
        return

    existing = get_video(video_doc_id) or {}
    prerequisites = _ensure_ingest_prerequisites(
        commit_doc_id=commit_doc_id,
        video_doc_id=video_doc_id,
        commit_doc=commit_doc,
        existing=existing,
    )
    if not prerequisites:
        return
    script, shot_plan = prerequisites

    workspace_dir = Path(tempfile.mkdtemp(prefix="diffcast-finalize-"))
    fallback_used = False
    current_stage = "normalize_video"
    try:
        logger.info("Ingest stage=%s video_id=%s", current_stage, video_doc_id)
        update_video_status(
            video_doc_id=video_doc_id,
            status="running",
            stage="normalize_video",
            error=None,
            extra_fields={
                "source_video": source_video,
                "languages_requested": languages_requested,
                "style_profile": style_profile,
            },
        )

        source_local = _resolve_source_video_to_local(
            source_video=source_video, workspace_dir=workspace_dir
        )
        source_meta = probe_video(source_local)
        normalized_source = workspace_dir / "source_normalized.mp4"
        base_meta = normalize_video(source_local, normalized_source)

        current_stage = "veo_generate"
        logger.info("Ingest stage=%s video_id=%s", current_stage, video_doc_id)
        update_video_status(
            video_doc_id=video_doc_id,
            status="running",
            stage="veo_generate",
            error=None,
            extra_fields={
                "source_video": {**source_video, **source_meta},
                "video_meta": base_meta,
            },
        )

        veo_enabled = os.environ.get("PIPELINE_ENABLE_VEO", "true").lower() == "true"
        veo_clips: list[Path] = []
        if veo_enabled:
            for idx, (prompt, duration_sec) in enumerate(
                _build_veo_prompts(shot_plan), start=1
            ):
                try:
                    clip_path = workspace_dir / f"veo_{idx:02d}.mp4"
                    result = generate_veo_clip(
                        prompt=prompt,
                        output_path=clip_path,
                        duration_sec=max(4, min(int(round(duration_sec)), 8)),
                    )
                    normalized_insert = workspace_dir / f"veo_{idx:02d}_norm.mp4"
                    normalize_video(result["path"], normalized_insert)
                    veo_clips.append(normalized_insert)
                except GeminiVideoError:
                    logger.exception("Veo clip generation failed index=%s", idx)
                    fallback_used = True
                    break

        current_stage = "stitch"
        logger.info(
            "Ingest stage=%s video_id=%s fallback_used=%s",
            current_stage,
            video_doc_id,
            fallback_used,
        )
        update_video_status(
            video_doc_id=video_doc_id, status="running", stage="stitch", error=None
        )

        opener_card = workspace_dir / "opener.mp4"
        outro_card = workspace_dir / "outro.mp4"
        render_title_card(
            script.get("title", "Product Update"), opener_card, duration_sec=2.0
        )
        render_title_card("Now live", outro_card, duration_sec=2.0)

        trimmed_source = workspace_dir / "source_trimmed.mp4"
        trim_video(
            normalized_source,
            trimmed_source,
            duration_sec=max(12.0, min(base_meta["duration_sec"], 28.0)),
        )

        segment_paths: list[str | Path] = [opener_card]
        if not fallback_used and veo_clips:
            segment_paths.append(veo_clips[0])
        segment_paths.append(trimmed_source)
        if not fallback_used and len(veo_clips) > 1:
            segment_paths.extend(veo_clips[1:])
        segment_paths.append(outro_card)

        enhanced_video = workspace_dir / "enhanced_visual.mp4"
        enhanced_meta = concat_videos(segment_paths, enhanced_video)

        current_stage = "voiceover"
        logger.info(
            "Ingest stage=%s video_id=%s languages=%s",
            current_stage,
            video_doc_id,
            languages_requested,
        )
        update_video_status(
            video_doc_id=video_doc_id,
            status="running",
            stage="voiceover",
            error=None,
            extra_fields={
                "fallback_used": fallback_used,
                "enhancement_plan": shot_plan,
            },
        )

        track_payloads: dict[str, dict[str, Any]] = {}
        for language in languages_requested:
            try:
                narration_text = generate_narration_script(
                    shot_plan=shot_plan, language=language
                )
                audio_path = workspace_dir / f"narration_{language}.wav"
                tts_result = synthesize_with_gemini_tts(
                    text=narration_text,
                    output_path=audio_path,
                    language=language,
                )

                srt_text = build_srt_from_timeline(shot_plan.get("timeline", []))
                captions_path = workspace_dir / f"captions_{language}.srt"
                captions_path.write_text(srt_text, encoding="utf-8")

                mixed_video = workspace_dir / f"mixed_{language}.mp4"
                mix_meta = mix_with_narration(enhanced_video, audio_path, mixed_video)

                final_video_path = workspace_dir / f"final_{language}.mp4"
                final_meta = burn_captions(mixed_video, captions_path, final_video_path)

                track_payloads[language] = {
                    "status": "completed",
                    "error": None,
                    "voice_script": narration_text,
                    "duration_sec": enhanced_meta.get("duration_sec"),
                    "audio_path": str(audio_path),
                    "captions_path": str(captions_path),
                    "final_video_path": str(final_video_path),
                    "mix_meta": mix_meta,
                    "final_video_meta": final_meta,
                    "voice_model": tts_result.get("model"),
                }
            except Exception as exc:
                logger.exception(
                    "Track generation failed video_id=%s language=%s workspace=%s",
                    video_doc_id,
                    language,
                    workspace_dir,
                )
                track_payloads[language] = {
                    "status": "failed",
                    "error": str(exc),
                    "voice_script": None,
                    "duration_sec": enhanced_meta.get("duration_sec"),
                    "audio_path": None,
                    "captions_path": None,
                    "final_video_path": None,
                    "mix_meta": None,
                    "final_video_meta": None,
                }

        current_stage = "upload"
        logger.info("Ingest stage=%s video_id=%s", current_stage, video_doc_id)
        update_video_status(
            video_doc_id=video_doc_id, status="running", stage="upload", error=None
        )

        source_upload, base_upload, enhanced_upload, uploaded_tracks = (
            _upload_finalize_assets(
                repo_id=commit_doc["repo_id"],
                sha_short=commit_doc["sha_short"],
                source_video_path=str(source_local),
                normalized_source_path=str(normalized_source),
                enhanced_video_path=str(enhanced_video),
                track_payloads=track_payloads,
            )
        )

        successful_tracks = [
            lang
            for lang, track in uploaded_tracks.items()
            if track.get("status") == "completed"
        ]
        final_status = "completed" if successful_tracks else "failed"
        final_stage = "done" if successful_tracks else "error"
        final_error = (
            None
            if successful_tracks
            else "No language tracks were generated successfully"
        )
        current_stage = final_stage

        update_video_status(
            video_doc_id=video_doc_id,
            status=final_status,
            stage=final_stage,
            error=final_error,
            extra_fields={
                "source_video": {
                    **(source_video or {}),
                    **source_meta,
                    "url": source_upload["url"],
                },
                "base_video_url": base_upload["url"],
                "enhanced_video_url": enhanced_upload["url"],
                "tracks": uploaded_tracks,
                "fallback_used": fallback_used,
                "video_meta": enhanced_meta,
            },
        )
        logger.info(
            "Ingest pipeline finished commit_id=%s video_id=%s status=%s successful_tracks=%s",
            commit_doc_id,
            video_doc_id,
            final_status,
            successful_tracks,
        )
    except Exception as exc:
        logger.exception(
            "Ingest/finalize pipeline failed commit_id=%s video_id=%s stage=%s source_kind=%s source_uri=%s languages=%s style_profile=%s workspace=%s",
            commit_doc_id,
            video_doc_id,
            current_stage,
            source_video.get("kind"),
            source_video.get("uri"),
            languages_requested,
            style_profile,
            workspace_dir,
        )
        update_video_status(
            video_doc_id=video_doc_id,
            status="failed",
            stage="error",
            error=str(exc),
        )
    finally:
        with _LOCK:
            _FUTURES.pop(video_doc_id, None)
        shutil.rmtree(workspace_dir, ignore_errors=True)


def enqueue_commit_pipeline(
    commit_id: str,
    languages: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Queue script-preparation job for a commit."""
    logger.info(
        "Enqueue prepare requested commit_id=%s force=%s languages=%s",
        commit_id,
        force,
        languages,
    )
    commit_doc = get_commit_by_id(commit_id)
    if not commit_doc:
        raise ValueError(f"Commit not found: {commit_id}")

    languages_requested = parse_target_languages(languages)
    video_doc_id = build_video_doc_id(commit_doc["repo_full_name"], commit_doc["sha"])
    existing = get_video(video_doc_id)
    if (
        existing
        and existing.get("status") in {"running", "awaiting_input", "completed"}
        and not force
    ):
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
            return {
                "queued": False,
                "skipped": True,
                "reason": "already_queued",
                "video_id": video_doc_id,
                "commit_id": commit_id,
                "status": "running",
            }

        future = _EXECUTOR.submit(
            _run_prepare_pipeline, commit_id, video_doc_id, languages_requested
        )
        _FUTURES[video_doc_id] = future

    return {
        "queued": True,
        "skipped": False,
        "video_id": video_doc_id,
        "commit_id": commit_id,
        "status": "queued",
        "languages_requested": languages_requested,
        "stage": "script",
    }


def enqueue_ingest_pipeline(
    commit_id: str,
    source_video: dict[str, Any],
    languages: list[str] | None = None,
    style_profile: str = "apple_keynote_v1",
    force: bool = False,
) -> dict[str, Any]:
    """Queue finalization pipeline from source video."""
    commit_doc = get_commit_by_id(commit_id)
    if not commit_doc:
        raise ValueError(f"Commit not found: {commit_id}")

    languages_requested = parse_target_languages(languages)
    video_doc_id = build_video_doc_id(commit_doc["repo_full_name"], commit_doc["sha"])
    existing = get_video(video_doc_id)

    if existing and existing.get("status") == "running" and not force:
        return {
            "queued": False,
            "skipped": True,
            "reason": "already_running",
            "video_id": video_doc_id,
            "commit_id": commit_id,
            "status": existing.get("status"),
        }

    if existing and existing.get("status") == "completed" and not force:
        previous_source = existing.get("source_video") or {}
        if _source_key(previous_source) == _source_key(source_video):
            return {
                "queued": False,
                "skipped": True,
                "reason": "already_completed_for_source",
                "video_id": video_doc_id,
                "commit_id": commit_id,
                "status": "completed",
            }

    upsert_video_doc(
        video_doc_id=video_doc_id,
        payload={
            **(
                _base_video_doc(video_doc_id, commit_doc, languages_requested)
                if not existing
                else existing
            ),
            "status": "queued",
            "stage": "normalize_video",
            "source_video": source_video,
            "languages_requested": languages_requested,
            "style_profile": style_profile,
            "error": None,
            "tracks": {},
            "updated_at": datetime.utcnow(),
        },
    )

    with _LOCK:
        running_future = _FUTURES.get(video_doc_id)
        if running_future and not running_future.done() and not force:
            return {
                "queued": False,
                "skipped": True,
                "reason": "already_queued",
                "video_id": video_doc_id,
                "commit_id": commit_id,
                "status": "running",
            }

        future = _EXECUTOR.submit(
            _run_ingest_pipeline,
            commit_id,
            video_doc_id,
            source_video,
            languages_requested,
            style_profile,
        )
        _FUTURES[video_doc_id] = future

    return {
        "queued": True,
        "skipped": False,
        "video_id": video_doc_id,
        "commit_id": commit_id,
        "status": "running",
        "stage": "normalize_video",
        "languages_requested": languages_requested,
    }


def _run_feature_demo_pipeline(commit_doc_id: str) -> None:
    """Run feature demo pipeline: generate goal -> record -> upload -> update commit."""
    logger.info("Feature demo pipeline started commit_id=%s", commit_doc_id)
    commit_doc = get_commit_by_id(commit_doc_id)
    if not commit_doc:
        logger.error(
            "Feature demo failed: commit not found commit_id=%s", commit_doc_id
        )
        update_commit_feature_demo(
            commit_doc_id=commit_doc_id,
            status="failed",
            error=f"Commit not found: {commit_doc_id}",
        )
        return

    repo_full_name = commit_doc.get("repo_full_name", "")
    repo = get_repo(repo_full_name)
    website_url = (repo or {}).get("website_url") or ""
    if not website_url or not website_url.strip():
        logger.error(
            "Feature demo failed: repo has no website_url commit_id=%s repo=%s",
            commit_doc_id,
            repo_full_name,
        )
        update_commit_feature_demo(
            commit_doc_id=commit_doc_id,
            status="failed",
            error=f"Repo {repo_full_name} has no website_url. Set it via PATCH /api/repos/{repo_full_name}",
        )
        return

    website_url = website_url.strip()
    workspace_dir: Path | None = None

    try:
        update_commit_feature_demo(commit_doc_id=commit_doc_id, status="running")

        goal = generate_browser_use_goal(commit_doc)
        logger.info(
            "Feature demo goal generated commit_id=%s goal_len=%s",
            commit_doc_id,
            len(goal),
        )

        workspace_dir = Path(tempfile.mkdtemp(prefix="diffcast-feature-demo-"))
        video_path = record_feature_demo_sync(
            website_url=website_url,
            feature_description=goal,
            output_dir=workspace_dir,
            headless=True,
        )

        if not video_path.exists():
            raise FileNotFoundError(f"Recorded video not found: {video_path}")

        repo_id_val = commit_doc.get("repo_id", "")
        sha_short = commit_doc.get("sha_short", commit_doc_id.split("_")[-1])
        ext = video_path.suffix or ".mp4"
        content_type = "video/mp4" if ext == ".mp4" else "video/webm"
        dest_path = f"feature_demos/{repo_id_val}/{sha_short}/demo{ext}"

        upload_result = upload_file(
            local_path=video_path,
            destination_path=dest_path,
            content_type=content_type,
        )
        video_url = upload_result.get("url", "")

        update_commit_feature_demo(
            commit_doc_id=commit_doc_id,
            status="completed",
            video_url=video_url,
            error=None,
            goal=goal,
        )
        logger.info(
            "Feature demo completed commit_id=%s video_url=%s",
            commit_doc_id,
            video_url,
        )
    except Exception as exc:
        logger.exception("Feature demo failed commit_id=%s", commit_doc_id)
        update_commit_feature_demo(
            commit_doc_id=commit_doc_id,
            status="failed",
            error=str(exc),
        )
    finally:
        with _FEATURE_DEMO_LOCK:
            _FEATURE_DEMO_FUTURES.pop(commit_doc_id, None)
        if workspace_dir and workspace_dir.exists():
            shutil.rmtree(workspace_dir, ignore_errors=True)
            logger.info("Feature demo workspace cleaned commit_id=%s", commit_doc_id)


def enqueue_feature_demo_pipeline(
    commit_id: str, force: bool = False
) -> dict[str, Any]:
    """Queue feature demo pipeline: generate goal, record via browser-use, upload, save to commit."""
    logger.info(
        "Feature demo enqueue requested commit_id=%s force=%s", commit_id, force
    )
    commit_doc = get_commit_by_id(commit_id)
    if not commit_doc:
        raise ValueError(f"Commit not found: {commit_id}")

    existing_status = commit_doc.get("feature_demo_status")
    if existing_status in {"running", "completed"} and not force:
        logger.info(
            "Feature demo enqueue skipped commit_id=%s existing_status=%s",
            commit_id,
            existing_status,
        )
        return {
            "queued": False,
            "skipped": True,
            "reason": "already_running_or_completed",
            "commit_id": commit_id,
            "status": existing_status,
        }

    with _FEATURE_DEMO_LOCK:
        running_future = _FEATURE_DEMO_FUTURES.get(commit_id)
        if running_future and not running_future.done() and not force:
            logger.info(
                "Feature demo enqueue skipped commit_id=%s reason=already_queued",
                commit_id,
            )
            return {
                "queued": False,
                "skipped": True,
                "reason": "already_queued",
                "commit_id": commit_id,
                "status": "running",
            }

        future = _EXECUTOR.submit(_run_feature_demo_pipeline, commit_id)
        _FEATURE_DEMO_FUTURES[commit_id] = future
        logger.info("Feature demo enqueued commit_id=%s", commit_id)

    return {
        "queued": True,
        "skipped": False,
        "commit_id": commit_id,
        "status": "queued",
    }
