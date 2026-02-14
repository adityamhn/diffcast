"""Webhook tests for pipeline enqueue behavior."""

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


class WebhookPipelineTests(unittest.TestCase):
    """Ensure webhook push events enqueue commit media jobs."""

    def setUp(self) -> None:
        self.app = create_app("development")
        self.client = self.app.test_client()
        self.payload = {
            "repository": {
                "full_name": "octo/repo",
                "name": "repo",
                "owner": {"login": "octo"},
            },
            "ref": "refs/heads/main",
            "after": "abc1234567890abc1234567890abc1234567890",
            "commits": [],
        }
        self.headers = {
            "X-Hub-Signature-256": "sha256=fake",
            "X-GitHub-Delivery": "delivery-1",
            "X-GitHub-Event": "push",
        }

    def test_push_enqueues_latest_commit_pipeline(self) -> None:
        with patch("routes.webhook._verify_signature", return_value=True):
            with patch("routes.webhook.store_webhook_event", return_value="delivery-1"):
                with patch("routes.webhook.update_webhook_event", return_value=None):
                    with patch("routes.webhook.get_repo", return_value=None):
                        with patch("routes.webhook._process_push", return_value=(1, "octo_repo_abc1234")):
                            with patch(
                                "routes.webhook.enqueue_commit_pipeline",
                                return_value={"video_id": "octo_repo_abc1234", "status": "queued"},
                            ) as enqueue_mock:
                                response = self.client.post(
                                    "/webhook/github",
                                    json=self.payload,
                                    headers=self.headers,
                                )

        self.assertEqual(response.status_code, 200)
        enqueue_mock.assert_called_once_with(commit_id="octo_repo_abc1234")
        body = response.get_json()
        self.assertEqual(body["commits_stored"], 1)

    def test_pull_request_does_not_enqueue_pipeline(self) -> None:
        headers = {**self.headers, "X-GitHub-Event": "pull_request"}
        payload = {
            "repository": {"full_name": "octo/repo"},
            "action": "closed",
            "pull_request": {"merged": True},
        }
        with patch("routes.webhook._verify_signature", return_value=True):
            with patch("routes.webhook.store_webhook_event", return_value="delivery-1"):
                with patch("routes.webhook.update_webhook_event", return_value=None):
                    with patch("routes.webhook.get_repo", return_value=None):
                        with patch("routes.webhook._process_pull_request", return_value=2):
                            with patch("routes.webhook.enqueue_commit_pipeline") as enqueue_mock:
                                response = self.client.post(
                                    "/webhook/github",
                                    json=payload,
                                    headers=headers,
                                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(enqueue_mock.call_count, 0)
        body = response.get_json()
        self.assertEqual(body["commits_stored"], 2)


if __name__ == "__main__":
    unittest.main()

