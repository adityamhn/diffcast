"""Unit tests for media generation helpers."""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

media_module = importlib.import_module("services.media_generation_service")
ScriptValidationError = media_module.ScriptValidationError
build_scene_ffmpeg_command = media_module.build_scene_ffmpeg_command
build_srt = media_module.build_srt
parse_target_languages = media_module.parse_target_languages
validate_scene_script = media_module.validate_scene_script


class MediaGenerationServiceTests(unittest.TestCase):
    """Test language parsing and script/caption validation helpers."""

    def test_parse_target_languages_uses_default_en(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(parse_target_languages(None), ["en"])

    def test_parse_target_languages_from_env(self) -> None:
        with patch.dict(os.environ, {"PIPELINE_LANGUAGES": "en, es,fr,en"}):
            self.assertEqual(parse_target_languages(None), ["en", "es", "fr"])

    def test_validate_scene_script_rejects_jargon(self) -> None:
        bad_payload = {
            "title": "Backend API updates",
            "feature_summary": "Better endpoint behavior",
            "scenes": [
                {
                    "on_screen_text": "New API endpoint is live",
                    "narration_seed": "The backend refactor reduced response time",
                    "duration_sec": 5,
                }
            ],
        }
        with self.assertRaises(ScriptValidationError):
            validate_scene_script(bad_payload)

    def test_build_scene_ffmpeg_command_uses_image_input(self) -> None:
        cmd = build_scene_ffmpeg_command(
            frame_image_path="frame.png",
            duration_sec=6,
            output_path="scene.mp4",
        )
        self.assertEqual(cmd[0], "ffmpeg")
        self.assertIn("frame.png", cmd)
        self.assertIn("scene.mp4", cmd)
        self.assertIn("-loop", cmd)
        # No drawtext filter should be present
        self.assertFalse(any("drawtext=" in item for item in cmd))

    def test_build_srt_aligns_with_durations(self) -> None:
        srt = build_srt(
            caption_lines=["Welcome to the update", "Now sharing is simpler"],
            scene_durations=[4, 5.5],
        )
        self.assertIn("00:00:00,000 --> 00:00:04,000", srt)
        self.assertIn("00:00:04,000 --> 00:00:09,500", srt)


if __name__ == "__main__":
    unittest.main()
