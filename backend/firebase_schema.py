"""
Firebase Firestore schema for Diffcast.

Collections:
-----------

repos/{repoId}
  repoId: "owner_repo" (e.g. "octocat_hello-world")
  - full_name: str
  - owner: str
  - name: str
  - default_branch: str
  - webhook_secret: str | null  # Per-repo secret (optional; use shared GITHUB_WEBHOOK_SECRET if not set)
  - enabled: bool  # Process webhooks from this repo (default True)
  - created_at: timestamp
  - updated_at: timestamp

commits/{commitId}
  commitId: "{repoId}_{sha}" (e.g. "octocat_hello-world_abc1234")
  - sha: str (full 40-char)
  - sha_short: str (first 7)
  - repo_id: str
  - repo_full_name: str
  - message: str
  - author: { name, email, avatar_url }
  - timestamp: datetime
  - branch: str
  - pr_number: int | null
  - pr_url: str | null
  - pr_title: str | null
  - files: [{ path, status, additions, deletions, patch }]
  - diff_summary: str | null  # Human-readable (Gemini later)
  - created_at: timestamp

webhook_events/{eventId}
  eventId: GitHub delivery ID (X-GitHub-Delivery header)
  - type: str (pull_request, push, etc.)
  - action: str (opened, closed, etc.)
  - repo_full_name: str
  - delivery_id: str
  - processed: bool
  - commits_stored: int
  - error: str | null
  - created_at: timestamp
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class RepoDoc:
    full_name: str
    owner: str
    name: str
    default_branch: str = "main"
    webhook_secret: Optional[str] = None
    enabled: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class FileChange:
    path: str
    status: str  # added, removed, modified
    additions: int
    deletions: int
    patch: Optional[str] = None


@dataclass
class CommitDoc:
    sha: str
    sha_short: str
    repo_id: str
    repo_full_name: str
    message: str
    author: dict
    timestamp: datetime
    branch: str
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
    pr_title: Optional[str] = None
    files: list = field(default_factory=list)
    diff_summary: Optional[str] = None
    created_at: Optional[datetime] = None


@dataclass
class WebhookEventDoc:
    type: str
    action: str
    repo_full_name: str
    delivery_id: str
    processed: bool = False
    commits_stored: int = 0
    error: Optional[str] = None
    created_at: Optional[datetime] = None


def commit_id(repo_full_name: str, sha: str) -> str:
    """Generate unique commit document ID."""
    repo_id = repo_full_name.replace("/", "_")
    return f"{repo_id}_{sha[:7]}"


def repo_id(full_name: str) -> str:
    """Generate repo document ID."""
    return full_name.replace("/", "_")
