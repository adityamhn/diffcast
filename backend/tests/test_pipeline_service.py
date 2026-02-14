"""Unit tests for pipeline queue orchestration."""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

pipeline_module = importlib.import_module("services.pipeline_service")
enqueue_commit_pipeline = pipeline_module.enqueue_commit_pipeline


class PipelineServiceTests(unittest.TestCase):
    """Test enqueue behavior and idempotency guards."""

    def test_enqueue_commit_pipeline_queues_job(self) -> None:
        commit_doc = {
            "id": "octo_repo_abc1234",
            "repo_full_name": "octo/repo",
            "repo_id": "octo_repo",
            "sha": "abc1234567890",
            "sha_short": "abc1234",
        }
        fake_executor = Mock()
        fake_executor.submit.return_value = Mock()

        with patch.object(pipeline_module, "get_commit_by_id", return_value=commit_doc):
            with patch.object(pipeline_module, "get_video", return_value=None):
                with patch.object(pipeline_module, "upsert_video_doc", return_value="octo_repo_abc1234"):
                    with patch.object(pipeline_module, "parse_target_languages", return_value=["en"]):
                        with patch.object(pipeline_module, "_EXECUTOR", fake_executor):
                            result = enqueue_commit_pipeline("octo_repo_abc1234")

        self.assertTrue(result["queued"])
        self.assertEqual(result["video_id"], "octo_repo_abc1234")
        self.assertEqual(result["languages_requested"], ["en"])
        self.assertEqual(fake_executor.submit.call_count, 1)

    def test_enqueue_commit_pipeline_skips_when_running(self) -> None:
        commit_doc = {
            "id": "octo_repo_abc1234",
            "repo_full_name": "octo/repo",
            "repo_id": "octo_repo",
            "sha": "abc1234567890",
            "sha_short": "abc1234",
        }
        existing_video = {"status": "running"}

        with patch.object(pipeline_module, "get_commit_by_id", return_value=commit_doc):
            with patch.object(pipeline_module, "get_video", return_value=existing_video):
                result = enqueue_commit_pipeline("octo_repo_abc1234")

        self.assertFalse(result["queued"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "already_running_or_completed")


if __name__ == "__main__":
    unittest.main()

