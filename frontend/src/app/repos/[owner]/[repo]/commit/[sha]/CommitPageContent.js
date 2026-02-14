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
  testGoalPhase,
  testDemoPhase,
  testScriptPhase,
  testSnapshotsPhase,
  testVeoPhase,
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

  // Test phase states
  const [testingGoal, setTestingGoal] = useState(false);
  const [testingDemo, setTestingDemo] = useState(false);
  const [testingScript, setTestingScript] = useState(false);
  const [testingSnapshots, setTestingSnapshots] = useState(false);
  const [testingVeo, setTestingVeo] = useState(false);
  const [testResults, setTestResults] = useState(null);
  const [showTestPanel, setShowTestPanel] = useState(false);

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

  // Test phase handlers
  const handleTestGoal = async () => {
    setTestingGoal(true);
    setError(null);
    setTestResults(null);
    try {
      const res = await testGoalPhase(owner, repo, sha);
      setTestResults({ phase: "goal", ...res });
      if (res.goal) setGoal(res.goal);
    } catch (e) {
      setError(e.message);
    } finally {
      setTestingGoal(false);
    }
  };

  const handleTestDemo = async () => {
    setTestingDemo(true);
    setError(null);
    setTestResults(null);
    try {
      const res = await testDemoPhase(owner, repo, sha);
      setTestResults({ phase: "demo", ...res });
      await fetchData();
    } catch (e) {
      setError(e.message);
    } finally {
      setTestingDemo(false);
    }
  };

  const handleTestScript = async () => {
    setTestingScript(true);
    setError(null);
    setTestResults(null);
    try {
      const demoDuration = video?.demo_video_duration_sec || 10;
      const res = await testScriptPhase(owner, repo, sha, demoDuration);
      setTestResults({ phase: "script", ...res });
    } catch (e) {
      setError(e.message);
    } finally {
      setTestingScript(false);
    }
  };

  const handleTestSnapshots = async () => {
    const videoUrl = commit?.feature_demo_video_url || video?.demo_video_url;
    if (!videoUrl) {
      setError("No demo video URL available. Record a demo first.");
      return;
    }
    setTestingSnapshots(true);
    setError(null);
    setTestResults(null);
    try {
      const res = await testSnapshotsPhase(videoUrl);
      setTestResults({ phase: "snapshots", ...res });
    } catch (e) {
      setError(e.message);
    } finally {
      setTestingSnapshots(false);
    }
  };

  const handleTestVeo = async () => {
    // Use the first clip prompt from the shot plan if available
    // testResults from script phase has clip_prompts directly, video might have shot_plan nested
    const clipPrompts = testResults?.clip_prompts || video?.shot_plan?.clip_prompts;
    const clipPrompt = clipPrompts?.[0]?.prompt;

    // Fallback prompt for testing without running script phase first
    const fallbackPrompt = `Photorealistic product demo of a modern web application displayed on a desktop monitor. 
The screen shows a clean, professional UI with interactive elements. A realistic mouse cursor moves smoothly 
across the interface, clicking buttons and navigating through the application. The camera holds a steady 
front-on view of the monitor with soft neutral studio lighting. Style: photorealistic cinematic product demo, 
16:9, 4K, clean modern interface, responsive UI, cinematic clarity.`;

    const promptToUse = clipPrompt || fallbackPrompt;

    setTestingVeo(true);
    setError(null);
    setTestResults(null);
    try {
      const res = await testVeoPhase(promptToUse, null, 6);
      setTestResults({ phase: "veo", used_fallback_prompt: !clipPrompt, ...res });
    } catch (e) {
      setError(e.message);
    } finally {
      setTestingVeo(false);
    }
  };

  const demoStatus = commit?.feature_demo_status;
  const videoStatus = video?.status;
  const videoStage = video?.stage;
  const isDemoRunning = demoStatus === "running" || demoStatus === "queued";
  const isVideoRunning = videoStatus === "running" || videoStatus === "queued";

  const progressPercent = () => {
    if (isVideoRunning && videoStage) {
      const stages = ["goal", "demo", "script", "snapshots", "veo", "stitch", "voice", "finalize", "done"];
      const idx = stages.indexOf(videoStage);
      if (idx >= 0) return Math.round(((idx + 1) / stages.length) * 100);
    }
    if (isDemoRunning) return 25;
    return 0;
  };

  const script = video?.script;
  const shotPlan = video?.shot_plan;
  const clipPrompts = shotPlan?.clip_prompts ?? [];
  const primaryTrack = video?.tracks?.en || (video?.tracks && Object.values(video.tracks)[0]);
  const featureReleaseUrl = primaryTrack?.final_video_url || video?.base_video_url;

  // Log Veo prompts to console when video data is available
  useEffect(() => {
    if (clipPrompts?.length > 0) {
      clipPrompts.forEach((cp, i) => {
        console.log(`[Veo] Clip ${i + 1} (${cp.role || "unknown"}):`, cp.prompt);
      });
    }
  }, [clipPrompts]);

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
          onClick={handleGenerateVideo}
          disabled={generatingVideo}
          className={styles.btnFullFlow}
          title="Runs the complete pipeline: goal → demo → script → snapshots → Veo → stitch → voice → captions → final video"
        >
          {generatingVideo ? "Starting full flow…" : "Generate full flow"}
        </button>
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
          onClick={() => setShowTestPanel(!showTestPanel)}
          className={styles.btnSecondary}
        >
          {showTestPanel ? "Hide Test Panel" : "Show Test Panel"}
        </button>
      </div>

      {showTestPanel && (
        <section className={styles.testPanel}>
          <h2 className={styles.sectionTitle}>Pipeline Test Panel</h2>
          <p className={styles.testDescription}>
            Test each phase of the pipeline individually. Phases run in order:
            Goal → Demo → Script → Snapshots → Veo → Stitch → Voice → Captions → Finalize
          </p>

          <div className={styles.testActions}>
            <div className={styles.testPhase}>
              <span className={styles.phaseNumber}>1</span>
              <button
                type="button"
                onClick={handleTestGoal}
                disabled={testingGoal}
                className={styles.btnTest}
              >
                {testingGoal ? "Testing…" : "Test Goal"}
              </button>
              <span className={styles.phaseDesc}>Generate browser-use goal from diff</span>
            </div>

            <div className={styles.testPhase}>
              <span className={styles.phaseNumber}>2</span>
              <button
                type="button"
                onClick={handleTestDemo}
                disabled={testingDemo}
                className={styles.btnTest}
              >
                {testingDemo ? "Testing…" : "Test Demo"}
              </button>
              <span className={styles.phaseDesc}>Record demo video with browser-use</span>
            </div>

            <div className={styles.testPhase}>
              <span className={styles.phaseNumber}>3</span>
              <button
                type="button"
                onClick={handleTestScript}
                disabled={testingScript}
                className={styles.btnTest}
              >
                {testingScript ? "Testing…" : "Test Script"}
              </button>
              <span className={styles.phaseDesc}>Generate scene script + 2 clip prompts</span>
            </div>

            <div className={styles.testPhase}>
              <span className={styles.phaseNumber}>4</span>
              <button
                type="button"
                onClick={handleTestSnapshots}
                disabled={testingSnapshots || !commit?.feature_demo_video_url}
                className={styles.btnTest}
              >
                {testingSnapshots ? "Testing…" : "Test Snapshots"}
              </button>
              <span className={styles.phaseDesc}>Extract 2 frames from demo video</span>
            </div>

            <div className={styles.testPhase}>
              <span className={styles.phaseNumber}>5</span>
              <button
                type="button"
                onClick={handleTestVeo}
                disabled={testingVeo}
                className={styles.btnTest}
              >
                {testingVeo ? "Testing…" : "Test Veo"}
              </button>
              <span className={styles.phaseDesc}>Generate Veo clip (uses script prompt or fallback)</span>
            </div>

            <div className={styles.testPhase}>
              <span className={styles.phaseNumber}>6-10</span>
              <span className={styles.phaseDisabled}>Stitch, Voice, Captions, Finalize</span>
              <span className={styles.phaseDesc}>Run full pipeline for remaining phases</span>
            </div>
          </div>

          {testResults && (
            <div className={styles.testResults}>
              <h3>Test Results: {testResults.phase}</h3>
              <pre className={styles.testResultsCode}>
                {JSON.stringify(testResults, null, 2)}
              </pre>
            </div>
          )}
        </section>
      )}

      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>How to use</h2>
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

      {(shotPlan || video?.goal) && (
        <section className={styles.section}>
          <h2 className={styles.sectionTitle}>Pipeline data</h2>
          <div className={styles.pipelineData}>
            {video?.goal && (
              <div className={styles.pipelineBlock}>
                <h3 className={styles.pipelineBlockTitle}>Goal</h3>
                <p className={styles.pipelineText}>{video.goal}</p>
              </div>
            )}
            {clipPrompts.length > 0 && (
              <div className={styles.pipelineBlock}>
                <h3 className={styles.pipelineBlockTitle}>Veo prompts sent</h3>
                {clipPrompts.map((cp, i) => (
                  <div key={i} className={styles.veoPromptBlock}>
                    <span className={styles.veoPromptRole}>
                      Clip {i + 1}: {cp.role || "unknown"}
                    </span>
                    <pre className={styles.veoPromptText}>{cp.prompt}</pre>
                  </div>
                ))}
              </div>
            )}
            {shotPlan?.timeline?.length > 0 && (
              <div className={styles.pipelineBlock}>
                <h3 className={styles.pipelineBlockTitle}>Timeline</h3>
                <ul className={styles.timelineList}>
                  {shotPlan.timeline.map((seg, i) => (
                    <li key={i} className={styles.timelineItem}>
                      <span className={styles.timelineKind}>{seg.kind}</span>
                      <span className={styles.timelineDuration}>{seg.duration_sec}s</span>
                      {seg.narration && (
                        <span className={styles.timelineNarration}>{seg.narration}</span>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}
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
