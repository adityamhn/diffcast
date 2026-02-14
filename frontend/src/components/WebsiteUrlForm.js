"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { updateRepoWebsiteUrl } from "@/lib/api";
import styles from "./WebsiteUrlForm.module.css";

export function WebsiteUrlForm({ owner, repo, initialUrl }) {
  const [url, setUrl] = useState(initialUrl ?? "");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [editing, setEditing] = useState(!initialUrl);
  const router = useRouter();

  const handleSave = async () => {
    setLoading(true);
    setError(null);
    try {
      await updateRepoWebsiteUrl(owner, repo, url.trim() || null);
      setEditing(false);
      router.refresh();
    } catch (e) {
      setError(e.message || "Failed to save");
    } finally {
      setLoading(false);
    }
  };

  if (editing) {
    return (
      <div className={styles.form}>
        <input
          type="url"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://app.example.com"
          className={styles.input}
          disabled={loading}
        />
        <button
          type="button"
          onClick={handleSave}
          disabled={loading}
          className={styles.saveBtn}
        >
          {loading ? "Saving…" : "Save"}
        </button>
        {!initialUrl && (
          <button
            type="button"
            onClick={() => setEditing(false)}
            disabled={loading}
            className={styles.cancelBtn}
          >
            Cancel
          </button>
        )}
        {error && <span className={styles.error}>{error}</span>}
      </div>
    );
  }

  return (
    <div className={styles.display}>
      {initialUrl ? (
        <>
          <a
            href={initialUrl}
            target="_blank"
            rel="noopener noreferrer"
            className={styles.link}
          >
            {initialUrl}
          </a>
          <button
            type="button"
            onClick={() => setEditing(true)}
            className={styles.editBtn}
            title="Edit website URL"
          >
            Edit
          </button>
        </>
      ) : (
        <button
          type="button"
          onClick={() => setEditing(true)}
          className={styles.addBtn}
        >
          + Set website URL
        </button>
      )}
    </div>
  );
}
