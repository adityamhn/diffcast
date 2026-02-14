"""Firebase Firestore service for storing webhook and commit data."""

import os
from datetime import datetime
from typing import Optional

from firebase_schema import (
    CommitDoc,
    RepoDoc,
    WebhookEventDoc,
    commit_id,
    repo_id,
)


def _get_db():
    """Lazy-init Firestore. Requires GOOGLE_APPLICATION_CREDENTIALS or FIREBASE_SERVICE_ACCOUNT_PATH."""
    import firebase_admin
    from firebase_admin import credentials, firestore

    if not firebase_admin._apps:
        cred_path = os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH") or os.environ.get(
            "GOOGLE_APPLICATION_CREDENTIALS"
        )
        if cred_path and os.path.exists(cred_path):
            cred = credentials.Certificate(cred_path)
            firebase_admin.initialize_app(cred)
        else:
            # For local dev: use default credentials (gcloud auth application-default login)
            firebase_admin.initialize_app()
    return firestore.client()


def _to_dict(obj, exclude_none=True):
    """Convert dataclass to dict, optionally excluding None values."""
    d = {}
    for k, v in obj.__dict__.items():
        if exclude_none and v is None:
            continue
        if isinstance(v, datetime):
            d[k] = v
        elif hasattr(v, "__dict__") and not isinstance(v, (str, int, float, bool, list, dict)):
            d[k] = _to_dict(v, exclude_none)
        elif isinstance(v, list) and v and hasattr(v[0], "__dict__"):
            d[k] = [_to_dict(x, exclude_none) for x in v]
        else:
            d[k] = v
    return d


def store_repo(
    full_name: str,
    owner: str,
    name: str,
    default_branch: str = "main",
    webhook_secret: Optional[str] = None,
    enabled: bool = True,
) -> str:
    """Upsert repo document. Returns repo_id. Does not overwrite webhook_secret if already set."""
    db = _get_db()
    rid = repo_id(full_name)
    now = datetime.utcnow()
    ref = db.collection("repos").document(rid)
    doc = ref.get()
    data = {
        "full_name": full_name,
        "owner": owner,
        "name": name,
        "default_branch": default_branch,
        "updated_at": now,
    }
    if not doc.exists:
        data["created_at"] = now
    ref.set(data, merge=True)
    if webhook_secret is not None:
        ref.update({"webhook_secret": webhook_secret})
    return rid


def list_repos() -> list[dict]:
    """List all registered repos."""
    db = _get_db()
    docs = db.collection("repos").stream()
    return [{"id": d.id, **d.to_dict()} for d in docs]


def list_commits(repo_full_name: str, limit: int = 50) -> list[dict]:
    """List commits for a repo, newest first."""
    db = _get_db()
    query = (
        db.collection("commits")
        .where("repo_full_name", "==", repo_full_name)
        .order_by("created_at", direction="DESCENDING")
        .limit(limit)
    )
    return [{"id": d.id, **d.to_dict()} for d in query.stream()]


def get_all_repo_secrets() -> list[tuple[str, str]]:
    """Return (repo_id, webhook_secret) for repos that have a per-repo secret."""
    db = _get_db()
    docs = db.collection("repos").stream()
    return [(d.id, s) for d in docs for s in [d.to_dict().get("webhook_secret")] if s]


def get_repo(repo_full_name: str) -> Optional[dict]:
    """Get repo by full name."""
    db = _get_db()
    rid = repo_id(repo_full_name)
    doc = db.collection("repos").document(rid).get()
    if not doc.exists:
        return None
    d = doc.to_dict()
    return {"id": doc.id, "enabled": d.get("enabled", True), **d}


def register_repo(
    owner: str,
    name: str,
    webhook_secret: Optional[str] = None,
) -> dict:
    """Register a repo. Optionally set per-repo webhook secret. Returns repo info with secret."""
    import secrets
    full_name = f"{owner}/{name}"
    rid = repo_id(full_name)
    secret = webhook_secret or secrets.token_urlsafe(32)
    store_repo(full_name=full_name, owner=owner, name=name, webhook_secret=secret)
    return {
        "repo_id": rid,
        "full_name": full_name,
        "owner": owner,
        "name": name,
        "webhook_secret": secret,
        "webhook_url": "/webhook/github",
    }


def store_commit(commit: CommitDoc) -> str:
    """Store commit with diff. Returns commit document ID."""
    db = _get_db()
    cid = commit_id(commit.repo_full_name, commit.sha)
    data = _to_dict(commit)
    data["created_at"] = data.get("created_at") or datetime.utcnow()
    db.collection("commits").document(cid).set(data, merge=True)
    return cid


def store_webhook_event(event: WebhookEventDoc) -> str:
    """Store webhook event for audit. Returns event document ID."""
    db = _get_db()
    data = _to_dict(event)
    data["created_at"] = data.get("created_at") or datetime.utcnow()
    ref = db.collection("webhook_events").document(event.delivery_id)
    ref.set(data, merge=True)
    return event.delivery_id


def update_webhook_event(delivery_id: str, processed: bool, commits_stored: int, error: Optional[str] = None):
    """Update webhook event after processing."""
    db = _get_db()
    db.collection("webhook_events").document(delivery_id).update({
        "processed": processed,
        "commits_stored": commits_stored,
        "error": error,
    })
