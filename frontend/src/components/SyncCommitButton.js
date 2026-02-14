"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { syncCommit } from "@/lib/api";
import styles from "./SyncCommitButton.module.css";

export function SyncCommitButton({ owner, repo, sha, branch }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const router = useRouter();

  const handleSync = async () => {
    setLoading(true);
    setError(null);
    try {
      await syncCommit(owner, repo, sha, branch);
      router.refresh();
    } catch (e) {
      setError(e.message || "Sync failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className={styles.wrapper}>
      <button
        type="button"
        onClick={handleSync}
        disabled={loading}
        className={styles.button}
        title="Re-sync commit from GitHub"
      >
        {loading ? "Syncing…" : "Sync"}
      </button>
      {error && <span className={styles.error}>{error}</span>}
    </div>
  );
}
