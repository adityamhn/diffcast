"""Unified pipeline orchestration for feature video generation.

Flow: goal → demo → script → snapshots → veo → stitch → voice → captions → finalize
"""

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

from services.caption_service import build_srt_from_timeline
from services.feature_video_recorder import record_feature_demo_sync
from services.firebase_service import (
    build_video_doc_id,
    get_commit_by_id,
    get_repo,
    get_video,
    update_commit_feature_demo,
    update_video_status,
    upsert_video_doc,
)
from services.gemini_tts_service import synthesize_with_gemini_tts
from services.gemini_video_service import GeminiVideoError, generate_veo_clip
from services.media_generation_service import (
    generate_browser_use_goal,
    generate_narration_script,
    generate_scene_script,
    generate_shot_plan,
    parse_target_languages,
)
from services.snapshot_service import extract_snapshots
from services.storage_service import upload_file
from services.video_stitch_service import (
    assemble_feature_video,
    burn_captions,
    mix_with_narration,
    normalize_video,
    probe_video,
)

MAX_WORKERS = max(1, int(os.environ.get("PIPELINE_MAX_WORKERS", "2")))
_EXECUTOR = ThreadPoolExecutor(
    max_workers=MAX_WORKERS, thread_name_prefix="diffcast-pipeline"
)
_FUTURES: dict[str, Future] = {}
_LOCK = Lock()
logger = logging.getLogger(__name__)

# Debug directory for saving intermediate audio/video files
# Set PIPELINE_DEBUG_DIR to a path to persist debug artifacts
DEBUG_DIR = os.environ.get("PIPELINE_DEBUG_DIR", "").strip()


def _save_debug_file(src_path: str | Path, label: str, video_id: str) -> None:
    """Copy a file to the debug directory if enabled."""
    if not DEBUG_DIR:
        return
    try:
        debug_root = Path(DEBUG_DIR)
        debug_root.mkdir(parents=True, exist_ok=True)
        src = Path(src_path)
        if not src.exists():
            logger.warning("Debug file not found: %s", src)
            return
        # Create a subfolder per video_id
        video_debug_dir = debug_root / video_id
        video_debug_dir.mkdir(parents=True, exist_ok=True)
        dest = video_debug_dir / f"{label}{src.suffix}"
        shutil.copy2(src, dest)
        logger.info("Debug file saved: %s -> %s", src, dest)
    except Exception as exc:
        logger.warning("Failed to save debug file %s: %s", label, exc)


def _extract_audio_track(video_path: str | Path, output_path: str | Path) -> bool:
    """Extract audio track from video to WAV for debugging."""
    import subprocess
    try:
        cmd = [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn", "-acodec", "pcm_s16le", "-ar", "48000", "-ac", "2",
            str(output_path)
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            logger.warning("Audio extraction failed: %s", proc.stderr[:500] if proc.stderr else "unknown")
            return False
        return True
    except Exception as exc:
        logger.warning("Audio extraction error: %s", exc)
        return False


# Pipeline stages for the unified flow
PIPELINE_STAGES = [
    "goal",       # Generate browser-use goal from commit diff
    "demo",       # Record demo video using browser-use
    "script",     # Generate scene script and shot plan with 2 clip prompts
    "snapshots",  # Extract 2 frames from demo video
    "veo",        # Generate 2x6s Veo clips using snapshots + prompts
    "stitch",     # Assemble: Opener → Demo → 1 closing clip
    "voice",      # Generate voiceover per language
    "captions",   # Generate SRT captions per language
    "finalize",   # Burn captions, mix audio, upload per language
    "done",       # Mark complete
]


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
        "stage": "goal",
        "error": None,
        "languages_requested": languages_requested,
        "goal": None,
        "demo_video_path": None,
        "demo_video_url": None,
        "script": None,
        "shot_plan": None,
        "snapshots": None,
        "veo_clips": None,
        "enhanced_video_url": None,
        "tracks": {},
        "updated_at": datetime.utcnow(),
    }


def _upload_track_assets(
    repo_id: str,
    sha_short: str,
    demo_video_path: str,
    enhanced_video_path: str,
    track_payloads: dict[str, dict[str, Any]],
) -> tuple[dict[str, str], dict[str, str], dict[str, dict[str, Any]]]:
    """Upload demo, enhanced video, and per-language tracks to storage."""
    demo_upload = upload_file(
        local_path=demo_video_path,
        destination_path=f"videos/{repo_id}/{sha_short}/demo.mp4",
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

    return demo_upload, enhanced_upload, tracks


def _run_unified_pipeline(
    commit_doc_id: str,
    video_doc_id: str,
    languages_requested: list[str],
) -> None:
    """Run the unified pipeline: goal → demo → script → snapshots → veo → stitch → voice → captions → finalize.

    This is the main pipeline that generates a complete feature video from a commit.
    """
    logger.info(
        "Unified pipeline started commit_id=%s video_id=%s languages=%s",
        commit_doc_id,
        video_doc_id,
        languages_requested,
    )

    commit_doc = get_commit_by_id(commit_doc_id)
    if not commit_doc:
        logger.error("Pipeline commit missing commit_id=%s video_id=%s", commit_doc_id, video_doc_id)
        update_video_status(
            video_doc_id=video_doc_id,
            status="failed",
            stage="error",
            error="Commit not found",
        )
        return

    # Get repo for website_url
    repo_full_name = commit_doc.get("repo_full_name", "")
    repo = get_repo(repo_full_name)
    website_url = (repo or {}).get("website_url") or ""
    if not website_url.strip():
        logger.error(
            "Pipeline failed: repo has no website_url commit_id=%s repo=%s",
            commit_doc_id,
            repo_full_name,
        )
        update_video_status(
            video_doc_id=video_doc_id,
            status="failed",
            stage="error",
            error=f"Repo {repo_full_name} has no website_url",
        )
        return

    website_url = website_url.strip()
    workspace_dir = Path(tempfile.mkdtemp(prefix="diffcast-unified-"))
    current_stage = "goal"

    try:
        # ========== STAGE: goal ==========
        logger.info("Pipeline stage=%s video_id=%s", current_stage, video_doc_id)
        update_video_status(
            video_doc_id=video_doc_id,
            status="running",
            stage="goal",
            error=None,
            extra_fields={"languages_requested": languages_requested},
        )

        goal = generate_browser_use_goal(commit_doc)
        logger.info("Goal generated commit_id=%s goal_len=%s", commit_doc_id, len(goal))

        # ========== STAGE: demo ==========
        current_stage = "demo"
        logger.info("Pipeline stage=%s video_id=%s", current_stage, video_doc_id)
        update_video_status(
            video_doc_id=video_doc_id,
            status="running",
            stage="demo",
            error=None,
            extra_fields={"goal": goal},
        )

        demo_output_dir = workspace_dir / "demo"
        demo_video_path = record_feature_demo_sync(
            website_url=website_url,
            feature_description=goal,
            output_dir=demo_output_dir,
            headless=True,
        )
        if not demo_video_path.exists():
            raise FileNotFoundError(f"Demo video not found: {demo_video_path}")

        # Normalize the demo video to standard format
        demo_normalized = workspace_dir / "demo_normalized.mp4"
        demo_meta = normalize_video(demo_video_path, demo_normalized)
        demo_duration = demo_meta["duration_sec"]
        logger.info(
            "Demo recorded commit_id=%s duration=%.2fs path=%s",
            commit_doc_id,
            demo_duration,
            demo_normalized,
        )

        # Save debug: raw and normalized demo video
        _save_debug_file(demo_video_path, "demo_raw", video_doc_id)
        _save_debug_file(demo_normalized, "demo_normalized", video_doc_id)

        # Also update commit with feature demo info
        update_commit_feature_demo(
            commit_doc_id=commit_doc_id,
            status="completed",
            goal=goal,
        )

        # ========== STAGE: script ==========
        current_stage = "script"
        logger.info("Pipeline stage=%s video_id=%s", current_stage, video_doc_id)
        update_video_status(
            video_doc_id=video_doc_id,
            status="running",
            stage="script",
            error=None,
            extra_fields={"demo_video_duration_sec": demo_duration},
        )

        script = generate_scene_script(commit_doc)
        target_duration = int(os.environ.get("PIPELINE_TARGET_DURATION_SEC", "28"))
        shot_plan = generate_shot_plan(
            script=script,
            target_duration_sec=target_duration,
            demo_video_duration_sec=demo_duration,
        )
        logger.info(
            "Script and shot plan generated commit_id=%s clip_prompts=%d timeline_segments=%d",
            commit_doc_id,
            len(shot_plan.get("clip_prompts", [])),
            len(shot_plan.get("timeline", [])),
        )

        # ========== STAGE: snapshots ==========
        current_stage = "snapshots"
        logger.info("Pipeline stage=%s video_id=%s", current_stage, video_doc_id)
        update_video_status(
            video_doc_id=video_doc_id,
            status="running",
            stage="snapshots",
            error=None,
            extra_fields={
                "script": script,
                "shot_plan": shot_plan,
            },
        )

        snapshots_dir = workspace_dir / "snapshots"
        snapshot_paths = extract_snapshots(
            video_path=demo_normalized,
            output_dir=snapshots_dir,
            num_snapshots=2,
            strategy="uniform",
        )
        logger.info(
            "Snapshots extracted commit_id=%s count=%d paths=%s",
            commit_doc_id,
            len(snapshot_paths),
            [str(p) for p in snapshot_paths],
        )

        # ========== STAGE: veo ==========
        current_stage = "veo"
        logger.info("Pipeline stage=%s video_id=%s", current_stage, video_doc_id)
        update_video_status(
            video_doc_id=video_doc_id,
            status="running",
            stage="veo",
            error=None,
            extra_fields={"snapshots": [str(p) for p in snapshot_paths]},
        )

        veo_enabled = os.environ.get("PIPELINE_ENABLE_VEO", "true").lower() == "true"
        veo_clips: list[Path] = []
        clip_prompts = shot_plan.get("clip_prompts", [])

        if veo_enabled and clip_prompts:
            for idx, clip_info in enumerate(clip_prompts[:2]):
                try:
                    prompt = clip_info.get("prompt", "")
                    snapshot_index = clip_info.get("snapshot_index", idx)
                    reference_image = (
                        snapshot_paths[snapshot_index]
                        if snapshot_index < len(snapshot_paths)
                        else snapshot_paths[0]
                    )

                    clip_path = workspace_dir / f"veo_{idx:02d}.mp4"
                    result = generate_veo_clip(
                        prompt=prompt,
                        output_path=clip_path,
                        duration_sec=6,  # 6 seconds per clip
                        reference_image_path=reference_image,
                    )

                    # Normalize the Veo clip
                    normalized_clip = workspace_dir / f"veo_{idx:02d}_norm.mp4"
                    normalize_video(result["path"], normalized_clip)
                    veo_clips.append(normalized_clip)

                    # Save debug files: raw Veo clip + extracted audio
                    _save_debug_file(result["path"], f"veo_{idx:02d}_raw", video_doc_id)
                    _save_debug_file(normalized_clip, f"veo_{idx:02d}_norm", video_doc_id)
                    veo_audio_debug = workspace_dir / f"veo_{idx:02d}_audio.wav"
                    if _extract_audio_track(result["path"], veo_audio_debug):
                        _save_debug_file(veo_audio_debug, f"veo_{idx:02d}_audio", video_doc_id)

                    logger.info(
                        "Veo clip generated index=%d role=%s path=%s",
                        idx,
                        clip_info.get("role", "unknown"),
                        normalized_clip,
                    )
                except GeminiVideoError:
                    logger.exception("Veo clip generation failed index=%d", idx)
                    # Continue with remaining clips
                    continue

        if len(veo_clips) < 2:
            logger.warning(
                "Only %d Veo clips generated (expected 2), continuing with available clips",
                len(veo_clips),
            )

        # ========== STAGE: stitch ==========
        current_stage = "stitch"
        logger.info(
            "Pipeline stage=%s video_id=%s veo_clips=%d",
            current_stage,
            video_doc_id,
            len(veo_clips),
        )
        update_video_status(
            video_doc_id=video_doc_id,
            status="running",
            stage="stitch",
            error=None,
            extra_fields={"veo_clips_count": len(veo_clips)},
        )

        enhanced_video = workspace_dir / "enhanced.mp4"

        if len(veo_clips) >= 2:
            # Full assembly: Opener → Demo → Conclusion (2 Veo clips, 6s each)
            enhanced_meta = assemble_feature_video(
                opener_clip=veo_clips[0],
                demo_video=demo_normalized,
                closing_clips=veo_clips[1:2],
                output_path=enhanced_video,
            )
        elif len(veo_clips) >= 1:
            # Partial assembly with available clips
            enhanced_meta = assemble_feature_video(
                opener_clip=veo_clips[0],
                demo_video=demo_normalized,
                closing_clips=veo_clips[1:] if len(veo_clips) > 1 else [],
                output_path=enhanced_video,
            )
        else:
            # No Veo clips - just use demo as enhanced video
            logger.warning("No Veo clips available, using demo as enhanced video")
            shutil.copy(demo_normalized, enhanced_video)
            enhanced_meta = demo_meta

        logger.info(
            "Enhanced video assembled commit_id=%s duration=%.2fs",
            commit_doc_id,
            enhanced_meta["duration_sec"],
        )

        # Save debug: enhanced (stitched) video
        _save_debug_file(enhanced_video, "enhanced", video_doc_id)

        # ========== STAGE: voice ==========
        current_stage = "voice"
        logger.info(
            "Pipeline stage=%s video_id=%s languages=%s",
            current_stage,
            video_doc_id,
            languages_requested,
        )
        update_video_status(
            video_doc_id=video_doc_id,
            status="running",
            stage="voice",
            error=None,
            extra_fields={"enhanced_video_duration_sec": enhanced_meta["duration_sec"]},
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

                # Save debug: TTS audio
                _save_debug_file(audio_path, f"tts_narration_{language}", video_doc_id)

                # ========== STAGE: captions (per language) ==========
                srt_text = build_srt_from_timeline(shot_plan.get("timeline", []))
                captions_path = workspace_dir / f"captions_{language}.srt"
                captions_path.write_text(srt_text, encoding="utf-8")

                # Save debug: enhanced video audio before mixing
                enhanced_audio_debug = workspace_dir / f"enhanced_audio_{language}.wav"
                if _extract_audio_track(enhanced_video, enhanced_audio_debug):
                    _save_debug_file(enhanced_audio_debug, f"enhanced_audio_{language}", video_doc_id)

                # ========== STAGE: finalize (per language) ==========
                mixed_video = workspace_dir / f"mixed_{language}.mp4"
                mix_meta = mix_with_narration(enhanced_video, audio_path, mixed_video)

                # Save debug: mixed video and its audio track
                _save_debug_file(mixed_video, f"mixed_{language}", video_doc_id)
                mixed_audio_debug = workspace_dir / f"mixed_audio_{language}.wav"
                if _extract_audio_track(mixed_video, mixed_audio_debug):
                    _save_debug_file(mixed_audio_debug, f"mixed_audio_{language}", video_doc_id)

                final_video_path = workspace_dir / f"final_{language}.mp4"
                final_meta = burn_captions(mixed_video, captions_path, final_video_path)

                # Save debug: final video
                _save_debug_file(final_video_path, f"final_{language}", video_doc_id)

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
                logger.info(
                    "Track generated language=%s duration=%.2fs",
                    language,
                    final_meta["duration_sec"],
                )
            except Exception as exc:
                logger.exception(
                    "Track generation failed video_id=%s language=%s",
                    video_doc_id,
                    language,
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

        # ========== STAGE: finalize (upload) ==========
        current_stage = "finalize"
        logger.info("Pipeline stage=%s video_id=%s", current_stage, video_doc_id)
        update_video_status(
            video_doc_id=video_doc_id,
            status="running",
            stage="finalize",
            error=None,
        )

        demo_upload, enhanced_upload, uploaded_tracks = _upload_track_assets(
            repo_id=commit_doc["repo_id"],
            sha_short=commit_doc["sha_short"],
            demo_video_path=str(demo_normalized),
            enhanced_video_path=str(enhanced_video),
            track_payloads=track_payloads,
        )

        # ========== STAGE: done ==========
        successful_tracks = [
            lang
            for lang, track in uploaded_tracks.items()
            if track.get("status") == "completed"
        ]
        final_status = "completed" if successful_tracks else "failed"
        final_stage = "done" if successful_tracks else "error"
        final_error = (
            None if successful_tracks else "No language tracks were generated successfully"
        )
        current_stage = final_stage

        update_video_status(
            video_doc_id=video_doc_id,
            status=final_status,
            stage=final_stage,
            error=final_error,
            extra_fields={
                "goal": goal,
                "script": script,
                "shot_plan": shot_plan,
                "demo_video_url": demo_upload["url"],
                "enhanced_video_url": enhanced_upload["url"],
                "tracks": uploaded_tracks,
                "video_meta": enhanced_meta,
                "veo_clips_generated": len(veo_clips),
            },
        )

        # Also update commit with demo video URL
        update_commit_feature_demo(
            commit_doc_id=commit_doc_id,
            status="completed",
            video_url=demo_upload["url"],
            goal=goal,
        )

        logger.info(
            "Unified pipeline finished commit_id=%s video_id=%s status=%s successful_tracks=%s",
            commit_doc_id,
            video_doc_id,
            final_status,
            successful_tracks,
        )

    except Exception as exc:
        logger.exception(
            "Unified pipeline failed commit_id=%s video_id=%s stage=%s",
            commit_doc_id,
            video_doc_id,
            current_stage,
        )
        update_video_status(
            video_doc_id=video_doc_id,
            status="failed",
            stage="error",
            error=str(exc),
        )
        update_commit_feature_demo(
            commit_doc_id=commit_doc_id,
            status="failed",
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
    """Queue the unified pipeline for a commit.

    This is the main entry point for generating a feature video from a commit.
    The pipeline will:
    1. Generate a browser-use goal from the commit diff
    2. Record a demo video using browser-use
    3. Generate scene script and shot plan with 2 clip prompts
    4. Extract 2 snapshots from the demo video
    5. Generate 2 Veo clips (6s each) using snapshots as references
    6. Assemble: [Opener] → [Demo] → [Conclusion]
    7. Generate voiceover per language
    8. Generate captions per language
    9. Mix audio and burn captions
    10. Upload all assets
    """
    logger.info(
        "Enqueue unified pipeline commit_id=%s force=%s languages=%s",
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

    if existing and existing.get("status") in {"running", "completed"} and not force:
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
            _run_unified_pipeline, commit_id, video_doc_id, languages_requested
        )
        _FUTURES[video_doc_id] = future

    return {
        "queued": True,
        "skipped": False,
        "video_id": video_doc_id,
        "commit_id": commit_id,
        "status": "queued",
        "languages_requested": languages_requested,
        "stage": "goal",
    }


# Keep the feature demo pipeline for standalone demo recording
def _run_feature_demo_pipeline(commit_doc_id: str) -> None:
    """Run standalone feature demo pipeline: generate goal -> record -> upload."""
    logger.info("Feature demo pipeline started commit_id=%s", commit_doc_id)
    commit_doc = get_commit_by_id(commit_doc_id)
    if not commit_doc:
        logger.error("Feature demo failed: commit not found commit_id=%s", commit_doc_id)
        update_commit_feature_demo(
            commit_doc_id=commit_doc_id,
            status="failed",
            error=f"Commit not found: {commit_doc_id}",
        )
        return

    repo_full_name = commit_doc.get("repo_full_name", "")
    repo = get_repo(repo_full_name)
    website_url = (repo or {}).get("website_url") or ""
    if not website_url.strip():
        logger.error(
            "Feature demo failed: repo has no website_url commit_id=%s repo=%s",
            commit_doc_id,
            repo_full_name,
        )
        update_commit_feature_demo(
            commit_doc_id=commit_doc_id,
            status="failed",
            error=f"Repo {repo_full_name} has no website_url",
        )
        return

    website_url = website_url.strip()
    workspace_dir: Path | None = None

    try:
        update_commit_feature_demo(commit_doc_id=commit_doc_id, status="running")

        goal = generate_browser_use_goal(commit_doc)
        logger.info("Feature demo goal generated commit_id=%s goal_len=%s", commit_doc_id, len(goal))

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
        logger.info("Feature demo completed commit_id=%s video_url=%s", commit_doc_id, video_url)
    except Exception as exc:
        logger.exception("Feature demo failed commit_id=%s", commit_doc_id)
        update_commit_feature_demo(
            commit_doc_id=commit_doc_id,
            status="failed",
            error=str(exc),
        )
    finally:
        if workspace_dir and workspace_dir.exists():
            shutil.rmtree(workspace_dir, ignore_errors=True)


_FEATURE_DEMO_FUTURES: dict[str, Future] = {}
_FEATURE_DEMO_LOCK = Lock()


def enqueue_feature_demo_pipeline(
    commit_id: str, force: bool = False
) -> dict[str, Any]:
    """Queue standalone feature demo pipeline (for re-recording demo only)."""
    logger.info("Feature demo enqueue requested commit_id=%s force=%s", commit_id, force)
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
            return {
                "queued": False,
                "skipped": True,
                "reason": "already_queued",
                "commit_id": commit_id,
                "status": "running",
            }

        future = _EXECUTOR.submit(_run_feature_demo_pipeline, commit_id)
        _FEATURE_DEMO_FUTURES[commit_id] = future

    return {
        "queued": True,
        "skipped": False,
        "commit_id": commit_id,
        "status": "queued",
    }


# =============================================================================
# Pipeline from pre-generated script (for testing)
# =============================================================================


def _run_pipeline_from_script(
    commit_id: str,
    video_doc_id: str,
    script: dict[str, Any],
    shot_plan: dict[str, Any],
    languages_requested: list[str],
) -> None:
    """Run pipeline from snapshots stage onwards using pre-generated script.
    
    This skips goal, demo, and script generation - uses existing demo video
    and the provided script/shot_plan.
    
    Flow: snapshots → veo → stitch → voice → captions → finalize
    """
    logger.info(
        "Pipeline from script started commit_id=%s video_id=%s",
        commit_id,
        video_doc_id,
    )
    
    commit_doc = get_commit_by_id(commit_id)
    if not commit_doc:
        update_video_status(
            video_doc_id=video_doc_id,
            status="failed",
            stage="error",
            error=f"Commit not found: {commit_id}",
        )
        return
    
    # Get demo video URL from commit
    demo_video_url = commit_doc.get("feature_demo_video_url")
    if not demo_video_url:
        update_video_status(
            video_doc_id=video_doc_id,
            status="failed",
            stage="error",
            error="No demo video found. Run demo phase first.",
        )
        return
    
    workspace_dir = Path(tempfile.mkdtemp(prefix="diffcast-from-script-"))
    current_stage = "snapshots"
    
    try:
        # Download demo video
        import requests as http_requests
        
        demo_path = workspace_dir / "demo.mp4"
        logger.info("Downloading demo video url=%s", demo_video_url)
        response = http_requests.get(demo_video_url, timeout=120)
        response.raise_for_status()
        demo_path.write_bytes(response.content)
        
        # Normalize demo
        demo_normalized = workspace_dir / "demo_normalized.mp4"
        demo_meta = normalize_video(demo_path, demo_normalized)
        
        # ========== STAGE: snapshots ==========
        logger.info("Pipeline stage=%s video_id=%s", current_stage, video_doc_id)
        update_video_status(
            video_doc_id=video_doc_id,
            status="running",
            stage="snapshots",
            error=None,
            extra_fields={"script": script, "shot_plan": shot_plan},
        )
        
        snapshots_dir = workspace_dir / "snapshots"
        snapshot_paths = extract_snapshots(
            video_path=demo_normalized,
            output_dir=snapshots_dir,
            num_snapshots=2,
            strategy="uniform",
        )
        logger.info("Snapshots extracted count=%d", len(snapshot_paths))
        
        # ========== STAGE: veo ==========
        current_stage = "veo"
        logger.info("Pipeline stage=%s video_id=%s", current_stage, video_doc_id)
        update_video_status(
            video_doc_id=video_doc_id,
            status="running",
            stage="veo",
            error=None,
            extra_fields={"snapshots": [str(p) for p in snapshot_paths]},
        )
        
        veo_enabled = os.environ.get("PIPELINE_ENABLE_VEO", "true").lower() == "true"
        veo_clips: list[Path] = []
        clip_prompts = shot_plan.get("clip_prompts", [])
        
        if veo_enabled and clip_prompts:
            for idx, clip_info in enumerate(clip_prompts[:2]):
                try:
                    prompt = clip_info.get("prompt", "")
                    snapshot_index = clip_info.get("snapshot_index", idx)
                    reference_image = (
                        snapshot_paths[snapshot_index]
                        if snapshot_index < len(snapshot_paths)
                        else snapshot_paths[0] if snapshot_paths else None
                    )
                    
                    clip_path = workspace_dir / f"veo_{idx:02d}.mp4"
                    result = generate_veo_clip(
                        prompt=prompt,
                        output_path=clip_path,
                        duration_sec=6,
                        reference_image_path=reference_image,
                    )
                    
                    normalized_clip = workspace_dir / f"veo_{idx:02d}_norm.mp4"
                    normalize_video(result["path"], normalized_clip)
                    veo_clips.append(normalized_clip)
                    
                    _save_debug_file(result["path"], f"veo_{idx:02d}_raw", video_doc_id)
                    _save_debug_file(normalized_clip, f"veo_{idx:02d}_norm", video_doc_id)
                    
                    logger.info(
                        "Veo clip generated index=%d role=%s",
                        idx,
                        clip_info.get("role", "unknown"),
                    )
                except GeminiVideoError:
                    logger.exception("Veo clip generation failed index=%d", idx)
                    continue
        
        # ========== STAGE: stitch ==========
        current_stage = "stitch"
        logger.info("Pipeline stage=%s video_id=%s veo_clips=%d", current_stage, video_doc_id, len(veo_clips))
        update_video_status(
            video_doc_id=video_doc_id,
            status="running",
            stage="stitch",
            error=None,
            extra_fields={"veo_clips_count": len(veo_clips)},
        )
        
        enhanced_video = workspace_dir / "enhanced.mp4"
        
        if len(veo_clips) >= 2:
            enhanced_meta = assemble_feature_video(
                opener_clip=veo_clips[0],
                demo_video=demo_normalized,
                closing_clips=veo_clips[1:2],
                output_path=enhanced_video,
            )
        elif len(veo_clips) >= 1:
            enhanced_meta = assemble_feature_video(
                opener_clip=veo_clips[0],
                demo_video=demo_normalized,
                closing_clips=veo_clips[1:] if len(veo_clips) > 1 else [],
                output_path=enhanced_video,
            )
        else:
            logger.warning("No Veo clips available, using demo as enhanced video")
            shutil.copy(demo_normalized, enhanced_video)
            enhanced_meta = demo_meta
        
        _save_debug_file(enhanced_video, "enhanced", video_doc_id)
        
        # ========== STAGE: voice ==========
        current_stage = "voice"
        logger.info("Pipeline stage=%s video_id=%s languages=%s", current_stage, video_doc_id, languages_requested)
        update_video_status(
            video_doc_id=video_doc_id,
            status="running",
            stage="voice",
            error=None,
            extra_fields={"enhanced_video_duration_sec": enhanced_meta["duration_sec"]},
        )
        
        track_payloads: dict[str, dict[str, Any]] = {}
        for language in languages_requested:
            try:
                narration_text = generate_narration_script(shot_plan=shot_plan, language=language)
                audio_path = workspace_dir / f"narration_{language}.wav"
                tts_result = synthesize_with_gemini_tts(
                    text=narration_text,
                    output_path=audio_path,
                    language=language,
                )
                
                _save_debug_file(audio_path, f"tts_narration_{language}", video_doc_id)
                
                # ========== STAGE: captions ==========
                srt_text = build_srt_from_timeline(shot_plan.get("timeline", []))
                captions_path = workspace_dir / f"captions_{language}.srt"
                captions_path.write_text(srt_text, encoding="utf-8")
                
                # ========== STAGE: finalize ==========
                mixed_video = workspace_dir / f"mixed_{language}.mp4"
                mix_meta = mix_with_narration(enhanced_video, audio_path, mixed_video)
                
                _save_debug_file(mixed_video, f"mixed_{language}", video_doc_id)
                
                final_video_path = workspace_dir / f"final_{language}.mp4"
                final_meta = burn_captions(mixed_video, captions_path, final_video_path)
                
                _save_debug_file(final_video_path, f"final_{language}", video_doc_id)
                
                track_payloads[language] = {
                    "status": "completed",
                    "error": None,
                    "voice_script": narration_text,
                    "duration_sec": final_meta.get("duration_sec"),
                    "audio_path": str(audio_path),
                    "captions_path": str(captions_path),
                    "final_video_path": str(final_video_path),
                    "mix_meta": mix_meta,
                    "final_video_meta": final_meta,
                    "voice_model": tts_result.get("model"),
                }
                logger.info("Track generated language=%s duration=%.2fs", language, final_meta["duration_sec"])
            except Exception as exc:
                logger.exception("Track generation failed language=%s", language)
                track_payloads[language] = {
                    "status": "failed",
                    "error": str(exc),
                }
        
        # ========== STAGE: done - Upload ==========
        current_stage = "done"
        update_video_status(
            video_doc_id=video_doc_id,
            status="running",
            stage="uploading",
            error=None,
        )
        
        # Upload enhanced video
        enhanced_upload = upload_file(
            local_path=enhanced_video,
            dest_path=f"videos/{video_doc_id}/enhanced.mp4",
            content_type="video/mp4",
        )
        
        # Upload tracks
        tracks_final: dict[str, dict[str, Any]] = {}
        for language, payload in track_payloads.items():
            if payload["status"] != "completed":
                tracks_final[language] = payload
                continue
            
            audio_upload = upload_file(
                local_path=payload["audio_path"],
                dest_path=f"videos/{video_doc_id}/{language}/narration.wav",
                content_type="audio/wav",
            )
            srt_upload = upload_file(
                local_path=payload["captions_path"],
                dest_path=f"videos/{video_doc_id}/{language}/captions.srt",
                content_type="text/plain",
            )
            final_upload = upload_file(
                local_path=payload["final_video_path"],
                dest_path=f"videos/{video_doc_id}/{language}/final.mp4",
                content_type="video/mp4",
            )
            
            tracks_final[language] = {
                "status": "completed",
                "error": None,
                "voice_script": payload.get("voice_script"),
                "duration_sec": payload.get("duration_sec"),
                "voice_provider": "gemini_tts",
                "caption_mode": "burned_plus_srt",
                "audio_url": audio_upload["url"],
                "captions_url": srt_upload["url"],
                "final_video_url": final_upload["url"],
            }
        
        # Final update
        update_video_status(
            video_doc_id=video_doc_id,
            status="completed",
            stage="done",
            error=None,
            extra_fields={
                "enhanced_video_url": enhanced_upload["url"],
                "base_video_url": enhanced_upload["url"],
                "tracks": tracks_final,
                "script": script,
                "shot_plan": shot_plan,
            },
        )
        logger.info("Pipeline from script completed video_id=%s", video_doc_id)
        
    except Exception as exc:
        logger.exception("Pipeline from script failed video_id=%s stage=%s", video_doc_id, current_stage)
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


def enqueue_pipeline_from_script(
    commit_id: str,
    script: dict[str, Any],
    shot_plan: dict[str, Any],
    languages: list[str] | None = None,
) -> dict[str, Any]:
    """Queue pipeline from snapshots stage using pre-generated script.
    
    This is for testing - skips goal/demo/script generation and uses
    the provided script and shot_plan with the existing demo video.
    """
    logger.info("Enqueue pipeline from script commit_id=%s", commit_id)
    
    commit_doc = get_commit_by_id(commit_id)
    if not commit_doc:
        raise ValueError(f"Commit not found: {commit_id}")
    
    if not commit_doc.get("feature_demo_video_url"):
        raise ValueError("No demo video found. Run demo phase first.")
    
    languages_requested = parse_target_languages(languages)
    video_doc_id = build_video_doc_id(commit_doc["repo_full_name"], commit_doc["sha"])
    
    # Create/update video doc
    upsert_video_doc(
        video_doc_id=video_doc_id,
        payload={
            "video_id": video_doc_id,
            "commit_id": commit_doc["id"],
            "repo_full_name": commit_doc["repo_full_name"],
            "sha": commit_doc["sha"],
            "sha_short": commit_doc["sha_short"],
            "status": "queued",
            "stage": "snapshots",
            "error": None,
            "languages_requested": languages_requested,
            "script": script,
            "shot_plan": shot_plan,
            "updated_at": datetime.utcnow(),
        },
    )
    
    with _LOCK:
        future = _EXECUTOR.submit(
            _run_pipeline_from_script,
            commit_id,
            video_doc_id,
            script,
            shot_plan,
            languages_requested,
        )
        _FUTURES[video_doc_id] = future
    
    return {
        "queued": True,
        "video_id": video_doc_id,
        "commit_id": commit_id,
        "status": "queued",
        "stage": "snapshots",
        "languages_requested": languages_requested,
        "clip_prompts_count": len(shot_plan.get("clip_prompts", [])),
    }
