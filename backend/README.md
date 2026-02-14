# Diffcast Backend

Flask API for the Diffcast pipeline: git diff analysis, video generation, and GitHub webhooks.

## Setup

### 1. Environment

Copy the example env and fill in your values:

```bash
cp .env.example .env
```

Edit `.env` with your API keys and config. Key variables:

- `GEMINI_API_KEY` — Required for diff analysis and TTS
- `FIREBASE_SERVICE_ACCOUNT_PATH` — Path to Firebase service account JSON
- `FIREBASE_STORAGE_BUCKET` — Your Firebase Storage bucket
- `GITHUB_TOKEN` — For fetching diffs (create at [github.com/settings/tokens](https://github.com/settings/tokens))

### 2. Install Dependencies

```bash
uv sync
```

### 3. Run the Server

```bash
uv run flask run --host 0.0.0.0 --port 8080
```

Or:

```bash
uv run python app.py
```

The API will be available at `http://localhost:8080`.
