"""Firebase Firestore service for storing webhook and commit data."""

import logging
import os

logger = logging.getLogger(__name__)
from datetime import datetime
from typing import Any, Optional

from firebase_schema import (
    CommitDoc,
    WebhookEventDoc,
    commit_id,
    repo_id,
    video_id,
)

logger = logging.getLogger(__name__)


def _resolve_cred_path(cred_path: str) -> str:
    """Resolve relative paths against the backend directory."""
    if os.path.isabs(cred_path):
        return cred_path
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(backend_dir, cred_path)


def _get_db():
    """Lazy-init Firestore. Requires GOOGLE_APPLICATION_CREDENTIALS or FIREBASE_SERVICE_ACCOUNT_PATH."""
    import firebase_admin
    from firebase_admin import credentials, firestore

    try:
        firebase_admin.get_app()
    except ValueError:
        # No app exists yet, initialize
        cred_path = os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH") or os.environ.get(
            "GOOGLE_APPLICATION_CREDENTIALS"
        )
        init_options: dict[str, str] = {}
        storage_bucket = os.environ.get("FIREBASE_STORAGE_BUCKET")
        if storage_bucket:
            init_options["storageBucket"] = storage_bucket
        opts = init_options or None
        if cred_path:
            resolved = _resolve_cred_path(cred_path)
            if os.path.exists(resolved):
                cred = credentials.Certificate(resolved)
                firebase_admin.initialize_app(cred, options=opts)
            else:
                raise FileNotFoundError(
                    f"Firebase credentials not found at {resolved} (from {cred_path}). "
                    "Set FIREBASE_SERVICE_ACCOUNT_PATH to the path of your service account JSON."
                )
        else:
            # For local dev: use default credentials (gcloud auth application-default login)
            firebase_admin.initialize_app(options=opts)
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
    website_url: Optional[str] = None,
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
    if website_url is not None:
        data["website_url"] = website_url
    ref.set(data, merge=True)
    if webhook_secret is not None:
        ref.update({"webhook_secret": webhook_secret})
    return rid


def update_repo_website_url(repo_full_name: str, website_url: str | None) -> None:
    """Update website_url for a repo."""
    db = _get_db()
    rid = repo_id(repo_full_name)
    ref = db.collection("repos").document(rid)
    ref.update({"website_url": website_url, "updated_at": datetime.utcnow()})


def list_repos() -> list[dict]:
    """List all registered repos."""
    db = _get_db()
    docs = db.collection("repos").stream()
    return [{"id": d.id, **d.to_dict()} for d in docs]


def _timestamp_sort_key(doc_dict: dict):
    """Extract sortable timestamp from commit doc (for fallback sort)."""
    ts = doc_dict.get("created_at") or doc_dict.get("timestamp")
    if ts is None:
        return datetime.min
    # Firestore Timestamp has .timestamp() or .seconds
    if hasattr(ts, "timestamp"):
        return datetime.fromtimestamp(ts.timestamp())
    if hasattr(ts, "seconds"):
        return datetime.fromtimestamp(getattr(ts, "seconds", 0))
    return ts


def list_commits(repo_full_name: str, limit: int = 50) -> list[dict]:
    """List commits for a repo, newest first. Tries both owner/repo and owner_repo formats."""
    db = _get_db()

    from google.cloud.firestore_v1.base_query import FieldFilter

    def _query(repofield: str):
        eq_filter = FieldFilter("repo_full_name", "==", repofield)
        try:
            query = (
                db.collection("commits")
                .where(filter=eq_filter)
                .order_by("created_at", direction="DESCENDING")
                .limit(limit)
            )
            return [{"id": d.id, **d.to_dict()} for d in query.stream()]
        except Exception:
            query = (
                db.collection("commits")
                .where(filter=eq_filter)
                .limit(limit * 2)
            )
            docs = [{"id": d.id, **d.to_dict()} for d in query.stream()]
            docs.sort(key=_timestamp_sort_key, reverse=True)
            return docs[:limit]

    # Try owner/repo first (canonical format)
    result = _query(repo_full_name)
    if result:
        return result
    # Fallback: try owner_repo (in case stored with underscore)
    alt = repo_full_name.replace("/", "_")
    if alt != repo_full_name:
        return _query(alt)
    return result


def get_commit_by_id(commit_doc_id: str) -> Optional[dict]:
    """Fetch a single commit document by commit document ID."""
    db = _get_db()
    doc = db.collection("commits").document(commit_doc_id).get()
    if not doc.exists:
        logger.info("Commit not found commit_id=%s", commit_doc_id)
        return None
    return {"id": doc.id, **doc.to_dict()}


def get_all_repo_secrets() -> list[tuple[str, str]]:
    """Return (repo_id, webhook_secret) for repos that have a per-repo secret."""
    db = _get_db()
    docs = db.collection("repos").stream()
    return [(d.id, s) for d in docs for s in [d.to_dict().get("webhook_secret")] if s]


def get_commit(repo_full_name: str, sha: str) -> Optional[dict]:
    """Get existing commit by repo and sha."""
    db = _get_db()
    cid = commit_id(repo_full_name, sha)
    doc = db.collection("commits").document(cid).get()
    if not doc.exists:
        return None
    return {"id": doc.id, **doc.to_dict()}


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
    website_url: Optional[str] = None,
) -> dict:
    """Register a repo. Optionally set per-repo webhook secret and website_url. Returns repo info with secret."""
    import secrets
    full_name = f"{owner}/{name}"
    rid = repo_id(full_name)
    secret = webhook_secret or secrets.token_urlsafe(32)
    store_repo(
        full_name=full_name,
        owner=owner,
        name=name,
        webhook_secret=secret,
        website_url=website_url,
    )
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


def update_commit_goal(commit_doc_id: str, goal: str) -> None:
    """Update commit document with feature demo goal. Persists to Firestore."""
    db = _get_db()
    db.collection("commits").document(commit_doc_id).set(
        {"feature_demo_goal": goal}, merge=True
    )
    logger.info("Updated commit goal commit_id=%s", commit_doc_id)


def update_commit_feature_demo(
    commit_doc_id: str,
    status: str,
    video_url: Optional[str] = None,
    error: Optional[str] = None,
    goal: Optional[str] = None,
) -> None:
    """Update commit document with feature demo pipeline result."""
    db = _get_db()
    ref = db.collection("commits").document(commit_doc_id)
    payload: dict[str, Any] = {
        "feature_demo_status": status,
        "feature_demo_video_url": video_url,
        "feature_demo_error": error,
    }
    if goal is not None:
        payload["feature_demo_goal"] = goal
    ref.set(payload, merge=True)


def upsert_video_doc(video_doc_id: str, payload: dict[str, Any]) -> str:
    """Create or update a pipeline video document."""
    db = _get_db()
    ref = db.collection("videos").document(video_doc_id)
    now = datetime.utcnow()
    existing = ref.get()
    payload = {**payload}
    if not existing.exists:
        payload.setdefault("created_at", now)
    payload["updated_at"] = now
    ref.set(payload, merge=True)
    logger.info("Upserted video doc video_id=%s status=%s stage=%s", video_doc_id, payload.get("status"), payload.get("stage"))
    return video_doc_id


def update_video_status(
    video_doc_id: str,
    status: str,
    stage: str,
    error: Optional[str] = None,
    extra_fields: Optional[dict[str, Any]] = None,
) -> str:
    """Update status/stage of a pipeline video document."""
    payload: dict[str, Any] = {
        "status": status,
        "stage": stage,
        "error": error,
        "updated_at": datetime.utcnow(),
    }
    if status in {"completed", "failed"}:
        payload["completed_at"] = datetime.utcnow()
    if extra_fields:
        payload.update(extra_fields)
    return upsert_video_doc(video_doc_id, payload)


def get_video(video_doc_id: str) -> Optional[dict]:
    """Fetch a generated video pipeline document by ID."""
    db = _get_db()
    doc = db.collection("videos").document(video_doc_id).get()
    if not doc.exists:
        logger.info("Video doc not found video_id=%s", video_doc_id)
        return None
    return {"id": doc.id, **doc.to_dict()}


def list_videos(
    repo_full_name: str,
    limit: int = 20,
    status_filter: Optional[str] = None,
) -> list[dict]:
    """List generated video documents for a repo, newest first."""
    db = _get_db()
    query = db.collection("videos").where("repo_full_name", "==", repo_full_name)
    if status_filter:
        query = query.where("status", "==", status_filter)
    query = query.order_by("created_at", direction="DESCENDING").limit(limit)
    return [{"id": d.id, **d.to_dict()} for d in query.stream()]


def build_video_doc_id(repo_full_name: str, sha: str) -> str:
    """Generate video document ID from repo and commit SHA."""
    return video_id(repo_full_name, sha)


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
