import { useCallback, useEffect, useRef, useState } from "react";
import { Sidebar } from "@/components/Sidebar";
import { MessageList } from "@/components/MessageList";
import { InputBox } from "@/components/InputBox";
import { createSession, streamQuery, loadSessionList, saveSessionList, ApiError } from "@/api/apiClient";

function makeId() {
  return typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : `id-${Date.now()}-${Math.random()}`;
}

function titleFromQuestion(question) {
  const trimmed = question.trim();
  return trimmed.length > 48 ? `${trimmed.slice(0, 48)}…` : trimmed;
}

export function ChatInterface() {
  const initialSessions = loadSessionList();
  const [sessions, setSessions] = useState(() => initialSessions);
  const [activeSessionId, setActiveSessionId] = useState(() => initialSessions[0]?.sessionId ?? null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [errorMessage, setErrorMessage] = useState(null);
  const [statusMessage, setStatusMessage] = useState("Ready");
  const bottomRef = useRef(null);
  const abortRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [sessions, activeSessionId]);

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  useEffect(() => {
    if (!activeSessionId && sessions.length > 0) {
      setActiveSessionId(sessions[0].sessionId);
    }
  }, [activeSessionId, sessions]);

  const persist = useCallback((next) => {
    setSessions(next);
    saveSessionList(next);
  }, []);

  const updateActiveMessages = useCallback(
    (sessionId, updater) => {
      setSessions((prev) => {
        const next = prev.map((s) => (s.sessionId === sessionId ? { ...s, messages: updater(s.messages) } : s));
        saveSessionList(next);
        return next;
      });
    },
    []
  );

  const handleNewChat = useCallback(async () => {
    try {
      const sessionId = await createSession();
      const next = [{ sessionId, title: "New conversation", updatedAt: Date.now(), messages: [] }, ...sessions];
      persist(next);
      setActiveSessionId(sessionId);
      setErrorMessage(null);
      setStatusMessage("Session created successfully.");
    } catch (err) {
      console.error("Failed to start a new session", err);
      setErrorMessage(err instanceof Error ? err.message : "Unable to start a new chat. Please try again.");
      setStatusMessage("Session creation failed.");
    }
  }, [sessions, persist]);

  const handleSelectSession = useCallback(
    (sessionId) => {
      setActiveSessionId(sessionId);
      const existing = sessions.find((s) => s.sessionId === sessionId);
      if (!existing) {
        const next = [{ sessionId, title: "Conversation", updatedAt: Date.now(), messages: [] }, ...sessions];
        persist(next);
      }
    },
    [sessions, persist]
  );

  const handleSend = useCallback(
    async (question) => {
      let sessionId = activeSessionId;
      let workingSessions = sessions;
      setErrorMessage(null);

      if (!sessionId) {
        try {
          sessionId = await createSession();
          setErrorMessage(null);
        } catch (err) {
          console.error("Failed to start a session", err);
          setErrorMessage("Unable to start a session. Please check the backend and try again.");
          return;
        }
        workingSessions = [{ sessionId, title: titleFromQuestion(question), updatedAt: Date.now(), messages: [] }, ...sessions];
        setActiveSessionId(sessionId);
      }

      const userMessage = { id: makeId(), role: "user", content: question };
      const assistantMessage = { id: makeId(), role: "assistant", content: "", citations: [], isStreaming: true, error: null };

      const withNewMessages = workingSessions.map((s) =>
        s.sessionId === sessionId
          ? {
              ...s,
              title: s.messages.length === 0 ? titleFromQuestion(question) : s.title,
              updatedAt: Date.now(),
              messages: [...s.messages, userMessage, assistantMessage],
            }
          : s
      );
      persist(withNewMessages);
      setErrorMessage(null);
      setStatusMessage("Sending question...");
      setIsStreaming(true);

      const controller = new AbortController();
      abortRef.current = controller;

      try {
        for await (const event of streamQuery(question, sessionId, { signal: controller.signal })) {
          if (event.event === "citations") {
            updateActiveMessages(sessionId, (msgs) =>
              msgs.map((m) => (m.id === assistantMessage.id ? { ...m, citations: event.data } : m))
            );
          } else if (event.event === "token") {
            updateActiveMessages(sessionId, (msgs) =>
              msgs.map((m) => (m.id === assistantMessage.id ? { ...m, content: m.content + event.data.text } : m))
            );
          } else if (event.event === "done") {
            updateActiveMessages(sessionId, (msgs) =>
              msgs.map((m) =>
                m.id === assistantMessage.id
                  ? { ...m, content: event.data.answer, citations: event.data.citations, isStreaming: false }
                  : m
              )
            );
          } else if (event.event === "error") {
            updateActiveMessages(sessionId, (msgs) =>
              msgs.map((m) =>
                m.id === assistantMessage.id ? { ...m, isStreaming: false, error: event.data.message } : m
              )
            );
          }
        }
      } catch (err) {
        if (err.name !== "AbortError") {
          const message = err instanceof ApiError ? err.message : "Connection lost while streaming the answer.";
          updateActiveMessages(sessionId, (msgs) =>
            msgs.map((m) => (m.id === assistantMessage.id ? { ...m, isStreaming: false, error: message } : m))
          );
          setErrorMessage(message);
          setStatusMessage("Error receiving answer.");
        }
      } finally {
        setIsStreaming(false);
      }
    },
    [activeSessionId, sessions, persist, updateActiveMessages]
  );

  const activeMessages = sessions.find((s) => s.sessionId === activeSessionId)?.messages ?? [];

  return (
    <div className="flex h-screen">
      <Sidebar
        sessions={sessions}
        activeSessionId={activeSessionId}
        onSelectSession={handleSelectSession}
        onNewChat={handleNewChat}
      />
      <div className="ruled-margin flex flex-1 flex-col bg-ink px-4 pt-4 md:px-8">
        <header className="mb-2 flex items-center gap-2 border-b border-ink-3 pb-4">
          <span className="font-display text-xl font-medium tracking-tight text-text-ink">LexAI</span>
          <span className="font-mono text-[11px] uppercase tracking-widest text-muted-ink">Legal Research Desk</span>
        </header>
        <MessageList messages={activeMessages} bottomRef={bottomRef} />
        <div className="sticky bottom-0 bg-ink pb-4 pt-2 md:pb-8">
          {errorMessage && (
            <div className="mb-2 rounded-md border border-brass/30 bg-brass/5 px-3 py-2 text-sm text-brass">
              {errorMessage}
            </div>
          )}
          <div className="mb-2 flex items-center justify-between gap-3 px-2 text-[11px] uppercase tracking-wide text-muted-ink">
            <span>{statusMessage}</span>
            <span>{isStreaming ? "Streaming answer..." : activeSessionId ? `Active session: ${activeSessionId.slice(0, 8)}` : "No active session"}</span>
          </div>
          <InputBox onSend={handleSend} disabled={isStreaming} />
          <p className="mt-2 text-center text-[11px] text-muted-ink">
            LexAI answers are grounded in retrieved statute and case text, but can be incomplete — verify anything
            that matters against the primary source.
          </p>
        </div>
      </div>
    </div>
  );
}
