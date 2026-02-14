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
  - website_url: str | null  # URL for feature demo recording (e.g. https://app.example.com)
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
  - feature_demo_status: str | null  # queued, running, completed, failed
  - feature_demo_goal: str | null  # Goal text used for recording
  - feature_demo_video_url: str | null  # Blob storage URL of recorded demo
  - feature_demo_error: str | null
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

videos/{videoId}
  videoId: "{repoId}_{sha}" (same shape as commitId)
  - video_id: str
  - commit_id: str
  - repo_full_name: str
  - sha: str
  - sha_short: str
  - status: str  # queued, running, completed, failed
  - stage: str  # script, awaiting_source_video, normalize_video, veo_generate, stitch, voiceover, captions, upload, done, error
  - error: str | null
  - languages_requested: list[str]
  - source_video: map | null
  - base_video_url: str | null
  - enhanced_video_url: str | null
  - enhancement_plan: map | null
  - fallback_used: bool
  - script: { title, feature_summary, scenes[], total_duration_sec } | null
  - tracks: {
      "<lang>": {
        audio_url, captions_url, voice_script, duration_sec, voice_provider, caption_mode, mix_meta, status, error
      }
    }
  - created_at: timestamp
  - updated_at: timestamp
  - completed_at: timestamp | null
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
    website_url: Optional[str] = None
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


@dataclass
class VideoDoc:
    video_id: str
    commit_id: str
    repo_full_name: str
    sha: str
    sha_short: str
    status: str
    stage: str
    error: Optional[str] = None
    languages_requested: list[str] = field(default_factory=list)
    source_video: Optional[dict] = None
    base_video_url: Optional[str] = None
    enhanced_video_url: Optional[str] = None
    enhancement_plan: Optional[dict] = None
    fallback_used: bool = False
    script: Optional[dict] = None
    tracks: dict = field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


def commit_id(repo_full_name: str, sha: str) -> str:
    """Generate unique commit document ID."""
    repo_id = repo_full_name.replace("/", "_")
    return f"{repo_id}_{sha[:7]}"


def repo_id(full_name: str) -> str:
    """Generate repo document ID."""
    return full_name.replace("/", "_")


def video_id(repo_full_name: str, sha: str) -> str:
    """Generate video document ID from repo and commit SHA."""
    return commit_id(repo_full_name, sha)
