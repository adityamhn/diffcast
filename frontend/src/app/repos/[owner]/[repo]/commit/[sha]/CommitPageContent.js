"use client";

import { useState, useEffect, useCallback } from "react";
import { useRouter } from "next/navigation";
import {
  getCommit,
  getVideo,
  syncCommit,
  triggerFeatureDemo,
  triggerCommitPipeline,
  getBrowserUseGoal,
} from "@/lib/api";
import { CommitChat } from "./CommitChat";
import styles from "./CommitPageContent.module.css";

const POLL_INTERVAL_MS = 2000;

export function CommitPageContent({
  owner,
  repo,
  sha,
  initialCommit,
  initialVideo,
}) {
  const router = useRouter();
  const commitId = `${owner}_${repo}_${sha.slice(0, 7)}`;

  const [commit, setCommit] = useState(initialCommit);
  const [video, setVideo] = useState(initialVideo);
  const [goal, setGoal] = useState(initialCommit?.feature_demo_goal ?? null);
  const [loadingGoal, setLoadingGoal] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [recordingDemo, setRecordingDemo] = useState(false);
  const [generatingVideo, setGeneratingVideo] = useState(false);
  const [error, setError] = useState(null);

  const fetchData = useCallback(async () => {
    try {
      const [c, v] = await Promise.all([
        getCommit(owner, repo, sha),
        getVideo(commitId),
      ]);
      setCommit(c);
      setVideo(v);
      if (c?.feature_demo_goal) setGoal(c.feature_demo_goal);
    } catch (e) {
      setError(e.message);
    }
  }, [owner, repo, sha, commitId]);

  const needsPolling =
    commit?.feature_demo_status === "running" ||
    commit?.feature_demo_status === "queued" ||
    video?.status === "running" ||
    video?.status === "queued";

  useEffect(() => {
    if (!needsPolling) return;
    const id = setInterval(fetchData, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [needsPolling, fetchData]);

  const handleSync = async () => {
    setSyncing(true);
    setError(null);
    try {
      await syncCommit(owner, repo, sha, commit?.branch);
      await fetchData();
      router.refresh();
    } catch (e) {
      setError(e.message);
    } finally {
      setSyncing(false);
    }
  };

  const handleRecordDemo = async () => {
    setRecordingDemo(true);
    setError(null);
    try {
      await triggerFeatureDemo(owner, repo, sha, true);
      await fetchData();
      router.refresh();
    } catch (e) {
      setError(e.message);
    } finally {
      setRecordingDemo(false);
    }
  };

  const handleGenerateVideo = async () => {
    setGeneratingVideo(true);
    setError(null);
    try {
      await triggerCommitPipeline(owner, repo, sha, ["en"], true);
      await fetchData();
      router.refresh();
    } catch (e) {
      setError(e.message);
    } finally {
      setGeneratingVideo(false);
    }
  };

  const handleFetchGoal = async () => {
    setLoadingGoal(true);
    setError(null);
    try {
      const res = await getBrowserUseGoal(owner, repo, sha);
      setGoal(res.browser_use_goal);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoadingGoal(false);
    }
  };

  const demoStatus = commit?.feature_demo_status;
  const videoStatus = video?.status;
  const videoStage = video?.stage;
  const isDemoRunning = demoStatus === "running" || demoStatus === "queued";
  const isVideoRunning = videoStatus === "running" || videoStatus === "queued";

  const progressPercent = () => {
    if (isVideoRunning && videoStage) {
      const stages = ["script", "video", "voiceover", "captions", "upload", "done"];
      const idx = stages.indexOf(videoStage);
      if (idx >= 0) return Math.round(((idx + 1) / stages.length) * 100);
    }
    if (isDemoRunning) return 50;
    return 0;
  };

  const script = video?.script;
  const primaryTrack = video?.tracks?.en || (video?.tracks && Object.values(video.tracks)[0]);
  const featureReleaseUrl = primaryTrack?.final_video_url || video?.base_video_url;

  return (
    <div className={styles.layout}>
      <div className={styles.mainColumn}>
        <div className={styles.content}>
      {error && (
        <div className={styles.errorBanner}>
          {error}
          <button type="button" onClick={() => setError(null)} className={styles.dismiss}>
            ×
          </button>
        </div>
      )}

      {(isDemoRunning || isVideoRunning) && (
        <div className={styles.progressSection}>
          <div className={styles.progressBar}>
            <div
              className={styles.progressFill}
              style={{ width: `${progressPercent()}%` }}
            />
          </div>
          <span className={styles.progressLabel}>
            {isDemoRunning && "Recording demo…"}
            {isVideoRunning && !isDemoRunning && `${videoStage || videoStatus}…`}
          </span>
        </div>
      )}

      <div className={styles.actions}>
        <button
          type="button"
          onClick={handleSync}
          disabled={syncing}
          className={styles.btn}
        >
          {syncing ? "Syncing…" : "Sync"}
        </button>
        <button
          type="button"
          onClick={handleRecordDemo}
          disabled={recordingDemo}
          className={styles.btn}
        >
          {recordingDemo ? "Starting…" : "Re-record demo"}
        </button>
        <button
          type="button"
          onClick={handleGenerateVideo}
          disabled={generatingVideo}
          className={styles.btnPrimary}
        >
          {generatingVideo ? "Starting…" : "Generate feature video"}
        </button>
      </div>

      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>Demo goal</h2>
        {goal ? (
          <p className={styles.goal}>{goal}</p>
        ) : (
          <div className={styles.goalPlaceholder}>
            <p>No goal yet. Generate from commit diff.</p>
            <button
              type="button"
              onClick={handleFetchGoal}
              disabled={loadingGoal}
              className={styles.btn}
            >
              {loadingGoal ? "Generating…" : "Generate goal"}
            </button>
          </div>
        )}
      </section>

      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>Demo video</h2>
        {commit?.feature_demo_video_url ? (
          <div className={styles.videoWrapper}>
            <video
              src={commit.feature_demo_video_url}
              controls
              className={styles.video}
              aria-label="Feature demo recording"
            >
              <track kind="captions" />
            </video>
          </div>
        ) : (
          <div className={styles.placeholder}>
            {demoStatus === "failed" && commit?.feature_demo_error && (
              <p className={styles.errorText}>{commit.feature_demo_error}</p>
            )}
            <p>No demo video yet. Click &quot;Re-record demo&quot; to create one.</p>
          </div>
        )}
      </section>

      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>Feature release video</h2>
        {featureReleaseUrl ? (
          <div className={styles.videoWrapper}>
            <video
              src={featureReleaseUrl}
              controls
              className={styles.video}
              aria-label="Feature release video"
            >
              {primaryTrack?.captions_url ? (
                <track
                  kind="captions"
                  src={primaryTrack.captions_url}
                  srcLang="en"
                  label="English"
                />
              ) : (
                <track kind="captions" />
              )}
            </video>
          </div>
        ) : (
          <div className={styles.placeholder}>
            {videoStatus === "failed" && video?.error && (
              <p className={styles.errorText}>{video.error}</p>
            )}
            <p>No feature video yet. Click &quot;Generate feature video&quot; to create one.</p>
          </div>
        )}
      </section>

      {script && (
        <section className={styles.section}>
          <h2 className={styles.sectionTitle}>Transcript</h2>
          <div className={styles.transcript}>
            <h3 className={styles.scriptTitle}>{script.title}</h3>
            <p className={styles.scriptSummary}>{script.feature_summary}</p>
            <ol className={styles.sceneList}>
              {script.scenes?.map((scene, i) => (
                <li key={scene.on_screen_text || i} className={styles.sceneItem}>
                  <span className={styles.sceneText}>{scene.on_screen_text}</span>
                  <span className={styles.sceneNarration}>{scene.narration_seed}</span>
                </li>
              ))}
            </ol>
          </div>
        </section>
      )}
        </div>
      </div>
      <aside className={styles.sidebar}>
        <CommitChat owner={owner} repo={repo} sha={sha} />
      </aside>
    </div>
  );
}
