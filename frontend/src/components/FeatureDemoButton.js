"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { triggerFeatureDemo } from "@/lib/api";
import styles from "./FeatureDemoButton.module.css";

export function FeatureDemoButton({
  owner,
  repo,
  sha,
  status,
  videoUrl,
  error,
  hasWebsiteUrl,
}) {
  const [loading, setLoading] = useState(false);
  const [triggerError, setTriggerError] = useState(null);
  const router = useRouter();

  const isRunning = status === "running" || status === "queued";
  const isCompleted = status === "completed";
  const isFailed = status === "failed";

  const handleTrigger = async () => {
    if (!hasWebsiteUrl) return;
    setLoading(true);
    setTriggerError(null);
    try {
      await triggerFeatureDemo(owner, repo, sha, false);
      router.refresh();
    } catch (e) {
      setTriggerError(e.message || "Failed to start");
    } finally {
      setLoading(false);
    }
  };

  if (isCompleted && videoUrl) {
    return (
      <div className={styles.wrapper}>
        <a
          href={videoUrl}
          target="_blank"
          rel="noopener noreferrer"
          className={styles.videoLink}
          title="View feature demo"
        >
          ▶ Demo
        </a>
      </div>
    );
  }

  if (isRunning) {
    return (
      <span className={styles.status}>
        {status === "queued" ? "Queued…" : "Recording…"}
      </span>
    );
  }

  const disabled = !hasWebsiteUrl || loading;
  const title = !hasWebsiteUrl
    ? "Set website URL for this repo first"
    : "Record feature demo";

  return (
    <div className={styles.wrapper}>
      <button
        type="button"
        onClick={handleTrigger}
        disabled={disabled}
        className={styles.button}
        title={title}
      >
        {loading ? "Starting…" : "Record demo"}
      </button>
      {(triggerError || (isFailed && error)) && (
        <span className={styles.error} title={error}>
          {triggerError || error}
        </span>
      )}
    </div>
  );
}
