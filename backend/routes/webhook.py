"""GitHub webhook routes."""

import hashlib
import hmac
import os
from datetime import datetime
from dateutil import parser as date_parser

from flask import Blueprint, request, jsonify, current_app

from firebase_schema import CommitDoc, WebhookEventDoc, commit_id, repo_id
from services import (
    enqueue_commit_pipeline,
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


def _process_push(payload: dict) -> tuple[int, str | None]:
    """Process push event - fetch and store each commit's diff and return latest commit ID."""
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
    current_app.logger.info(
        "Processing push payload repo=%s branch=%s commits=%s before=%s after=%s",
        full_name,
        branch,
        len(commits),
        before[:7] if isinstance(before, str) else before,
        after[:7] if isinstance(after, str) else after,
    )

    stored = 0
    stored_shas: list[str] = []
    if commits:
        for c in commits:
            sha = c.get("id") or c.get("sha")
            if not sha:
                continue
            try:
                raw_diff, files, commit_meta = get_commit_diff(owner, name, sha)
            except Exception as e:
                current_app.logger.warning(f"Failed to fetch diff for {sha}: {e}")
                continue

            doc = CommitDoc(
                sha=sha,
                sha_short=sha[:7],
                repo_id=repo_id(full_name),
                repo_full_name=full_name,
                message=commit_meta.get("message", c.get("message", "")),
                author=commit_meta.get("author", {"name": "unknown", "email": "", "avatar_url": ""}),
                timestamp=_parse_timestamp(commit_meta.get("timestamp")),
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
            stored_shas.append(sha)
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
                stored_shas.append(c["sha"])
        except Exception as e:
            current_app.logger.warning(f"Compare diff failed: {e}")

    latest_sha = after if isinstance(after, str) and after and after != "0" * 40 else None
    if latest_sha not in stored_shas:
        latest_sha = stored_shas[-1] if stored_shas else None
    latest_commit_doc_id = commit_id(full_name, latest_sha) if latest_sha else None
    current_app.logger.info(
        "Push processed repo=%s commits_stored=%s latest_commit_id=%s",
        full_name,
        stored,
        latest_commit_doc_id,
    )

    return stored, latest_commit_doc_id


def _process_pull_request(payload: dict) -> int:
    """Process pull_request event - when merged, fetch each commit's diff."""
    action = payload.get("action")
    if action != "closed":
        current_app.logger.info("Skipping pull_request action=%s (only closed handled)", action)
        return 0

    pr = payload.get("pull_request", {})
    if not pr.get("merged"):
        current_app.logger.info("Skipping unmerged pull_request number=%s", pr.get("number"))
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
        current_app.logger.info(
            "Processing merged pull_request repo=%s pr_number=%s base=%s head=%s",
            full_name,
            pr_num,
            base_ref[:7] if isinstance(base_ref, str) else base_ref,
            head_ref[:7] if isinstance(head_ref, str) else head_ref,
        )
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

    current_app.logger.info(
        "Merged pull_request processed repo=%s pr_number=%s commits_stored=%s",
        full_name,
        pr_num,
        stored,
    )

    return stored


@webhook_bp.route("/github", methods=["POST"])
def github_webhook():
    """Receive GitHub webhook events."""
    payload_bytes = request.get_data()
    signature = request.headers.get("X-Hub-Signature-256")
    delivery_id = request.headers.get("X-GitHub-Delivery", "unknown")
    event_type = request.headers.get("X-GitHub-Event", "")
    current_app.logger.info(
        "Webhook received delivery_id=%s event_type=%s",
        delivery_id,
        event_type,
    )

    if not _verify_signature(payload_bytes, signature):
        current_app.logger.warning(
            "Webhook signature verification failed delivery_id=%s event_type=%s",
            delivery_id,
            event_type,
        )
        return jsonify({"error": "Invalid signature"}), 401

    try:
        payload = request.get_json(force=True)
    except Exception:
        current_app.logger.warning(
            "Webhook payload invalid JSON delivery_id=%s event_type=%s",
            delivery_id,
            event_type,
        )
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
    current_app.logger.info(
        "Webhook event stored delivery_id=%s repo=%s action=%s",
        delivery_id,
        repo_full_name,
        action,
    )

    # Check if repo is explicitly disabled (only if already registered)
    repo_doc = get_repo(repo_full_name)
    if repo_doc is not None and repo_doc.get("enabled") is False:
        update_webhook_event(delivery_id, processed=True, commits_stored=0)
        current_app.logger.info(
            "Webhook skipped because repo disabled delivery_id=%s repo=%s",
            delivery_id,
            repo_full_name,
        )
        return jsonify({
            "ok": True,
            "event": event_type,
            "commits_stored": 0,
            "skipped": "repo_disabled",
            "delivery_id": delivery_id,
        })

    try:
        pipeline_job = None
        if event_type == "push":
            stored, latest_commit_doc_id = _process_push(payload)
            if latest_commit_doc_id:
                try:
                    pipeline_job = enqueue_commit_pipeline(commit_id=latest_commit_doc_id)
                    current_app.logger.info(
                        "Pipeline enqueue attempted delivery_id=%s commit_id=%s queued=%s skipped=%s",
                        delivery_id,
                        latest_commit_doc_id,
                        pipeline_job.get("queued"),
                        pipeline_job.get("skipped"),
                    )
                except Exception as enqueue_error:
                    current_app.logger.warning(
                        "Failed to enqueue media pipeline for %s: %s",
                        latest_commit_doc_id,
                        enqueue_error,
                    )
        elif event_type == "pull_request":
            stored = _process_pull_request(payload)
        else:
            stored = 0
            current_app.logger.info(f"Ignoring webhook event: {event_type}")

        update_webhook_event(delivery_id, processed=True, commits_stored=stored)
        current_app.logger.info(
            "Webhook processed delivery_id=%s event_type=%s commits_stored=%s",
            delivery_id,
            event_type,
            stored,
        )
        return jsonify({
            "ok": True,
            "event": event_type,
            "commits_stored": stored,
            "delivery_id": delivery_id,
            "pipeline_job": pipeline_job,
        })
    except Exception as e:
        update_webhook_event(delivery_id, processed=False, commits_stored=0, error=str(e))
        current_app.logger.exception(
            "Webhook processing failed delivery_id=%s event_type=%s repo=%s",
            delivery_id,
            event_type,
            repo_full_name,
        )
        return jsonify({"error": str(e), "delivery_id": delivery_id}), 500
