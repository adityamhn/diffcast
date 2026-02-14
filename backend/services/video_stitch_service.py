"""Video normalization, cinematic stitching, and mux helpers."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)


class VideoStitchError(Exception):
    """Raised when ffmpeg/ffprobe operations fail."""


def _run(cmd: list[str], error_prefix: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown error").strip()
        raise VideoStitchError(f"{error_prefix}: {detail}")


def ensure_ffmpeg() -> None:
    if not shutil.which("ffmpeg"):
        raise VideoStitchError("ffmpeg is required")
    if not shutil.which("ffprobe"):
        raise VideoStitchError("ffprobe is required")


def probe_video(video_path: str | Path) -> dict[str, Any]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,width,height",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(video_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise VideoStitchError(f"ffprobe failed: {(proc.stderr or '').strip()}")
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise VideoStitchError("ffprobe returned invalid json") from exc

    streams = payload.get("streams") or []
    vstream = next((s for s in streams if s.get("codec_type") == "video"), {})
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    duration = float((payload.get("format") or {}).get("duration") or 0)
    width = int(vstream.get("width") or 0)
    height = int(vstream.get("height") or 0)
    if duration <= 0 or width <= 0 or height <= 0:
        raise VideoStitchError("invalid video metadata")
    return {
        "duration_sec": round(duration, 2),
        "width": width,
        "height": height,
        "has_audio": has_audio,
    }


def normalize_video(input_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    """Normalize input clip to 720p/30fps h264+aac and ensure an audio track exists."""
    ensure_ffmpeg()
    meta = probe_video(input_path)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    common_vf = "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:black,fps=30,format=yuv420p"
    if meta["has_audio"]:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-vf",
            common_vf,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(out),
        ]
    else:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-f",
            "lavfi",
            "-t",
            str(meta["duration_sec"]),
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-vf",
            common_vf,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-shortest",
            str(out),
        ]

    _run(cmd, "normalize video failed")
    return probe_video(out)


def render_title_card(text: str, output_path: str | Path, duration_sec: float = 2.0) -> dict[str, Any]:
    """Render a minimal keynote-like title card clip with silent audio."""
    ensure_ffmpeg()
    with tempfile.TemporaryDirectory(prefix="diffcast-card-") as temp_dir:
        image_path = Path(temp_dir) / "card.png"
        img = Image.new("RGB", (1280, 720), "#0a0f1d")
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 58)
        except Exception:
            font = ImageFont.load_default()
        txt = (text or "Product Update").strip()[:80]
        bbox = draw.textbbox((0, 0), txt, font=font)
        x = (1280 - (bbox[2] - bbox[0])) // 2
        y = (720 - (bbox[3] - bbox[1])) // 2
        draw.text((x, y), txt, fill="#ffffff", font=font)
        img.save(str(image_path), "PNG")

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            str(image_path),
            "-f",
            "lavfi",
            "-t",
            str(duration_sec),
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-t",
            str(duration_sec),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(out),
        ]
        _run(cmd, "render title card failed")

    return probe_video(out)


def trim_video(input_path: str | Path, output_path: str | Path, duration_sec: float) -> dict[str, Any]:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-t",
        str(max(0.5, duration_sec)),
        "-c",
        "copy",
        str(out),
    ]
    _run(cmd, "trim video failed")
    return probe_video(out)


def concat_videos(video_paths: list[str | Path], output_path: str | Path) -> dict[str, Any]:
    if not video_paths:
        raise VideoStitchError("video_paths must be non-empty")

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="diffcast-concat-") as temp_dir:
        concat_file = Path(temp_dir) / "concat.txt"
        concat_file.write_text(
            "\n".join(f"file '{Path(path).resolve().as_posix()}'" for path in video_paths),
            encoding="utf-8",
        )
        cmd = [
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
            "-c:a",
            "aac",
            str(out),
        ]
        _run(cmd, "concat videos failed")

    return probe_video(out)


def mix_with_narration(
    base_video_path: str | Path,
    narration_audio_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    """Keep base audio ambience and Veo audio, duck under narration."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(base_video_path),
        "-i",
        str(narration_audio_path),
        "-filter_complex",
        "[0:a][1:a]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=350[ducked];"
        "[ducked][1:a]amix=inputs=2:weights='0.7 1.0':normalize=0[aout]",
        "-map",
        "0:v:0",
        "-map",
        "[aout]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        str(out),
    ]
    _run(cmd, "mix narration failed")
    return probe_video(out)


def _escape_ffmpeg_filter_path(path: str) -> str:
    """Escape a file path for use inside an ffmpeg filter expression.

    The subtitles filter (libass) treats  :  \\  '  [  ]  ;  ,
    as special characters.  Each must be backslash-escaped.
    """
    out: list[str] = []
    for ch in path:
        if ch in r":\';[],":
            out.append("\\")
        out.append(ch)
    return "".join(out)


def burn_captions(
    input_video_path: str | Path,
    captions_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    """Burn SRT subtitles into the video. Uses explicit filename= for FFmpeg 8.x compatibility."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    srt_path = Path(captions_path).resolve()
    escaped_srt = _escape_ffmpeg_filter_path(srt_path.as_posix())
    # FFmpeg 8.x subtitles filter expects explicit filename= to avoid "No option name" parse errors.
    vf_arg = f"subtitles=filename={escaped_srt}"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_video_path),
        "-vf",
        vf_arg,
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        str(out),
    ]
    _run(cmd, "caption burn failed")
    return probe_video(out)


def assemble_feature_video(
    opener_clip: str | Path,
    demo_video: str | Path,
    closing_clips: list[str | Path],
    output_path: str | Path,
) -> dict[str, Any]:
    """Assemble final feature video from Veo clips and demo recording.

    Final structure: [Veo Opener 6s] → [Demo ~10s] → [Veo Conclusion 6s]

    All clips are normalized to ensure consistent format before concatenation.

    Args:
        opener_clip: Path to Veo opener clip (6s)
        demo_video: Path to browser-use demo recording
        closing_clips: List of paths to closing Veo clips (1 clip, 6s)
        output_path: Where to save the assembled video

    Returns:
        Dict with video metadata (duration_sec, width, height, has_audio)
    """
    ensure_ffmpeg()
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    logger.info(
        "Assembling feature video opener=%s demo=%s closing_count=%d output=%s",
        opener_clip,
        demo_video,
        len(closing_clips),
        out,
    )

    with tempfile.TemporaryDirectory(prefix="diffcast-assemble-") as temp_dir:
        temp_root = Path(temp_dir)
        normalized_paths: list[Path] = []

        # Normalize opener
        opener_norm = temp_root / "opener_norm.mp4"
        normalize_video(opener_clip, opener_norm)
        normalized_paths.append(opener_norm)
        logger.debug("Normalized opener: %s", opener_norm)

        # Normalize demo
        demo_norm = temp_root / "demo_norm.mp4"
        normalize_video(demo_video, demo_norm)
        normalized_paths.append(demo_norm)
        logger.debug("Normalized demo: %s", demo_norm)

        # Normalize closing clips
        for i, clip in enumerate(closing_clips):
            clip_norm = temp_root / f"closing_{i:02d}_norm.mp4"
            normalize_video(clip, clip_norm)
            normalized_paths.append(clip_norm)
            logger.debug("Normalized closing clip %d: %s", i, clip_norm)

        # Concatenate all normalized clips
        result = concat_videos(normalized_paths, out)

    logger.info(
        "Feature video assembled output=%s duration=%.2fs",
        out,
        result["duration_sec"],
    )
    return result
