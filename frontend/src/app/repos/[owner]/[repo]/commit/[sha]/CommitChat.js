"use client";

import { useState, useRef, useCallback } from "react";
import { chatCommit } from "@/lib/api";
import styles from "./CommitChat.module.css";

function MicIcon({ className }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" />
      <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
      <line x1="12" x2="12" y1="19" y2="22" />
    </svg>
  );
}

function ChatIcon({ className }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="m3 21 1.9-5.7a8.5 8.5 0 1 1 3.8 3.8z" />
    </svg>
  );
}

export function CommitChat({ owner, repo, sha }) {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [listening, setListening] = useState(false);
  const [error, setError] = useState(null);
  const messagesEndRef = useRef(null);
  const recognitionRef = useRef(null);

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  const sendMessage = useCallback(
    async (text) => {
      const trimmed = (text || input).trim();
      if (!trimmed || loading) return;

      setInput("");
      setError(null);
      const userMsg = { id: `user-${Date.now()}`, role: "user", content: trimmed };
      setMessages((prev) => [...prev, userMsg]);
      setLoading(true);

      try {
        const history = [...messages, userMsg];
        const { answer } = await chatCommit(owner, repo, sha, history);
        setMessages((prev) => [
          ...prev,
          { id: `assistant-${Date.now()}`, role: "assistant", content: answer },
        ]);
        scrollToBottom();
      } catch (e) {
        setError(e.message || "Failed to get answer");
      } finally {
        setLoading(false);
      }
    },
    [owner, repo, sha, messages, input, loading, scrollToBottom]
  );

  const handleVoiceInput = useCallback(() => {
    const SpeechRecognition =
      typeof window !== "undefined" &&
      (window.SpeechRecognition || window.webkitSpeechRecognition);

    if (!SpeechRecognition) {
      setError("Voice input is not supported in this browser. Try Chrome or Edge.");
      return;
    }

    if (listening) {
      recognitionRef.current?.stop();
      setListening(false);
      return;
    }

    const recognition = new SpeechRecognition();
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.lang = "en-US";

    recognition.onstart = () => setListening(true);
    recognition.onend = () => setListening(false);
    recognition.onerror = (e) => {
      setListening(false);
      if (e.error !== "aborted") {
        setError("Voice recognition failed. Please try again.");
      }
    };
    recognition.onresult = (e) => {
      const last = e.results.length - 1;
      const transcript = e.results[last][0].transcript;
      if (e.results[last].isFinal) {
        setInput((prev) => (prev ? `${prev} ${transcript}` : transcript).trim());
      }
    };

    recognitionRef.current = recognition;
    recognition.start();
  }, [listening]);

  const handleSubmit = (e) => {
    e.preventDefault();
    sendMessage(input);
  };

  return (
    <section className={styles.chatSection}>
      <h2 className={styles.sectionTitle}>Ask about this feature</h2>
      <p className={styles.chatHint}>
        Ask questions in plain language or use voice input
      </p>

      <div className={styles.messages}>
        {messages.length === 0 && !loading && (
          <div className={styles.emptyState}>
            <div className={styles.emptyIcon}>
              <ChatIcon />
            </div>
            <p className={styles.emptyTitle}>What would you like to know?</p>
            <p className={styles.suggestions}>
              Try: &quot;What does this update do?&quot; or<br />
              &quot;How do I use the new feature?&quot;
            </p>
          </div>
        )}
        {messages.map((m) => (
          <div
            key={m.id}
            className={m.role === "user" ? styles.userMsg : styles.assistantMsg}
          >
            {m.content}
          </div>
        ))}
        {loading && (
          <div className={styles.assistantMsg}>
            <span className={styles.typing}>Thinking</span>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {error && (
        <div className={styles.chatError}>
          {error}
          <button type="button" onClick={() => setError(null)} className={styles.dismiss}>
            ×
          </button>
        </div>
      )}

      <form onSubmit={handleSubmit} className={styles.inputRow}>
        <button
          type="button"
          onClick={handleVoiceInput}
          className={`${styles.voiceBtn} ${listening ? styles.listening : ""}`}
          title={listening ? "Stop listening" : "Start voice input"}
          aria-label={listening ? "Stop listening" : "Start voice input"}
        >
          <MicIcon className={`${styles.micIcon} ${listening ? styles.micPulse : ""}`} />
        </button>
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Type your question..."
          className={styles.input}
          disabled={loading}
          aria-label="Chat message"
        />
        <button
          type="submit"
          disabled={loading || !input.trim()}
          className={styles.sendBtn}
        >
          Send
        </button>
      </form>
    </section>
  );
}
