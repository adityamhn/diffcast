"""Gemini Veo helpers for AI Studio video generation."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class GeminiVideoError(Exception):
    """Raised when Veo generation fails."""


def _build_client() -> Any:
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        raise GeminiVideoError("Missing GEMINI_API_KEY")
    try:
        from google import genai
    except Exception as exc:  # pragma: no cover - dependency import guard
        raise GeminiVideoError("google-genai package is required for Veo generation") from exc
    return genai.Client(api_key=api_key)


def _safe_model_list(client: Any) -> list[str]:
    try:
        models = client.models.list()
    except Exception:
        return []
    names: list[str] = []
    for model in models:
        name = getattr(model, "name", "")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def resolve_veo_model() -> str:
    """Pick fast model if available, otherwise use default."""
    default_model = "veo-3.1-generate-preview"
    fast_model = "veo-3.1-generate-preview"

    client = _build_client()
    available = _safe_model_list(client)
    if any(fast_model in name for name in available):
        logger.info("Using fast Veo model model=%s", fast_model)
        return fast_model
    logger.info("Using default Veo model model=%s", default_model)
    return default_model


def _extract_video_bytes(result: Any) -> bytes | None:
    """Best-effort extraction across SDK variants."""
    candidate_attrs = (
        "generated_video",
        "video",
        "inline_data",
        "data",
    )
    for attr in candidate_attrs:
        value = getattr(result, attr, None)
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)

    payload = getattr(result, "model_dump", None)
    if callable(payload):
        dumped = payload()
        blob = dumped.get("video") or dumped.get("generated_video") or dumped.get("inline_data")
        if isinstance(blob, dict):
            data = blob.get("data")
            if isinstance(data, str):
                import base64

                try:
                    return base64.b64decode(data)
                except Exception:
                    return None
        if isinstance(blob, str):
            import base64

            try:
                return base64.b64decode(blob)
            except Exception:
                return None

    return None


def _save_video_blob(video_bytes: bytes, output_path: str | Path) -> str:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(video_bytes)
    return str(out)


_VEO_ALLOWED_DURATIONS = (4, 6, 8)


def _snap_duration(requested: int) -> int:
    """Snap requested duration to nearest Veo-allowed value (4, 6, or 8)."""
    return min(_VEO_ALLOWED_DURATIONS, key=lambda d: abs(d - requested))


def generate_veo_clip(
    prompt: str,
    output_path: str | Path,
    duration_sec: int = 4,
    aspect_ratio: str = "16:9",
) -> dict[str, Any]:
    """Generate a short cinematic insert clip with Veo.

    Uses AI Studio Gemini API (google-genai SDK) and returns local path + metadata.
    Veo 3.1 models only accept durations of exactly 4, 6, or 8 seconds.

    Follows the official polling pattern from:
    https://ai.google.dev/gemini-api/docs/video
    """
    if not prompt.strip():
        raise GeminiVideoError("Veo prompt must be non-empty")

    model = resolve_veo_model()
    client = _build_client()
    actual_duration = _snap_duration(int(duration_sec))

    logger.info("Generating Veo clip model=%s requested_sec=%s actual_sec=%s", model, duration_sec, actual_duration)

    try:
        from google.genai import types

        operation = client.models.generate_videos(
            model=model,
            prompt=prompt,
            config=types.GenerateVideosConfig(
                duration_seconds=actual_duration,
                aspect_ratio=aspect_ratio,
                person_generation="allow_all",
            ),
        )
    except Exception as exc:
        raise GeminiVideoError(f"Veo request failed: {exc}") from exc

    # Poll the operation status until the video is ready.
    # The operation object must be refreshed via client.operations.get().
    timeout_sec = int(os.environ.get("PIPELINE_VEO_TIMEOUT_SEC", "300"))
    poll_sec = 10.0
    start = time.time()
    last_progress_log = 0.0

    while not operation.done:
        elapsed = time.time() - start
        if elapsed > timeout_sec:
            raise GeminiVideoError(f"Veo generation timed out after {timeout_sec}s")
        if elapsed - last_progress_log >= 10:
            logger.info(
                "Veo generation pending model=%s elapsed_sec=%.0f timeout_sec=%s",
                model,
                elapsed,
                timeout_sec,
            )
            last_progress_log = elapsed
        time.sleep(poll_sec)
        operation = client.operations.get(operation)

    logger.info(
        "Veo generation completed model=%s elapsed_sec=%.0f",
        model,
        time.time() - start,
    )

    # Extract the generated video from the response.
    response = operation.response
    generated_videos = getattr(response, "generated_videos", None)
    if not generated_videos:
        raise GeminiVideoError("Veo response did not include generated videos")

    generated_video = generated_videos[0]
    video_obj = getattr(generated_video, "video", None)

    # Try the SDK download + save approach first (official pattern).
    try:
        client.files.download(file=video_obj)
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        video_obj.save(str(out))
        logger.info("Veo clip saved via SDK path=%s", out)
        return {
            "path": str(out),
            "model": model,
            "duration_sec": actual_duration,
            "prompt": prompt,
        }
    except Exception as sdk_err:
        logger.warning("SDK save failed, falling back to byte extraction: %s", sdk_err)

    # Fallback: extract raw bytes from the response object.
    video_bytes = _extract_video_bytes(generated_video)
    if not video_bytes and video_obj:
        video_bytes = _extract_video_bytes(video_obj)

    if not video_bytes:
        serialized = ""
        try:
            serialized = json.dumps(getattr(generated_video, "model_dump", lambda: {})())[:300]
        except Exception:
            pass
        raise GeminiVideoError(f"Unable to extract video bytes from Veo response {serialized}")

    saved_path = _save_video_blob(video_bytes, output_path)
    return {
        "path": saved_path,
        "model": model,
        "duration_sec": actual_duration,
        "prompt": prompt,
    }
