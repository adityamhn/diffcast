"""LLM + media generation utilities for commit pipeline."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from utils.invoke_llm import LLMModel, invoke_llm

logger = logging.getLogger(__name__)


class MediaGenerationError(Exception):
    """Base error for media generation failures."""


class ScriptValidationError(MediaGenerationError):
    """Raised when generated script payload is invalid."""


class VideoRenderError(MediaGenerationError):
    """Raised when ffmpeg/ffprobe pipeline fails."""


class VoiceoverError(MediaGenerationError):
    """Raised when TTS synthesis fails."""


class SceneScriptSceneSchema(BaseModel):
    """Structured scene payload expected from Gemini."""

    on_screen_text: str
    narration_seed: str
    duration_sec: float


class SceneScriptSchema(BaseModel):
    """Structured script payload expected from Gemini."""

    title: str
    feature_summary: str
    scenes: list[SceneScriptSceneSchema]


class BrowserUseGoalSchema(BaseModel):
    """Browser use goal from commit diff."""

    goal: str


LANGUAGE_CODE_MAP = {
    "en": "en-US",
    "es": "es-ES",
    "fr": "fr-FR",
    "de": "de-DE",
    "hi": "hi-IN",
    "it": "it-IT",
    "ja": "ja-JP",
    "ko": "ko-KR",
    "pt": "pt-BR",
}

JARGON_PATTERN = re.compile(
    r"\b(api|class|function|refactor|endpoint|backend|frontend|schema|regex|cli|sql|database|cache|repository)\b",
    flags=re.IGNORECASE,
)


def parse_target_languages(languages: list[str] | None = None) -> list[str]:
    """Resolve normalized language list from request or env."""
    raw = languages
    if raw is None:
        env_value = os.environ.get("PIPELINE_LANGUAGES", "")
        raw = [item.strip() for item in env_value.split(",") if item.strip()]

    normalized: list[str] = []
    for item in raw:
        code = str(item).strip().lower()
        if code and code not in normalized:
            normalized.append(code)

    resolved = normalized or ["en"]
    logger.info("Resolved target languages=%s", resolved)
    return resolved


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _build_diff_payload(commit_doc: dict[str, Any], max_patch_chars: int = 18000) -> str:
    files = commit_doc.get("files", [])
    chunks: list[str] = []
    chars = 0
    for file_item in files:
        patch = file_item.get("patch") or ""
        if not patch:
            continue
        header = (
            f"FILE: {file_item.get('path', '')}\n"
            f"STATUS: {file_item.get('status', 'modified')}\n"
            f"PATCH:\n"
        )
        body = f"{header}{patch}\n\n"
        remaining = max_patch_chars - chars
        if remaining <= 0:
            break
        if len(body) > remaining:
            body = f"{body[:remaining]}\n...TRUNCATED..."
        chunks.append(body)
        chars += len(body)

    return "".join(chunks)


def _contains_jargon(text: str) -> bool:
    return bool(JARGON_PATTERN.search(text or ""))


def _build_commit_chat_context(commit_doc: dict[str, Any], video_doc: dict[str, Any] | None) -> str:
    """Build context string for commit Q&A chat (non-technical audience)."""
    parts: list[str] = []

    msg = (commit_doc.get("message") or "").strip()
    if msg:
        parts.append(f"Commit message: {msg}")

    goal = (commit_doc.get("feature_demo_goal") or "").strip()
    if goal:
        parts.append(f"Demo goal: {goal}")

    diff_summary = (commit_doc.get("diff_summary") or "").strip()
    if diff_summary:
        parts.append(f"Diff summary: {diff_summary}")

    diff_payload = _build_diff_payload(commit_doc, max_patch_chars=12000)
    if diff_payload.strip():
        parts.append(f"Code changes (unified diff):\n{diff_payload}")

    if video_doc:
        script = video_doc.get("script") or {}
        if isinstance(script, dict):
            title = (script.get("title") or "").strip()
            summary = (script.get("feature_summary") or "").strip()
            scenes = script.get("scenes") or []
            if title:
                parts.append(f"Feature video title: {title}")
            if summary:
                parts.append(f"Feature summary (for video): {summary}")
            if scenes:
                scene_lines = []
                for i, s in enumerate(scenes[:6], 1):
                    if isinstance(s, dict):
                        text = s.get("on_screen_text") or s.get("narration_seed") or ""
                        if text:
                            scene_lines.append(f"  {i}. {text}")
                if scene_lines:
                    parts.append("Video scenes:\n" + "\n".join(scene_lines))

    return "\n\n".join(parts) if parts else "No context available for this commit."


def answer_commit_question(
    commit_doc: dict[str, Any],
    video_doc: dict[str, Any] | None,
    messages: list[dict[str, str]],
) -> str:
    """
    Answer a user question about a commit using commit + video context.
    Messages are OpenAI-style: [{ role: "user"|"assistant", content: string }].
    Responds in plain language for non-technical users.
    """
    context = _build_commit_chat_context(commit_doc, video_doc)

    system_message = (
        "You are a helpful assistant explaining product updates and feature releases to non-technical users. "
        "You have access to the commit details, code changes, demo goal, and feature video script. "
        "Answer questions in plain, friendly language. Avoid jargon (API, endpoint, refactor, schema, etc.). "
        "If the user asks something you cannot answer from the context, say so politely. "
        "Keep answers concise but informative."
    )

    user_context = (
        "Use the following context about this feature release to answer the user's questions:\n\n"
        f"{context}\n\n"
        "---\n\n"
        "Conversation:"
    )

    # Build full messages: system + context + conversation
    full_messages: list[dict[str, str]] = [
        {"role": "system", "content": f"{system_message}\n\n{user_context}"},
    ]

    for m in messages:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            full_messages.append({"role": role, "content": content})

    response = invoke_llm(
        messages=full_messages,
        model=LLMModel.GEMINI_2_0_FLASH,
        json_mode=False,
        temperature=0.3,
        max_output_tokens=1024,
        retries=1,
        timeout_seconds=30.0,
    )
    return (response.get("text") or "").strip()


def validate_scene_script(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize Gemini script JSON."""
    if not isinstance(payload, dict):
        raise ScriptValidationError("script payload must be an object")

    title = str(payload.get("title", "")).strip()
    summary = str(payload.get("feature_summary", "")).strip()
    scenes = payload.get("scenes")

    if not title:
        raise ScriptValidationError("script.title is required")
    if not summary:
        raise ScriptValidationError("script.feature_summary is required")
    if not isinstance(scenes, list) or not scenes:
        raise ScriptValidationError("script.scenes must be a non-empty list")
    if len(scenes) > 8:
        raise ScriptValidationError("script.scenes supports at most 8 scenes")

    if _contains_jargon(title) or _contains_jargon(summary):
        raise ScriptValidationError("script contains technical jargon")

    normalized_scenes: list[dict[str, Any]] = []
    total_duration = 0.0
    for index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            raise ScriptValidationError(f"scene[{index}] must be an object")
        on_screen_text = str(scene.get("on_screen_text", "")).strip()
        narration_seed = str(scene.get("narration_seed", "")).strip()
        duration_raw = scene.get("duration_sec")
        if not on_screen_text:
            raise ScriptValidationError(f"scene[{index}].on_screen_text is required")
        if not narration_seed:
            raise ScriptValidationError(f"scene[{index}].narration_seed is required")
        if _contains_jargon(on_screen_text) or _contains_jargon(narration_seed):
            raise ScriptValidationError(f"scene[{index}] contains technical jargon")
        try:
            duration = float(duration_raw)
        except (TypeError, ValueError) as exc:
            raise ScriptValidationError(f"scene[{index}].duration_sec must be numeric") from exc
        if duration < 2 or duration > 20:
            raise ScriptValidationError(f"scene[{index}].duration_sec must be between 2 and 20")

        total_duration += duration
        normalized_scenes.append(
            {
                "on_screen_text": _truncate(on_screen_text, 240),
                "narration_seed": _truncate(narration_seed, 260),
                "duration_sec": duration,
            }
        )

    if total_duration > 120:
        raise ScriptValidationError("total video duration must be at most 120 seconds")

    return {
        "title": _truncate(title, 120),
        "feature_summary": _truncate(summary, 260),
        "scenes": normalized_scenes,
        "total_duration_sec": round(total_duration, 2),
    }

def generate_browser_use_goal(commit_doc: dict[str, Any]) -> str:
    """Generate a browser use goal from commit diff."""
    diff_payload = _build_diff_payload(commit_doc)
    if not diff_payload.strip():
        raise ScriptValidationError("commit has no patch content to describe")

    logger.info(
        "Generating browser use goal commit_id=%s repo=%s files=%s",
        commit_doc.get("id"),
        commit_doc.get("repo_full_name"),
        len(commit_doc.get("files", [])),
    )

    system_message = (
        "You are a head of product engineering who defines a goal for demonstrating a new product feature release. "
        "You are given a commit diff and you need to define a goal for demonstrating the new feature. "
        "The goal should be a short paragraph (no more than 70 words) describing: "
        "the steps to demonstrate the new feature (e.g. first navigate to this page) and the expected outcome. "
        "Respond with JSON only: {\"goal\": \"your goal text\"}."
    )

    user_message = (
        "Analyze the commit diff and produce a JSON object with a single \"goal\" field containing "
        "a short paragraph describing the steps to demonstrate the new feature and the expected outcome.\n\n"
        f"Unified diff:\n{diff_payload}\n"
    )

    response = invoke_llm(
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
        model=LLMModel.GEMINI_2_5_PRO,
        json_mode=True,
        temperature=0.2,
        max_output_tokens=3000,
        retries=1,
        timeout_seconds=60.0,
    )

    parsed = response.get("json")
    schema = BrowserUseGoalSchema.model_validate(parsed)
    return schema.goal

def generate_scene_script(commit_doc: dict[str, Any]) -> dict[str, Any]:
    """Generate a non-technical scene-by-scene script from commit diff."""
    diff_payload = _build_diff_payload(commit_doc)
    if not diff_payload.strip():
        raise ScriptValidationError("commit has no patch content to describe")
    logger.info(
        "Generating scene script commit_id=%s repo=%s files=%s",
        commit_doc.get("id"),
        commit_doc.get("repo_full_name"),
        len(commit_doc.get("files", [])),
    )

    commit_message = commit_doc.get("message", "")
    repo_name = commit_doc.get("repo_full_name", "")
    file_paths = [item.get("path", "") for item in commit_doc.get("files", [])]
    file_paths_str = "\n".join(f"- {path}" for path in file_paths[:30])

    system_message = (
        "You are a really smart talented product manager who writes short product update transcripts for non-technical audiences. "
        "Avoid all code or engineering jargon (unless it contributes in selling that feature somehow)."
        "This is going to be used to generate a video for a product update, so it should be engaging and interesting to watch."
    )
    user_message = (
        "Analyze the commit diff and produce JSON only.\n\n"
        "Required JSON object:\n"
        "{\n"
        '  "title": "string",\n'
        '  "feature_summary": "string",\n'
        '  "scenes": [\n'
        "    {\n"
        '      "on_screen_text": "string",\n'
        '      "narration_seed": "string",\n'
        '      "duration_sec": number\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Constraints:\n"
        "- 3 to 6 scenes.\n"
        "- Keep language simple and non-technical.\n"
        "- No words like API, endpoint, refactor, class, backend, frontend.\n"
        "- on_screen_text: short sentence, <= 140 chars.\n"
        "- narration_seed: friendly explanation, <= 180 chars.\n"
        "- duration_sec: between 3 and 12.\n\n"
        f"Repo: {repo_name}\n"
        f"Commit message: {commit_message}\n"
        f"Changed files:\n{file_paths_str}\n\n"
        f"Unified diff:\n{diff_payload}\n"
    )

    response = invoke_llm(
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
        model=LLMModel.GEMINI_2_5_PRO,
        json_mode=True,
        response_schema=SceneScriptSchema,
        temperature=0.2,
        max_output_tokens=2000,
        retries=1,
        timeout_seconds=60.0,
    )
    payload = response.get("json")
    if not isinstance(payload, dict):
        raise ScriptValidationError("Gemini script response must be a JSON object")
    script = validate_scene_script(payload)
    logger.info(
        "Scene script generated commit_id=%s scenes=%s total_duration_sec=%s",
        commit_doc.get("id"),
        len(script.get("scenes", [])),
        script.get("total_duration_sec"),
    )
    return script


def _render_text_frame(
    text: str,
    output_path: str | Path,
    width: int = 1280,
    height: int = 720,
    bg_color: str = "#111827",
    text_color: str = "#FFFFFF",
    font_size: int = 44,
    wrap_width: int = 36,
    max_lines: int = 4,
) -> None:
    """Render a text-on-background PNG frame using Pillow (no drawtext needed)."""
    from PIL import Image, ImageDraw, ImageFont

    # Create background
    img = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    # Wrap text
    wrapped = textwrap.wrap(text.strip(), width=wrap_width) or [text.strip()]
    wrapped = wrapped[:max_lines]

    # Try to use a decent built-in font; fall back to default
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", font_size)
    except (OSError, IOError):
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size)
        except (OSError, IOError):
            try:
                # macOS system font
                font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
            except (OSError, IOError):
                font = ImageFont.load_default()

    line_spacing = 10
    # Measure each line and compute total block height
    line_bboxes = [draw.textbbox((0, 0), line, font=font) for line in wrapped]
    line_heights = [bb[3] - bb[1] for bb in line_bboxes]
    total_text_height = sum(line_heights) + line_spacing * (len(wrapped) - 1)

    # Draw semi-transparent box behind text
    box_pad = 30
    max_line_width = max(bb[2] - bb[0] for bb in line_bboxes)
    box_x0 = (width - max_line_width) // 2 - box_pad
    box_y0 = (height - total_text_height) // 2 - box_pad
    box_x1 = (width + max_line_width) // 2 + box_pad
    box_y1 = (height + total_text_height) // 2 + box_pad

    # Draw rounded box overlay
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rounded_rectangle(
        [box_x0, box_y0, box_x1, box_y1],
        radius=12,
        fill=(0, 0, 0, 115),  # ~45% opacity
    )
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    # Draw text lines centered
    y_cursor = (height - total_text_height) // 2
    for line, bbox, lh in zip(wrapped, line_bboxes, line_heights):
        line_width = bbox[2] - bbox[0]
        x = (width - line_width) // 2
        draw.text((x, y_cursor), line, fill=text_color, font=font)
        y_cursor += lh + line_spacing

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    img.save(str(output_path), "PNG")


def build_scene_ffmpeg_command(
    frame_image_path: str | Path,
    duration_sec: float,
    output_path: str | Path,
) -> list[str]:
    """Build ffmpeg command for one scene from a static PNG frame."""
    return [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-i",
        str(frame_image_path),
        "-t",
        str(duration_sec),
        "-r",
        "30",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-vf",
        "scale=1280:720",
        str(output_path),
    ]


def _run_subprocess(cmd: list[str], error_prefix: str) -> None:
    logger.debug("Running subprocess: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        detail = stderr or stdout or "unknown error"
        raise VideoRenderError(f"{error_prefix}: {detail}")


def _ensure_ffmpeg_tools() -> None:
    if not shutil.which("ffmpeg"):
        raise VideoRenderError("ffmpeg is required but not installed")
    if not shutil.which("ffprobe"):
        raise VideoRenderError("ffprobe is required but not installed")
    logger.debug("ffmpeg/ffprobe availability check passed")


def _probe_video(video_path: str | Path) -> dict[str, Any]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=width,height",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(video_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise VideoRenderError(f"ffprobe failed: {(proc.stderr or '').strip()}")
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise VideoRenderError("ffprobe returned invalid JSON") from exc

    streams = payload.get("streams") or []
    first_stream = streams[0] if streams else {}
    fmt = payload.get("format") or {}
    try:
        duration = float(fmt.get("duration", 0))
    except (TypeError, ValueError):
        duration = 0.0
    width = int(first_stream.get("width", 0) or 0)
    height = int(first_stream.get("height", 0) or 0)

    if duration <= 0 or width <= 0 or height <= 0:
        raise VideoRenderError("ffprobe validation failed for rendered video")

    return {"duration_sec": round(duration, 2), "width": width, "height": height}


def render_scene_video(scenes: list[dict[str, Any]], output_path: str | Path) -> dict[str, Any]:
    """Render text-scene video from script scenes."""
    _ensure_ffmpeg_tools()
    logger.info("Rendering scene video scenes=%s output=%s", len(scenes), output_path)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="diffcast-scenes-") as temp_dir:
        temp_root = Path(temp_dir)
        scene_paths: list[Path] = []
        for index, scene in enumerate(scenes):
            # 1) Render text onto a PNG frame using Pillow
            frame_path = temp_root / f"frame_{index:03d}.png"
            _render_text_frame(
                text=scene["on_screen_text"],
                output_path=frame_path,
            )
            # 2) Convert the static PNG into a video clip with ffmpeg
            scene_path = temp_root / f"scene_{index:03d}.mp4"
            cmd = build_scene_ffmpeg_command(
                frame_image_path=frame_path,
                duration_sec=scene["duration_sec"],
                output_path=scene_path,
            )
            _run_subprocess(cmd, f"ffmpeg scene render failed for scene {index}")
            scene_paths.append(scene_path)
            logger.debug("Rendered scene index=%s duration=%s", index, scene["duration_sec"])

        concat_file = temp_root / "concat.txt"
        lines = [f"file '{scene_path.as_posix()}'" for scene_path in scene_paths]
        concat_file.write_text("\n".join(lines), encoding="utf-8")

        concat_cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ]
        _run_subprocess(concat_cmd, "ffmpeg concat failed")

    if not out.exists() or out.stat().st_size == 0:
        raise VideoRenderError("rendered video file is missing or empty")
    metadata = _probe_video(out)
    logger.info("Scene video rendered output=%s metadata=%s", out, metadata)
    return metadata


def render_final_localized_video(
    base_video_path: str | Path,
    audio_path: str | Path,
    captions_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    """Mux base video, localized audio, and captions into one MP4."""
    _ensure_ffmpeg_tools()
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Rendering final localized video base=%s audio=%s captions=%s output=%s",
        base_video_path,
        audio_path,
        captions_path,
        out,
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(base_video_path),
        "-i",
        str(audio_path),
        "-i",
        str(captions_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-map",
        "2:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-c:s",
        "mov_text",
        "-metadata:s:s:0",
        "language=und",
        "-shortest",
        str(out),
    ]
    _run_subprocess(cmd, "ffmpeg localized mux failed")
    if not out.exists() or out.stat().st_size == 0:
        raise VideoRenderError("localized output video is missing or empty")
    metadata = _probe_video(out)
    logger.info("Final localized video rendered output=%s metadata=%s", out, metadata)
    return metadata


def _parse_voice_map() -> dict[str, str]:
    raw = os.environ.get("PIPELINE_VOICE_MAP", "").strip()
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    voice_map: dict[str, str] = {}
    for key, value in loaded.items():
        if isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip():
            voice_map[key.strip().lower()] = value.strip()
    return voice_map


def generate_localized_script(
    base_script: dict[str, Any],
    language: str,
) -> dict[str, list[str]]:
    """Generate language-specific voice and caption lines for each scene."""
    logger.info(
        "Generating localized script language=%s scenes=%s",
        language,
        len(base_script["scenes"]),
    )
    scenes = base_script["scenes"]
    payload = {
        "language": language,
        "title": base_script["title"],
        "feature_summary": base_script["feature_summary"],
        "scene_narration_seed": [scene["narration_seed"] for scene in scenes],
        "scene_on_screen_text": [scene["on_screen_text"] for scene in scenes],
    }
    response = invoke_llm(
        messages=[
            {
                "role": "system",
                "content": (
                    "You produce narration and caption lines for short product videos. "
                    "Use simple non-technical language."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Return JSON only with this object shape:\n"
                    '{ "voice_lines": [string], "caption_lines": [string] }\n'
                    "- Keep one line per scene.\n"
                    "- Keep each line concise and easy to read.\n"
                    "- Do not add numbering or extra metadata.\n"
                    f"- Output language: {language}\n\n"
                    f"Source JSON:\n{json.dumps(payload, ensure_ascii=True)}"
                ),
            },
        ],
        model=LLMModel.GEMINI_2_0_FLASH,
        json_mode=True,
        temperature=0.3,
        max_output_tokens=1800,
        retries=1,
        timeout_seconds=45.0,
    )
    data = response.get("json")
    if not isinstance(data, dict):
        raise ScriptValidationError(f"localized script for {language} must be a JSON object")
    voice_lines = data.get("voice_lines")
    caption_lines = data.get("caption_lines")
    if not isinstance(voice_lines, list) or not isinstance(caption_lines, list):
        raise ScriptValidationError(
            f"localized script for {language} must include voice_lines and caption_lines arrays"
        )
    if len(voice_lines) != len(scenes) or len(caption_lines) != len(scenes):
        raise ScriptValidationError(
            f"localized script for {language} must match the scene count ({len(scenes)})"
        )
    normalized_voice = [str(line).strip() for line in voice_lines]
    normalized_captions = [str(line).strip() for line in caption_lines]
    if any(not line for line in normalized_voice) or any(not line for line in normalized_captions):
        raise ScriptValidationError(f"localized script for {language} contains empty lines")
    logger.info("Localized script generated language=%s", language)
    return {"voice_lines": normalized_voice, "caption_lines": normalized_captions}


def _format_srt_timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    hours = total_ms // 3_600_000
    minutes = (total_ms % 3_600_000) // 60_000
    secs = (total_ms % 60_000) // 1000
    millis = total_ms % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _limit_caption_lines(text: str) -> str:
    wrapped = textwrap.wrap(text.strip(), width=42) or [text.strip()]
    if len(wrapped) <= 2:
        return "\n".join(wrapped)
    return f"{wrapped[0]}\n{' '.join(wrapped[1:])[:42]}"


def _resolve_google_credentials_path() -> Path | None:
    """Resolve credentials path from ADC env or FIREBASE_SERVICE_ACCOUNT_PATH."""
    raw_path = (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    if not raw_path:
        raw_path = (os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH") or "").strip()
    if not raw_path:
        return None
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        backend_root = Path(__file__).resolve().parents[1]
        candidate = backend_root / candidate
    return candidate if candidate.exists() else None


def build_srt(caption_lines: list[str], scene_durations: list[float]) -> str:
    """Build SRT captions aligned to scene durations."""
    if len(caption_lines) != len(scene_durations):
        raise ScriptValidationError("caption lines must match scene durations")

    current = 0.0
    chunks: list[str] = []
    for index, (line, duration) in enumerate(zip(caption_lines, scene_durations), start=1):
        start = _format_srt_timestamp(current)
        end = _format_srt_timestamp(current + float(duration))
        text = _limit_caption_lines(line)
        chunks.append(f"{index}\n{start} --> {end}\n{text}")
        current += float(duration)
    return "\n\n".join(chunks).strip() + "\n"


def synthesize_voiceover(text: str, language: str, output_path: str | Path) -> None:
    """Generate MP3 voiceover for given text/language via Google Cloud TTS."""
    try:
        from google.cloud import texttospeech
    except Exception as exc:
        raise VoiceoverError("google-cloud-texttospeech is required for voiceover generation") from exc

    language_code = LANGUAGE_CODE_MAP.get(language.lower(), LANGUAGE_CODE_MAP["en"])
    voice_map = _parse_voice_map()
    voice_name = voice_map.get(language.lower())
    logger.info(
        "Synthesizing voiceover language=%s language_code=%s custom_voice=%s output=%s",
        language,
        language_code,
        bool(voice_name),
        output_path,
    )

    client_kwargs: dict[str, Any] = {}
    credentials_path = _resolve_google_credentials_path()
    if credentials_path:
        try:
            from google.oauth2 import service_account
        except Exception as exc:
            raise VoiceoverError("google-auth is required to load service-account credentials") from exc
        credentials = service_account.Credentials.from_service_account_file(
            str(credentials_path),
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        client_kwargs["credentials"] = credentials
        logger.info("Using service account credentials for TTS path=%s", credentials_path)

    client = texttospeech.TextToSpeechClient(**client_kwargs)
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice_kwargs = {"language_code": language_code}
    if voice_name:
        voice_kwargs["name"] = voice_name
    voice = texttospeech.VoiceSelectionParams(**voice_kwargs)
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=1.0,
    )

    response = client.synthesize_speech(
        input=synthesis_input,
        voice=voice,
        audio_config=audio_config,
    )
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(response.audio_content)
    if not out.exists() or out.stat().st_size == 0:
        raise VoiceoverError(f"voiceover synthesis produced empty output for {language}")
    logger.info("Voiceover synthesized language=%s bytes=%s", language, out.stat().st_size)


def generate_commit_media_assets(
    commit_doc: dict[str, Any],
    languages: list[str] | None = None,
) -> dict[str, Any]:
    """Generate script, base video, localized voiceovers, and captions for a commit."""
    target_languages = parse_target_languages(languages)
    logger.info(
        "Commit media generation started commit_id=%s repo=%s languages=%s",
        commit_doc.get("id"),
        commit_doc.get("repo_full_name"),
        target_languages,
    )
    script = generate_scene_script(commit_doc)

    workspace_dir = Path(tempfile.mkdtemp(prefix="diffcast-pipeline-"))
    base_video_path = workspace_dir / "base.mp4"
    video_meta = render_scene_video(script["scenes"], base_video_path)
    scene_durations = [float(scene["duration_sec"]) for scene in script["scenes"]]

    tracks: dict[str, dict[str, Any]] = {}
    for language in target_languages:
        try:
            localized = generate_localized_script(script, language)
            captions_text = build_srt(localized["caption_lines"], scene_durations)
            captions_path = workspace_dir / f"{language}.srt"
            captions_path.write_text(captions_text, encoding="utf-8")

            voice_script = " ".join(localized["voice_lines"]).strip()
            audio_path = workspace_dir / f"{language}.mp3"
            synthesize_voiceover(voice_script, language, audio_path)
            final_video_path = workspace_dir / f"final_{language}.mp4"
            final_video_meta = render_final_localized_video(
                base_video_path=base_video_path,
                audio_path=audio_path,
                captions_path=captions_path,
                output_path=final_video_path,
            )

            tracks[language] = {
                "status": "completed",
                "error": None,
                "voice_script": voice_script,
                "duration_sec": script["total_duration_sec"],
                "audio_path": str(audio_path),
                "captions_path": str(captions_path),
                "final_video_path": str(final_video_path),
                "final_video_meta": final_video_meta,
            }
            logger.info("Language asset generation completed language=%s", language)
        except Exception as exc:
            tracks[language] = {
                "status": "failed",
                "error": str(exc),
                "voice_script": None,
                "duration_sec": script["total_duration_sec"],
                "audio_path": None,
                "captions_path": None,
                "final_video_path": None,
                "final_video_meta": None,
            }
            logger.exception("Language asset generation failed language=%s", language)

    result = {
        "script": script,
        "workspace_dir": str(workspace_dir),
        "base_video_path": str(base_video_path),
        "base_video_meta": video_meta,
        "tracks": tracks,
    }
    logger.info(
        "Commit media generation finished commit_id=%s workspace=%s",
        commit_doc.get("id"),
        result["workspace_dir"],
    )
    return result
