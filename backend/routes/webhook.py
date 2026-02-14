"""GitHub webhook routes."""

import hashlib
import hmac
import os
from datetime import datetime
from dateutil import parser as date_parser

from flask import Blueprint, request, jsonify, current_app

from firebase_schema import CommitDoc, WebhookEventDoc, repo_id
from services import (
    get_commit_diff,
    get_compare_diff,
    get_all_repo_secrets,
    get_repo,
    store_commit,
    store_repo,
    store_webhook_event,
    update_webhook_event,
)

webhook_bp = Blueprint("webhook", __name__, url_prefix="/webhook")


def _verify_signature(payload: bytes, signature: str | None) -> bool:
    """Verify GitHub webhook signature (X-Hub-Signature-256). Tries shared secret first, then per-repo secrets."""
    if not signature or not signature.startswith("sha256="):
        return False

    secrets_to_try = []
    shared = os.environ.get("GITHUB_WEBHOOK_SECRET")
    if shared:
        secrets_to_try.append(shared)
    try:
        for _rid, s in get_all_repo_secrets():
            if s and s not in secrets_to_try:
                secrets_to_try.append(s)
    except Exception as e:
        current_app.logger.warning(f"Could not fetch repo secrets: {e}")

    if not secrets_to_try:
        current_app.logger.warning("No webhook secrets configured - skipping verification")
        return True

    for secret in secrets_to_try:
        expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, signature):
            return True
    return False


def _parse_timestamp(ts: str | None) -> datetime:
    if not ts:
        return datetime.utcnow()
    try:
        return date_parser.parse(ts)
    except Exception:
        return datetime.utcnow()


def _process_push(payload: dict) -> int:
    """Process push event - fetch and store each commit's diff."""
    repo = payload.get("repository", {})
    owner = repo.get("owner", {}).get("login") or repo.get("owner", {}).get("name")
    name = repo.get("name")
    full_name = repo.get("full_name") or f"{owner}/{name}"
    ref = payload.get("ref", "")
    branch = ref.replace("refs/heads/", "") if ref.startswith("refs/heads/") else ref

    store_repo(
        full_name=full_name,
        owner=owner,
        name=name,
        default_branch=repo.get("default_branch", "main"),
    )

    commits = payload.get("commits", [])
    before = payload.get("before")
    after = payload.get("after")

    stored = 0
    if commits:
        for c in commits:
            sha = c.get("id") or c.get("sha")
            if not sha:
                continue
            try:
                raw_diff, files = get_commit_diff(owner, name, sha)
            except Exception as e:
                current_app.logger.warning(f"Failed to fetch diff for {sha}: {e}")
                continue

            commit_data = c.get("commit", {})
            author_data = commit_data.get("author", {})
            author_info = c.get("author") or {}

            doc = CommitDoc(
                sha=sha,
                sha_short=sha[:7],
                repo_id=repo_id(full_name),
                repo_full_name=full_name,
                message=commit_data.get("message", ""),
                author={
                    "name": author_data.get("name", author_info.get("login", "unknown")),
                    "email": author_data.get("email", ""),
                    "avatar_url": author_info.get("avatar_url", ""),
                },
                timestamp=_parse_timestamp(author_data.get("date")),
                branch=branch,
                pr_number=None,
                pr_url=None,
                pr_title=None,
                files=[
                    {
                        "path": f.get("filename", f.get("path", "")),
                        "status": f.get("status", "modified"),
                        "additions": f.get("additions", 0),
                        "deletions": f.get("deletions", 0),
                        "patch": f.get("patch"),
                    }
                    for f in files
                ],
            )
            store_commit(doc)
            stored += 1
    elif before and after and before != "0" * 40:
        # Empty push or force push - try compare
        try:
            compare_commits = get_compare_diff(owner, name, before, after)
            for c in compare_commits:
                doc = CommitDoc(
                    sha=c["sha"],
                    sha_short=c["sha_short"],
                    repo_id=repo_id(full_name),
                    repo_full_name=full_name,
                    message=c["message"],
                    author=c["author"],
                    timestamp=_parse_timestamp(c.get("timestamp")),
                    branch=branch,
                    pr_number=None,
                    pr_url=None,
                    pr_title=None,
                    files=[
                        {
                            "path": f.get("path", ""),
                            "status": f.get("status", "modified"),
                            "additions": f.get("additions", 0),
                            "deletions": f.get("deletions", 0),
                            "patch": f.get("patch"),
                        }
                        for f in c.get("files", [])
                    ],
                )
                store_commit(doc)
                stored += 1
        except Exception as e:
            current_app.logger.warning(f"Compare diff failed: {e}")

    return stored


def _process_pull_request(payload: dict) -> int:
    """Process pull_request event - when merged, fetch each commit's diff."""
    action = payload.get("action")
    if action != "closed":
        return 0

    pr = payload.get("pull_request", {})
    if not pr.get("merged"):
        return 0

    repo = payload.get("repository", {})
    owner = repo.get("owner", {}).get("login") or repo.get("owner", {}).get("name")
    name = repo.get("name")
    full_name = repo.get("full_name") or f"{owner}/{name}"
    pr_num = pr.get("number")
    pr_url = pr.get("html_url", "")
    pr_title = pr.get("title", "")
    base_ref = pr.get("base", {}).get("sha")
    head_ref = pr.get("head", {}).get("sha")

    store_repo(
        full_name=full_name,
        owner=owner,
        name=name,
        default_branch=repo.get("default_branch", "main"),
    )

    stored = 0
    try:
        compare_commits = get_compare_diff(owner, name, base_ref, head_ref)
        for c in compare_commits:
            doc = CommitDoc(
                sha=c["sha"],
                sha_short=c["sha_short"],
                repo_id=repo_id(full_name),
                repo_full_name=full_name,
                message=c["message"],
                author=c["author"],
                timestamp=_parse_timestamp(c.get("timestamp")),
                branch=pr.get("head", {}).get("ref", ""),
                pr_number=pr_num,
                pr_url=pr_url,
                pr_title=pr_title,
                files=[
                    {
                        "path": f.get("path", ""),
                        "status": f.get("status", "modified"),
                        "additions": f.get("additions", 0),
                        "deletions": f.get("deletions", 0),
                        "patch": f.get("patch"),
                    }
                    for f in c.get("files", [])
                ],
            )
            store_commit(doc)
            stored += 1
    except Exception as e:
        current_app.logger.warning(f"PR diff fetch failed: {e}")
        raise

    return stored


@webhook_bp.route("/github", methods=["POST"])
def github_webhook():
    """Receive GitHub webhook events."""
    payload_bytes = request.get_data()
    signature = request.headers.get("X-Hub-Signature-256")
    delivery_id = request.headers.get("X-GitHub-Delivery", "unknown")
    event_type = request.headers.get("X-GitHub-Event", "")

    if not _verify_signature(payload_bytes, signature):
        return jsonify({"error": "Invalid signature"}), 401

    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    repo = payload.get("repository", {})
    repo_full_name = repo.get("full_name", "unknown")
    action = payload.get("action", "")

    event_doc = WebhookEventDoc(
        type=event_type,
        action=action,
        repo_full_name=repo_full_name,
        delivery_id=delivery_id,
        processed=False,
        commits_stored=0,
        created_at=datetime.utcnow(),
    )
    store_webhook_event(event_doc)

    # Check if repo is explicitly disabled (only if already registered)
    repo_doc = get_repo(repo_full_name)
    if repo_doc is not None and repo_doc.get("enabled") is False:
        update_webhook_event(delivery_id, processed=True, commits_stored=0)
        return jsonify({
            "ok": True,
            "event": event_type,
            "commits_stored": 0,
            "skipped": "repo_disabled",
            "delivery_id": delivery_id,
        })

    try:
        if event_type == "push":
            stored = _process_push(payload)
        elif event_type == "pull_request":
            stored = _process_pull_request(payload)
        else:
            stored = 0
            current_app.logger.info(f"Ignoring webhook event: {event_type}")

        update_webhook_event(delivery_id, processed=True, commits_stored=stored)
        return jsonify({
            "ok": True,
            "event": event_type,
            "commits_stored": stored,
            "delivery_id": delivery_id,
        })
    except Exception as e:
        update_webhook_event(delivery_id, processed=False, commits_stored=0, error=str(e))
        current_app.logger.exception("Webhook processing failed")
        return jsonify({"error": str(e), "delivery_id": delivery_id}), 500
