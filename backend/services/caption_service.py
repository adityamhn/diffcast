"""Caption generation helpers for cinematic pipeline."""

from __future__ import annotations

import textwrap
from typing import Any


class CaptionError(Exception):
    """Raised for caption timing/formatting failures."""


def _format_srt_timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    hours = total_ms // 3_600_000
    minutes = (total_ms % 3_600_000) // 60_000
    secs = (total_ms % 60_000) // 1000
    millis = total_ms % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _wrap_caption_line(text: str, width: int = 42, max_lines: int = 2) -> str:
    wrapped = textwrap.wrap(text.strip(), width=width) or [text.strip()]
    wrapped = wrapped[:max_lines]
    if len(wrapped) == max_lines and len(textwrap.wrap(text.strip(), width=width)) > max_lines:
        wrapped[-1] = wrapped[-1][: max(0, width - 1)] + "…"
    return "\n".join(wrapped)


def build_srt_from_timeline(segments: list[dict[str, Any]]) -> str:
    """Build SRT from timeline segments containing narration lines and durations."""
    if not segments:
        raise CaptionError("segments must be non-empty")

    chunks: list[str] = []
    current = 0.0
    idx = 1
    for segment in segments:
        duration = float(segment.get("duration_sec", 0))
        if duration <= 0:
            continue
        line = str(segment.get("narration", "")).strip()
        if not line:
            current += duration
            continue

        start = _format_srt_timestamp(current)
        end = _format_srt_timestamp(current + duration)
        caption_text = _wrap_caption_line(line)
        chunks.append(f"{idx}\n{start} --> {end}\n{caption_text}")
        idx += 1
        current += duration

    if not chunks:
        raise CaptionError("No captionable narration found in timeline")
    return "\n\n".join(chunks).strip() + "\n"
