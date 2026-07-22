/**
 * SSE client for POST /api/query, hand-rolled with fetch() + ReadableStream
 * rather than a third-party SSE package (checked: `axios-sse` exists on
 * npm but is a single-maintainer, low-adoption package; browser-native
 * `EventSource` can't be used at all here since it only supports GET
 * requests with no custom body, and this endpoint needs a JSON body).
 * This is a well-understood, standard pattern and small enough to fully
 * own and test.
 *
 * The one thing this MUST get right: a network chunk boundary can land
 * anywhere — mid-line, mid-JSON-value, anywhere — so frames are only
 * parsed once a full `\n\n` terminator has been seen; everything after
 * the last `\n\n` is held back in `buffer` until more data arrives.
 * Verified with a dedicated test that deliberately splits a single SSE
 * frame's JSON payload across multiple simulated reads.
 */

const API_BASE = (import.meta.env && import.meta.env.VITE_API_BASE_URL) || "http://localhost:8000";
const SESSION_STORAGE_KEY = "lexai_sessions_v1";

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Parses one complete SSE frame (everything between two `\n\n`s) into {event, data}, or null if malformed/empty. */
export function parseSSEFrame(frame) {
  let eventType = "message";
  const dataLines = [];
  for (const rawLine of frame.split("\n")) {
    const line = rawLine.replace(/\r$/, ""); // tolerate CRLF
    if (line.startsWith("event:")) {
      eventType = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (dataLines.length === 0) return null;
  try {
    return { event: eventType, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    return null; // malformed/partial JSON - drop this frame rather than throw and kill the whole stream
  }
}

/**
 * Async generator yielding {event, data} objects as they arrive.
 * event is one of: "citations" | "token" | "done" | "error" (see backend
 * app/services/qa_service.py module docstring for the contract).
 */
export async function* streamQuery(question, sessionId, { signal } = {}) {
  const response = await fetch(`${API_BASE}/api/query`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, session_id: sessionId ?? null }),
    signal,
  });

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail || body.error || detail;
    } catch {
      /* response wasn't JSON, keep statusText */
    }
    throw new ApiError(detail, response.status);
  }
  if (!response.body) {
    throw new ApiError("Response had no body to stream.", response.status);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? ""; // last segment may be incomplete - held back for the next read
      for (const frame of frames) {
        const event = parseSSEFrame(frame);
        if (event) yield event;
      }
    }
    if (buffer.trim()) {
      const event = parseSSEFrame(buffer);
      if (event) yield event;
    }
  } finally {
    reader.releaseLock();
  }
}

export async function createSession() {
  const response = await fetch(`${API_BASE}/api/session`, { method: "POST" });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail || body.error || detail;
    } catch {
      /* keep statusText */
    }
    throw new ApiError(`Could not start a new session: ${detail}`, response.status);
  }

  let body;
  try {
    body = await response.json();
  } catch (err) {
    const text = await response.text().catch(() => "<unreadable response>");
    throw new ApiError(`Could not parse session response: ${text}`, response.status);
  }

  if (!body || typeof body.session_id !== "string") {
    throw new ApiError(`Invalid session response body: ${JSON.stringify(body)}`, response.status);
  }

  return body.session_id;
}

export async function fetchHealth() {
  const response = await fetch(`${API_BASE}/api/health`);
  if (!response.ok) throw new ApiError("Health check failed.", response.status);
  return response.json();
}

/* ---- Local session-list persistence (see components/Sidebar.jsx) ----
 * The backend keeps server-side memory per session_id (30-min TTL, see
 * app/services/memory.py) but has no endpoint to list past sessions or
 * replay their transcripts - it's a memory store, not a chat history
 * database. So "chat history sidebar" is implemented client-side: the
 * browser remembers which sessions it started and their message
 * transcripts in localStorage, and reuses the same session_id when you
 * click back into one (continuing the same server-side memory, if it
 * hasn't expired - if it has, the backend transparently starts a fresh
 * one, per SessionStore.get_history's documented fallback).
 */
export function loadSessionList() {
  try {
    const raw = localStorage.getItem(SESSION_STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

export function saveSessionList(sessions) {
  try {
    localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(sessions));
  } catch {
    /* localStorage unavailable (private browsing, quota) - degrade to in-memory only for this tab */
  }
}
