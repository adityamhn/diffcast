# Firebase Firestore Schema

## Overview

Diffcast stores GitHub webhook data and per-commit diffs in Firestore. Each commit gets its own document with full diff data for later Gemini analysis (human-readable summaries).

## Collections

### `repos/{repoId}`

Repository metadata. `repoId` = `owner_repo` (e.g. `octocat_hello-world`).

| Field | Type | Description |
|-------|------|-------------|
| full_name | string | e.g. `octocat/hello-world` |
| owner | string | Repository owner |
| name | string | Repository name |
| default_branch | string | Default branch (e.g. `main`) |
| webhook_secret | string \| null | Per-repo webhook secret (optional) |
| enabled | bool | Process webhooks from this repo (default true) |
| created_at | timestamp | First seen |
| updated_at | timestamp | Last updated |

---

### `commits/{commitId}`

Per-commit diff and metadata. `commitId` = `{repoId}_{sha_short}` (e.g. `octocat_hello-world_abc1234`).

| Field | Type | Description |
|-------|------|-------------|
| sha | string | Full 40-char commit SHA |
| sha_short | string | Short 7-char SHA |
| repo_id | string | Reference to repos collection |
| repo_full_name | string | e.g. `octocat/hello-world` |
| message | string | Commit message |
| author | map | `{ name, email, avatar_url }` |
| timestamp | datetime | Commit timestamp |
| branch | string | Branch name |
| pr_number | int \| null | PR number if from merged PR |
| pr_url | string \| null | PR URL |
| pr_title | string \| null | PR title |
| files | array | File changes (see below) |
| diff_summary | string \| null | Human-readable summary (Gemini, later) |
| created_at | timestamp | When stored |

**files** array items:

| Field | Type | Description |
|-------|------|-------------|
| path | string | File path |
| status | string | `added`, `removed`, `modified` |
| additions | int | Lines added |
| deletions | int | Lines removed |
| patch | string \| null | Unified diff hunk |

---

### `webhook_events/{eventId}`

Audit log of webhook deliveries. `eventId` = GitHub `X-GitHub-Delivery` header.

| Field | Type | Description |
|-------|------|-------------|
| type | string | `push`, `pull_request`, etc. |
| action | string | `opened`, `closed`, etc. |
| repo_full_name | string | Repository |
| delivery_id | string | Same as document ID |
| processed | bool | Successfully processed |
| commits_stored | int | Number of commits stored |
| error | string \| null | Error message if failed |
| created_at | timestamp | When received |

## Indexes (if needed)

For querying commits by repo:

- **Collection**: `commits`
- **Fields**: `repo_full_name` (Ascending), `created_at` (Descending)

For webhook audit:

- **Collection**: `webhook_events`
- **Fields**: `created_at` (Descending)

## Security Rules (Firestore)

```javascript
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    // Backend writes via Admin SDK (bypasses rules)
    // Frontend read-only access
    match /repos/{repoId} {
      allow read: if true;
      allow write: if false;
    }
    match /commits/{commitId} {
      allow read: if true;
      allow write: if false;
    }
    match /webhook_events/{eventId} {
      allow read: if true;
      allow write: if false;
    }
  }
}
```
