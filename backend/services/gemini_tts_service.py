"""Gemini TTS helpers (AI Studio)."""

from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Mime-type → file extension mapping for Gemini TTS output
_MIME_TO_EXT: dict[str, str] = {
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/mp3": ".mp3",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/l16": ".pcm",
    "audio/pcm": ".pcm",
}


class GeminiTTSError(Exception):
    """Raised when TTS synthesis fails."""


def _build_client() -> Any:
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        raise GeminiTTSError("Missing GEMINI_API_KEY")
    try:
        from google import genai
    except Exception as exc:  # pragma: no cover
        raise GeminiTTSError("google-genai package is required for Gemini TTS") from exc
    return genai.Client(api_key=api_key)


def _extract_audio_data(response: Any) -> tuple[bytes | None, str | None]:
    """Extract audio bytes and mime_type from Gemini TTS response."""
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) if content else None
        if not parts:
            continue
        for part in parts:
            inline_data = getattr(part, "inline_data", None)
            if inline_data is None:
                continue
            mime_type = getattr(inline_data, "mime_type", None)
            data = getattr(inline_data, "data", None)
            if isinstance(data, (bytes, bytearray)):
                return bytes(data), mime_type
            if isinstance(data, str):
                try:
                    return base64.b64decode(data), mime_type
                except Exception:
                    continue
    return None, None


def _parse_mime_type(mime_type: str | None) -> tuple[str, dict[str, str]]:
    """Return lowercased base mime type + lowercase params."""
    raw = (mime_type or "").strip()
    if not raw:
        return "", {}
    parts = [p.strip() for p in raw.split(";") if p.strip()]
    base = parts[0].lower()
    params: dict[str, str] = {}
    for item in parts[1:]:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        params[key.strip().lower()] = value.strip().strip('"').lower()
    return base, params


def _convert_to_wav(input_path: str | Path, output_path: str | Path) -> None:
    """Convert any audio file to WAV PCM s16le 48kHz stereo using ffmpeg."""
    if not shutil.which("ffmpeg"):
        raise GeminiTTSError("ffmpeg is required for audio conversion")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-acodec",
        "pcm_s16le",
        "-ar",
        "48000",
        "-ac",
        "1",
        str(output_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown error").strip()[-500:]
        raise GeminiTTSError(f"Audio conversion to WAV failed: {detail}")


def _convert_pcm_to_wav(
    input_path: str | Path,
    output_path: str | Path,
    sample_rate: int,
    channels: int,
    sample_format: str,
) -> None:
    """Convert raw PCM bytes to WAV with explicit input format."""
    if not shutil.which("ffmpeg"):
        raise GeminiTTSError("ffmpeg is required for audio conversion")
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        sample_format,
        "-ar",
        str(sample_rate),
        "-ac",
        str(channels),
        "-i",
        str(input_path),
        "-acodec",
        "pcm_s16le",
        "-ar",
        "48000",
        "-ac",
        "1",
        str(output_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown error").strip()[-500:]
        raise GeminiTTSError(f"Raw PCM conversion to WAV failed: {detail}")


def _write_l16_wav_bytes(
    audio_bytes: bytes,
    output_path: str | Path,
    sample_rate: int,
    channels: int,
    big_endian: bool,
) -> None:
    """Write raw 16-bit PCM bytes into a WAV container."""
    if not audio_bytes:
        raise GeminiTTSError("PCM audio payload is empty")
    pcm = audio_bytes
    # PCM16 needs even number of bytes.
    if len(pcm) % 2 == 1:
        pcm = pcm[:-1]
    if not pcm:
        raise GeminiTTSError("PCM audio payload is too short")

    # WAV PCM expects little-endian sample bytes.
    if big_endian:
        swapped = bytearray(len(pcm))
        swapped[0::2] = pcm[1::2]
        swapped[1::2] = pcm[0::2]
        pcm = bytes(swapped)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as wav_file:
        wav_file.setnchannels(max(1, channels))
        wav_file.setsampwidth(2)
        wav_file.setframerate(max(8000, sample_rate))
        wav_file.writeframes(pcm)


def synthesize_with_gemini_tts(
    text: str,
    output_path: str | Path,
    language: str = "en",
    voice_name: str = "Kore",
) -> dict[str, Any]:
    """Generate narration audio using Gemini preview TTS models.

    The raw audio from Gemini is saved with its native format then
    converted to WAV PCM (s16le, 48 kHz, mono) so downstream ffmpeg
    commands can reliably consume it.
    """
    if not text.strip():
        raise GeminiTTSError("TTS text must be non-empty")

    model = os.environ.get("PIPELINE_TTS_MODEL", "gemini-2.5-flash-preview-tts")
    fallback_model = os.environ.get("PIPELINE_TTS_MODEL_FALLBACK", "gemini-2.5-pro-preview-tts")
    client = _build_client()

    config = {
        "response_modalities": ["AUDIO"],
        "speech_config": {
            "voice_config": {
                "prebuilt_voice_config": {
                    "voice_name": voice_name,
                }
            }
        },
    }

    errors: list[str] = []
    for model_name in (model, fallback_model):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=f"Language: {language}\n\n{text}",
                config=config,
            )
            audio, mime_type = _extract_audio_data(response)
            if not audio:
                raise GeminiTTSError("Gemini returned no audio data")

            logger.info(
                "Gemini TTS raw audio received model=%s mime_type=%s bytes=%s",
                model_name,
                mime_type,
                len(audio),
            )

            mime_base, mime_params = _parse_mime_type(mime_type)
            src_ext = _MIME_TO_EXT.get(mime_base, ".raw")

            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)

            # Save raw bytes to temp file with correct extension, then convert
            with tempfile.TemporaryDirectory(prefix="diffcast-tts-") as tmp_dir:
                raw_path = Path(tmp_dir) / f"tts_raw{src_ext}"
                raw_path.write_bytes(audio)
                if mime_base in {"audio/l16", "audio/pcm"}:
                    sample_rate = int(mime_params.get("rate", "24000"))
                    channels = int(mime_params.get("channels", "1"))
                    if mime_base == "audio/l16":
                        _write_l16_wav_bytes(
                            audio_bytes=audio,
                            output_path=out,
                            sample_rate=sample_rate,
                            channels=channels,
                            big_endian=True,
                        )
                    else:
                        _write_l16_wav_bytes(
                            audio_bytes=audio,
                            output_path=out,
                            sample_rate=sample_rate,
                            channels=channels,
                            big_endian=False,
                        )
                else:
                    _convert_to_wav(raw_path, out)

            logger.info("Gemini TTS synthesis completed model=%s output=%s", model_name, out)
            return {
                "path": str(out),
                "model": model_name,
                "voice": voice_name,
            }
        except Exception as exc:  # noqa: PERF203
            errors.append(f"{model_name}: {exc}")

    raise GeminiTTSError(f"Gemini TTS failed across models: {' | '.join(errors)}")
