import Link from "next/link";
import { getRepos, getRepoCommits } from "@/lib/api";
import { SyncCommitButton } from "@/components/SyncCommitButton";
import styles from "./page.module.css";

export const dynamic = "force-dynamic";

export async function generateMetadata({ params }) {
  const { owner, repo } = await params;
  const repoPath = `${owner}/${repo}`;
  return { title: `${repoPath} | Diffcast` };
}

export default async function RepoPage({ params }) {
  const { owner, repo } = await params;
  const repoPath = `${owner}/${repo}`;

  let commitsData;
  let repoInfo;
  let apiError = null;
  try {
    [commitsData, repoInfo] = await Promise.all([
      getRepoCommits(owner, repo),
      getRepos().then((d) => d.repos.find((r) => r.full_name === repoPath)),
    ]);
  } catch (e) {
    apiError = e.message || String(e);
  }

  const commits = commitsData?.commits ?? [];

  if (apiError) {
    return (
      <div className={styles.page}>
        <main className={styles.main}>
          <header className={styles.header}>
            <Link href="/" className={styles.back}>
              ← Repositories
            </Link>
            <h1 className={styles.title}>{repoPath}</h1>
          </header>
          <div className={styles.empty}>
            <p>Could not load commits.</p>
            <p className={styles.errorDetail}>{apiError}</p>
            <p className={styles.errorHint}>
              Ensure the backend is running and{" "}
              <code>NEXT_PUBLIC_API_URL</code> is correct.
            </p>
          </div>
        </main>
      </div>
    );
  }

  return (
    <div className={styles.page}>
      <main className={styles.main}>
        <header className={styles.header}>
          <Link href="/" className={styles.back}>
            ← Repositories
          </Link>
          <h1 className={styles.title}>{repoPath}</h1>
          {repoInfo?.default_branch && (
            <span className={styles.branch}>{repoInfo.default_branch}</span>
          )}
        </header>
        {console.log(commits)}
        {commits.length === 0 ? (
          <div className={styles.empty}>
            <p>No commits yet for this repository.</p>
          </div>
        ) : (
          <div className={styles.commitList}>
            {commits.map((commit) => (
              <article key={commit.id} className={styles.commitCard}>
                <div className={styles.commitHeader}>
                  <a
                    href={`https://github.com/${repoPath}/commit/${commit.sha}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className={styles.sha}
                  >
                    {commit.sha_short}
                  </a>
                  <span className={styles.branchTag}>{commit.branch}</span>
                  <SyncCommitButton
                    owner={owner}
                    repo={repo}
                    sha={commit.sha}
                    branch={commit.branch}
                  />
                  {commit.pr_url && (
                    <a
                      href={commit.pr_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className={styles.prLink}
                    >
                      PR #{commit.pr_number}
                    </a>
                  )}
                </div>
                <p className={styles.message}>{commit.message}</p>
                <div className={styles.meta}>
                  {commit.author?.avatar_url && (
                    <img
                      src={commit.author.avatar_url}
                      alt=""
                      width={20}
                      height={20}
                      className={styles.avatar}
                    />
                  )}
                  <span>{commit.author?.name ?? "Unknown"}</span>
                  {commit.timestamp && (
                    <span className={styles.time}>
                      {formatDate(commit.timestamp)}
                    </span>
                  )}
                </div>
                {commit.files?.length > 0 && (
                  <div className={styles.files}>
                    <span className={styles.filesLabel}>Files changed:</span>
                    <ul>
                      {commit.files.map((f, i) => (
                        <li key={i} className={styles.fileItem}>
                          <span
                            className={styles[`status_${f.status}`] || styles.status_modified}
                          >
                            {f.status}
                          </span>
                          <code>{f.path}</code>
                          <span className={styles.diffStats}>
                            +{f.additions} -{f.deletions}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                {commit.diff_summary && (
                  <div className={styles.summary}>
                    <span className={styles.summaryLabel}>Summary</span>
                    <p>{commit.diff_summary}</p>
                  </div>
                )}
                <div className={styles.placeholders}>
                  {!commit.diff_summary && (
                    <div className={styles.placeholder}>
                      <span className={styles.placeholderLabel}>
                        Transcript
                      </span>
                      <p className={styles.placeholderText}>Coming soon</p>
                    </div>
                  )}
                  <div className={styles.placeholder}>
                    <span className={styles.placeholderLabel}>Video</span>
                    <p className={styles.placeholderText}>Coming soon</p>
                  </div>
                </div>
              </article>
            ))}
          </div>
        )}
      </main>
    </div>
  );
}

function formatDate(ts) {
  if (typeof ts === "string") {
    const d = new Date(ts);
    return d.toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }
  if (ts && typeof ts === "object" && "seconds" in ts) {
    return new Date(ts.seconds * 1000).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }
  return "";
}
