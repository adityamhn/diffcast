import Link from "next/link";
import { getRepos } from "@/lib/api";
import styles from "./page.module.css";

export const dynamic = "force-dynamic";

export default async function Home() {
  let data;
  try {
    data = await getRepos();
  } catch (e) {
    return (
      <div className={styles.page}>
        <main className={styles.main}>
          <h1 className={styles.title}>Diffcast</h1>
          <div className={styles.error}>
            <p>Could not connect to the API.</p>
            <p className={styles.errorHint}>
              Ensure the backend is running and{" "}
              <code>NEXT_PUBLIC_API_URL</code> is set correctly.
            </p>
          </div>
        </main>
      </div>
    );
  }

  const repos = data?.repos ?? [];

  return (
    <div className={styles.page}>
      <main className={styles.main}>
        <header className={styles.header}>
          <h1 className={styles.title}>Diffcast</h1>
          <p className={styles.subtitle}>
            Repositories tracked via GitHub webhooks
          </p>
        </header>

        {repos.length === 0 ? (
          <div className={styles.empty}>
            <p>No repositories yet.</p>
            <p className={styles.emptyHint}>
              Add a repo via <code>POST /api/repos/add</code> or trigger a
              webhook.
            </p>
          </div>
        ) : (
          <div className={styles.grid}>
            {repos.map((repo) => (
              <Link
                key={repo.id}
                href={`/repos/${repo.full_name}`}
                className={styles.card}
              >
                <div className={styles.cardHeader}>
                  <span className={styles.repoName}>{repo.full_name}</span>
                  <span className={styles.branch}>{repo.default_branch}</span>
                </div>
                <div className={styles.cardMeta}>
                  <span>{repo.owner}</span>
                  <span>/{repo.name}</span>
                </div>
              </Link>
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
