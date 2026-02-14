"""Firebase Storage helpers for pipeline artifacts."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
APPSPOT_SUFFIX = ".appspot.com"
FIREBASESTORAGE_SUFFIX = ".firebasestorage.app"


def _get_bucket():
    """Return Firebase Storage bucket for uploads."""
    import firebase_admin
    from firebase_admin import storage

    if not firebase_admin._apps:
        # Import to trigger shared Firebase initialization path.
        from services.firebase_service import _get_db  # pylint: disable=import-outside-toplevel

        _get_db()

    bucket_name = os.environ.get("FIREBASE_STORAGE_BUCKET")
    return storage.bucket(bucket_name if bucket_name else None)


def _alternate_bucket_name(bucket_name: str | None) -> str | None:
    """Return common Firebase bucket-name variant, if available."""
    if not bucket_name:
        return None
    if bucket_name.endswith(APPSPOT_SUFFIX):
        return bucket_name.replace(APPSPOT_SUFFIX, FIREBASESTORAGE_SUFFIX)
    if bucket_name.endswith(FIREBASESTORAGE_SUFFIX):
        return bucket_name.replace(FIREBASESTORAGE_SUFFIX, APPSPOT_SUFFIX)
    return None


def upload_file(
    local_path: str | Path,
    destination_path: str,
    content_type: Optional[str] = None,
    make_public: bool = True,
) -> dict[str, str]:
    """Upload a local file to Firebase Storage and return URL metadata."""
    src = Path(local_path)
    if not src.exists():
        raise FileNotFoundError(f"File not found for upload: {src}")
    logger.info(
        "Uploading file src=%s destination=%s content_type=%s make_public=%s",
        src,
        destination_path,
        content_type,
        make_public,
    )

    bucket = _get_bucket()
    blob = bucket.blob(destination_path)
    try:
        blob.upload_from_filename(str(src), content_type=content_type)
    except Exception as exc:
        is_not_found = False
        try:
            from google.api_core.exceptions import NotFound

            is_not_found = isinstance(exc, NotFound)
        except Exception:
            is_not_found = "not found" in str(exc).lower() or "404" in str(exc)

        fallback_bucket_name = _alternate_bucket_name(bucket.name)
        if not is_not_found or not fallback_bucket_name:
            raise

        logger.warning(
            "Upload failed for bucket=%s; retrying with bucket=%s",
            bucket.name,
            fallback_bucket_name,
        )
        from firebase_admin import storage

        bucket = storage.bucket(fallback_bucket_name)
        blob = bucket.blob(destination_path)
        blob.upload_from_filename(str(src), content_type=content_type)

    public_url = ""
    if make_public:
        try:
            blob.make_public()
            public_url = blob.public_url
        except Exception:
            logger.exception("Failed to make blob public destination=%s", destination_path)
            public_url = ""

    gs_url = f"gs://{bucket.name}/{destination_path}"
    result = {
        "bucket": bucket.name,
        "path": destination_path,
        "gs_url": gs_url,
        "url": public_url or gs_url,
    }
    logger.info("Upload complete destination=%s url=%s", destination_path, result["url"])
    return result
