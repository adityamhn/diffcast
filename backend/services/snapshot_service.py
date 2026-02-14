"""Snapshot extraction from video files using ffmpeg."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SnapshotError(Exception):
    """Raised when snapshot extraction fails."""


def _run(cmd: list[str], error_prefix: str) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown error").strip()
        raise SnapshotError(f"{error_prefix}: {detail}")
    return proc


def _probe_duration(video_path: str | Path) -> float:
    """Get video duration in seconds using ffprobe."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(video_path),
    ]
    proc = _run(cmd, "ffprobe duration failed")
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise SnapshotError("ffprobe returned invalid json") from exc

    duration = float((payload.get("format") or {}).get("duration") or 0)
    if duration <= 0:
        raise SnapshotError("video has no valid duration")
    return duration


def extract_snapshots(
    video_path: str | Path,
    output_dir: str | Path,
    num_snapshots: int = 3,
    strategy: str = "uniform",
) -> list[Path]:
    """Extract N frames from video using ffmpeg.

    Args:
        video_path: Path to input video file
        output_dir: Directory where snapshot PNGs will be saved
        num_snapshots: Number of snapshots to extract (default 3)
        strategy: Extraction strategy - "uniform" for evenly spaced frames

    Returns:
        List of paths to extracted PNG snapshots, ordered by timestamp

    Strategies:
        - uniform: Evenly spaced frames (e.g., for 3 snapshots: 25%, 50%, 75%)
    """
    video_path = Path(video_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not video_path.exists():
        raise SnapshotError(f"video file not found: {video_path}")

    duration = _probe_duration(video_path)
    logger.info(
        "Extracting snapshots video=%s duration=%.2fs num=%d strategy=%s",
        video_path.name,
        duration,
        num_snapshots,
        strategy,
    )

    if strategy == "uniform":
        return _extract_uniform(video_path, output_dir, duration, num_snapshots)
    else:
        raise SnapshotError(f"unsupported snapshot strategy: {strategy}")


def _extract_uniform(
    video_path: Path,
    output_dir: Path,
    duration: float,
    num_snapshots: int,
) -> list[Path]:
    """Extract evenly spaced frames from video.

    For 3 snapshots in a 10s video:
    - Snapshot 1: 2.5s (25%)
    - Snapshot 2: 5.0s (50%)
    - Snapshot 3: 7.5s (75%)
    """
    if num_snapshots < 1:
        raise SnapshotError("num_snapshots must be at least 1")

    snapshots: list[Path] = []

    # Calculate timestamps for uniform distribution
    # Avoid very start/end by using (i+1)/(num+1) distribution
    for i in range(num_snapshots):
        fraction = (i + 1) / (num_snapshots + 1)
        timestamp = duration * fraction
        output_path = output_dir / f"snapshot_{i:02d}.png"

        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            str(video_path),
            "-vframes",
            "1",
            "-q:v",
            "2",
            str(output_path),
        ]
        _run(cmd, f"snapshot extraction failed at {timestamp:.2f}s")

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise SnapshotError(f"snapshot file missing or empty: {output_path}")

        snapshots.append(output_path)
        logger.debug("Extracted snapshot index=%d timestamp=%.2fs path=%s", i, timestamp, output_path)

    logger.info("Extracted %d snapshots to %s", len(snapshots), output_dir)
    return snapshots


def extract_snapshot_at_timestamp(
    video_path: str | Path,
    output_path: str | Path,
    timestamp_sec: float,
) -> Path:
    """Extract a single frame at a specific timestamp.

    Args:
        video_path: Path to input video file
        output_path: Path where snapshot PNG will be saved
        timestamp_sec: Timestamp in seconds to extract frame from

    Returns:
        Path to extracted PNG snapshot
    """
    video_path = Path(video_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not video_path.exists():
        raise SnapshotError(f"video file not found: {video_path}")

    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{timestamp_sec:.3f}",
        "-i",
        str(video_path),
        "-vframes",
        "1",
        "-q:v",
        "2",
        str(output_path),
    ]
    _run(cmd, f"snapshot extraction failed at {timestamp_sec:.2f}s")

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise SnapshotError(f"snapshot file missing or empty: {output_path}")

    logger.info("Extracted snapshot at %.2fs to %s", timestamp_sec, output_path)
    return output_path


def get_snapshot_metadata(snapshot_path: str | Path) -> dict[str, Any]:
    """Get metadata for a snapshot image.

    Returns:
        Dict with width, height, and file_size_bytes
    """
    snapshot_path = Path(snapshot_path)
    if not snapshot_path.exists():
        raise SnapshotError(f"snapshot not found: {snapshot_path}")

    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        str(snapshot_path),
    ]
    proc = _run(cmd, "ffprobe snapshot failed")
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise SnapshotError("ffprobe returned invalid json") from exc

    streams = payload.get("streams") or []
    if not streams:
        raise SnapshotError("no video stream in snapshot")

    stream = streams[0]
    return {
        "width": int(stream.get("width", 0)),
        "height": int(stream.get("height", 0)),
        "file_size_bytes": snapshot_path.stat().st_size,
    }
