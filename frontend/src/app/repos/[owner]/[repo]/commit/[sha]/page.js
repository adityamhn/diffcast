import Link from "next/link";
import { getCommit, getVideo } from "@/lib/api";
import { CommitPageContent } from "./CommitPageContent";
import styles from "./page.module.css";

export const dynamic = "force-dynamic";

export async function generateMetadata({ params }) {
  const { owner, repo, sha } = await params;
  const shortSha = sha.length >= 7 ? sha.slice(0, 7) : sha;
  return { title: `${owner}/${repo} @ ${shortSha} | Diffcast` };
}

export default async function CommitPage({ params }) {
  const { owner, repo, sha } = await params;
  const repoPath = `${owner}/${repo}`;
  const shortSha = sha.length >= 7 ? sha.slice(0, 7) : sha;
  const commitId = `${owner}_${repo}_${shortSha}`;

  let commit = null;
  let video = null;
  let error = null;

  try {
    commit = await getCommit(owner, repo, sha);
    video = await getVideo(commitId);
  } catch (e) {
    error = e.message || "Commit not found";
  }

  return (
    <div className={styles.page}>
      <main className={styles.main}>
        <header className={styles.header}>
          <Link href={`/repos/${owner}/${repo}`} className={styles.back}>
            ← {repoPath}
          </Link>
          <h1 className={styles.title}>
            <a
              href={`https://github.com/${repoPath}/commit/${commit?.sha || sha}`}
              target="_blank"
              rel="noopener noreferrer"
              className={styles.shaLink}
            >
              {shortSha}
            </a>
          </h1>
          {commit?.message && (
            <p className={styles.message}>{commit.message}</p>
          )}
        </header>

        {error ? (
          <div className={styles.empty}>
            <p>{error}</p>
            <p className={styles.hint}>
              Sync the commit first from the repo page.
            </p>
          </div>
        ) : (
          <CommitPageContent
            owner={owner}
            repo={repo}
            sha={commit?.sha || sha}
            initialCommit={commit}
            initialVideo={video}
          />
        )}
      </main>
    </div>
  );
}
