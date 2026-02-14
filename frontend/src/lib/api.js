const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:5000";

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
