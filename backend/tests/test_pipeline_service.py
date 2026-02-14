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

    def test_ingest_prerequisites_backfills_missing_plan(self) -> None:
        commit_doc = {
            "id": "octo_repo_abc1234",
            "repo_full_name": "octo/repo",
            "repo_id": "octo_repo",
            "sha": "abc1234567890",
            "sha_short": "abc1234",
        }
        existing = {"script": {"title": "Update Title"}, "enhancement_plan": None}
        fake_plan = {"timeline": [{"start_sec": 0, "end_sec": 2, "text": "hello"}]}

        with patch.object(pipeline_module, "generate_shot_plan", return_value=fake_plan) as shot_plan_mock:
            with patch.object(pipeline_module, "update_video_status") as update_mock:
                result = pipeline_module._ensure_ingest_prerequisites(
                    commit_doc_id="octo_repo_abc1234",
                    video_doc_id="octo_repo_abc1234",
                    commit_doc=commit_doc,
                    existing=existing,
                )

        self.assertIsNotNone(result)
        script, shot_plan = result
        self.assertEqual(script, existing["script"])
        self.assertEqual(shot_plan, fake_plan)
        shot_plan_mock.assert_called_once()
        update_mock.assert_called_once()

    def test_ingest_prerequisites_backfill_failure_marks_failed(self) -> None:
        commit_doc = {
            "id": "octo_repo_abc1234",
            "repo_full_name": "octo/repo",
            "repo_id": "octo_repo",
            "sha": "abc1234567890",
            "sha_short": "abc1234",
        }
        existing = {"script": {"title": "Update Title"}, "enhancement_plan": None}

        with patch.object(pipeline_module, "generate_shot_plan", side_effect=RuntimeError("plan generation failed")):
            with patch.object(pipeline_module, "update_video_status") as update_mock:
                result = pipeline_module._ensure_ingest_prerequisites(
                    commit_doc_id="octo_repo_abc1234",
                    video_doc_id="octo_repo_abc1234",
                    commit_doc=commit_doc,
                    existing=existing,
                )

        self.assertIsNone(result)
        update_mock.assert_called_once()
        kwargs = update_mock.call_args.kwargs
        self.assertEqual(kwargs["status"], "failed")
        self.assertEqual(kwargs["stage"], "error")
        self.assertIn("plan generation failed", kwargs["error"])


if __name__ == "__main__":
    unittest.main()

