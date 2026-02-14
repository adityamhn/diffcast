"""Route tests for pipeline endpoints."""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

app_module = importlib.import_module("app")
create_app = app_module.create_app


class PipelineRouteTests(unittest.TestCase):
    """Test pipeline route input and response behavior."""

    def setUp(self) -> None:
        self.app = create_app("development")
        self.client = self.app.test_client()

    def test_post_pipeline_commit_with_commit_id(self) -> None:
        enqueue_response = {
            "queued": True,
            "skipped": False,
            "video_id": "octo_repo_abc1234",
            "commit_id": "octo_repo_abc1234",
            "status": "queued",
        }
        with patch("routes.pipeline.enqueue_commit_pipeline", return_value=enqueue_response):
            with patch("routes.pipeline.get_video", return_value={"id": "octo_repo_abc1234", "status": "queued"}):
                response = self.client.post(
                    "/api/pipeline/commit",
                    json={"commit_id": "octo_repo_abc1234", "languages": ["en"]},
                )

        self.assertEqual(response.status_code, 202)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["video_id"], "octo_repo_abc1234")

    def test_post_pipeline_commit_with_owner_repo_sha(self) -> None:
        enqueue_response = {
            "queued": True,
            "skipped": False,
            "video_id": "octo_repo_abc1234",
            "commit_id": "octo_repo_abc1234",
            "status": "queued",
        }
        with patch("routes.pipeline.enqueue_commit_pipeline", return_value=enqueue_response) as enqueue_mock:
            with patch("routes.pipeline.get_video", return_value={"id": "octo_repo_abc1234", "status": "queued"}):
                response = self.client.post(
                    "/api/pipeline/commit",
                    json={"owner": "octo", "repo": "repo", "sha": "abc1234567890"},
                )

        self.assertEqual(response.status_code, 202)
        enqueue_mock.assert_called_once()
        kwargs = enqueue_mock.call_args.kwargs
        self.assertEqual(kwargs["commit_id"], "octo_repo_abc1234")

    def test_get_video_status_not_found(self) -> None:
        with patch("routes.pipeline.get_video", return_value=None):
            response = self.client.get("/api/videos/unknown_id")
        self.assertEqual(response.status_code, 404)

    def test_post_pipeline_ingest_base_video(self) -> None:
        ingest_response = {
            "queued": True,
            "skipped": False,
            "video_id": "octo_repo_abc1234",
            "commit_id": "octo_repo_abc1234",
            "status": "running",
            "stage": "normalize_video",
        }
        with patch("routes.pipeline.enqueue_ingest_pipeline", return_value=ingest_response):
            with patch("routes.pipeline.get_video", return_value={"id": "octo_repo_abc1234", "status": "running"}):
                response = self.client.post(
                    "/api/pipeline/ingest-base-video",
                    json={
                        "commit_id": "octo_repo_abc1234",
                        "source_video": {"kind": "local_path", "uri": "./backend/tests/videos/testvideo.mp4"},
                        "languages": ["en"],
                    },
                )

        self.assertEqual(response.status_code, 202)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["video_id"], "octo_repo_abc1234")

    def test_post_pipeline_ingest_base_video_rejects_invalid_kind(self) -> None:
        response = self.client.post(
            "/api/pipeline/ingest-base-video",
            json={
                "commit_id": "octo_repo_abc1234",
                "source_video": {"kind": "ftp", "uri": "foo"},
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_list_repo_videos(self) -> None:
        fake_videos = [{"id": "octo_repo_abc1234", "status": "completed"}]
        with patch("routes.pipeline.list_videos", return_value=fake_videos):
            response = self.client.get("/api/repos/octo/repo/videos?limit=10")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["count"], 1)


if __name__ == "__main__":
    unittest.main()
