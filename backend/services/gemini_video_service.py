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


def _load_reference_image(image_path: str | Path) -> Any:
    """Load an image file and create a Veo reference image object."""
    from google.genai import types

    image_path = Path(image_path)
    if not image_path.exists():
        raise GeminiVideoError(f"Reference image not found: {image_path}")

    image_bytes = image_path.read_bytes()
    if not image_bytes:
        raise GeminiVideoError(f"Reference image is empty: {image_path}")

    # Determine mime type from extension
    suffix = image_path.suffix.lower()
    mime_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }
    mime_type = mime_map.get(suffix, "image/png")

    # Create Image object with bytes and mime type
    image = types.Image(image_bytes=image_bytes, mime_type=mime_type)

    # Create reference with "asset" type to preserve visual style/elements
    reference = types.VideoGenerationReferenceImage(
        image=image,
        reference_type="asset",
    )

    logger.debug("Loaded reference image path=%s size=%d mime=%s", image_path, len(image_bytes), mime_type)
    return reference


def generate_veo_clip(
    prompt: str,
    output_path: str | Path,
    duration_sec: int = 6,
    aspect_ratio: str = "16:9",
    reference_image_path: str | Path | None = None,
) -> dict[str, Any]:
    """Generate a short cinematic insert clip with Veo.

    Uses AI Studio Gemini API (google-genai SDK) and returns local path + metadata.
    Veo 3.1 models only accept durations of exactly 4, 6, or 8 seconds.

    Args:
        prompt: Text prompt describing the video to generate
        output_path: Where to save the generated video
        duration_sec: Desired duration (will snap to 4, 6, or 8)
        aspect_ratio: Video aspect ratio (default 16:9)
        reference_image_path: Optional path to a reference image (snapshot) that
            will be used as a visual reference for the generated video

    Returns:
        Dict with path, model, duration_sec, prompt, and has_reference_image

    Follows the official polling pattern from:
    https://ai.google.dev/gemini-api/docs/video
    """
    if not prompt.strip():
        raise GeminiVideoError("Veo prompt must be non-empty")

    model = resolve_veo_model()
    client = _build_client()
    actual_duration = _snap_duration(int(duration_sec))

    has_reference = reference_image_path is not None
    logger.info(
        "Generating Veo clip model=%s requested_sec=%s actual_sec=%s has_reference=%s",
        model,
        duration_sec,
        actual_duration,
        has_reference,
    )
    logger.info("Veo prompt (clip): %s", prompt[:500] + ("..." if len(prompt) > 500 else ""))
    print(f"[Veo] Prompt sent:\n{prompt}\n")

    try:
        from google.genai import types

        # Build config with optional reference images.
        # Note: person_generation is not included - "allow_all" is not supported by Veo 3.1.
        config_kwargs: dict[str, Any] = {
            "duration_seconds": actual_duration,
            "aspect_ratio": aspect_ratio,
        }

        if reference_image_path:
            reference = _load_reference_image(reference_image_path)
            config_kwargs["reference_images"] = [reference]

        operation = client.models.generate_videos(
            model=model,
            prompt=prompt,
            config=types.GenerateVideosConfig(**config_kwargs),
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
        logger.info("Veo clip saved via SDK path=%s has_reference=%s", out, has_reference)
        return {
            "path": str(out),
            "model": model,
            "duration_sec": actual_duration,
            "prompt": prompt,
            "has_reference_image": has_reference,
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
        "has_reference_image": has_reference,
    }
