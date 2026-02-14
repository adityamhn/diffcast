const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";

export async function getRepos() {
  const res = await fetch(`${API_URL}/api/repos`);
  if (!res.ok) throw new Error("Failed to fetch repos");
  return res.json();
}

export async function getRepoCommits(owner, repo, limit = 50) {
  const res = await fetch(
    `${API_URL}/api/repos/${owner}/${repo}/commits?limit=${limit}`
  );
  if (!res.ok) throw new Error("Failed to fetch commits");
  return res.json();
}

export async function getRepo(owner, repo) {
  const res = await fetch(`${API_URL}/api/repos/${owner}/${repo}`);
  if (!res.ok) throw new Error("Failed to fetch repo");
  return res.json();
}

export async function syncCommit(owner, repo, sha, branch) {
  const res = await fetch(`${API_URL}/api/sync/commit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ owner, repo, sha, branch }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `Sync failed: ${res.status}`);
  }
  return res.json();
}

export async function updateRepoWebsiteUrl(owner, repo, websiteUrl) {
  const res = await fetch(`${API_URL}/api/repos/${owner}/${repo}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ website_url: websiteUrl || null }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `Update failed: ${res.status}`);
  }
  return res.json();
}

export async function triggerFeatureDemo(owner, repo, sha, force = false) {
  const res = await fetch(`${API_URL}/api/pipeline/feature-demo`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ owner, repo, sha, force }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `Feature demo failed: ${res.status}`);
  }
  return res.json();
}

export async function getCommit(owner, repo, sha) {
  const res = await fetch(`${API_URL}/api/repos/${owner}/${repo}/commits/${sha}`);
  if (!res.ok) throw new Error("Failed to fetch commit");
  return res.json();
}

export async function getVideo(videoId) {
  const res = await fetch(`${API_URL}/api/videos/${videoId}`);
  if (!res.ok) return null;
  return res.json();
}

export async function triggerCommitPipeline(owner, repo, sha, languages = ["en"], force = false) {
  const res = await fetch(`${API_URL}/api/pipeline/commit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ owner, repo, sha, languages, force }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `Pipeline failed: ${res.status}`);
  }
  return res.json();
}

export async function getBrowserUseGoal(owner, repo, sha) {
  const res = await fetch(`${API_URL}/api/pipeline/browser-use-goal`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ owner, repo, sha }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `Failed to get goal: ${res.status}`);
  }
  return res.json();
}

export async function chatCommit(owner, repo, sha, messages) {
  const res = await fetch(
    `${API_URL}/api/repos/${owner}/${repo}/commits/${sha}/chat`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages }),
    }
  );
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `Chat failed: ${res.status}`);
  }
  return res.json();
}
