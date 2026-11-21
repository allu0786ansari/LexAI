/**
 * Conversations, as a reducer. Streaming text lives only in React state; the
 * `persist` counter changes when an answer finishes, stops or fails (and on
 * new/delete), and the app saves to localStorage only then — never per token (B12).
 */
import type { Citation, ChatEvent, DonePayload, MetaPayload, VerificationPayload } from "@/api/types";

export type AnswerStatus = "streaming" | "done" | "stopped" | "error";

export interface UserMessage {
  id: string;
  role: "user";
  content: string;
}

export interface AssistantMessage {
  id: string;
  role: "assistant";
  question: string;
  content: string;
  status: AnswerStatus;
  citations: Citation[];
  meta?: Pick<MetaPayload, "intent" | "applicable_code" | "applicable_code_reason" | "references" | "request_id" | "trace_id">;
  verification?: VerificationPayload;
  abstained?: boolean;
  cached?: boolean;
  latencyMs?: number;
  requestId?: string;
  error?: string;
  feedback?: "up" | "down";
}

export type Message = UserMessage | AssistantMessage;

export interface Chat {
  id: string;
  sessionId: string | null;
  title: string;
  updatedAt: number;
  messages: Message[];
}

export interface State {
  chats: Chat[];
  activeId: string | null;
  persist: number;
}

export type Action =
  | { type: "new" }
  | { type: "select"; chatId: string }
  | { type: "delete"; chatId: string }
  | { type: "ask"; chatId: string; question: string; userId: string; answerId: string }
  | { type: "regenerate"; chatId: string; answerId: string }
  | { type: "session"; chatId: string; sessionId: string | null }
  | { type: "event"; chatId: string; answerId: string; event: ChatEvent }
  | { type: "stopped"; chatId: string; answerId: string }
  | { type: "failed"; chatId: string; answerId: string; error: string }
  | { type: "feedback"; chatId: string; answerId: string; rating: "up" | "down" };

let counter = 0;
export function newId(): string {
  counter += 1;
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `id-${Date.now()}-${counter}`;
}

export function emptyChat(): Chat {
  return { id: newId(), sessionId: null, title: "New conversation", updatedAt: Date.now(), messages: [] };
}

export function titleFrom(question: string): string {
  const q = question.trim().replace(/\s+/g, " ");
  return q.length > 60 ? `${q.slice(0, 57)}…` : q;
}

function updateChat(state: State, chatId: string, fn: (chat: Chat) => Chat, persist = false): State {
  return {
    ...state,
    chats: state.chats.map((c) => (c.id === chatId ? fn(c) : c)),
    persist: persist ? state.persist + 1 : state.persist,
  };
}

function updateAnswer(
  state: State,
  chatId: string,
  answerId: string,
  fn: (m: AssistantMessage) => AssistantMessage,
  persist = false,
): State {
  return updateChat(
    state,
    chatId,
    (chat) => ({
      ...chat,
      updatedAt: persist ? Date.now() : chat.updatedAt,
      messages: chat.messages.map((m) => (m.id === answerId && m.role === "assistant" ? fn(m) : m)),
    }),
    persist,
  );
}

function applyEvent(m: AssistantMessage, event: ChatEvent): AssistantMessage {
  switch (event.event) {
    case "meta": {
      const { intent, applicable_code, applicable_code_reason, references, request_id, trace_id } = event.data;
      return { ...m, meta: { intent, applicable_code, applicable_code_reason, references, request_id, trace_id }, requestId: request_id };
    }
    case "citations":
      return { ...m, citations: event.data };
    case "token":
      return { ...m, content: m.content + event.data.text };
    case "verification":
      return { ...m, verification: event.data };
    case "done": {
      const done: DonePayload = event.data;
      return {
        ...m,
        content: done.answer, // the verified answer (invalid markers removed)
        citations: done.citations,
        status: "done",
        abstained: done.abstained,
        cached: done.cached,
        latencyMs: done.latency.total_ms,
        requestId: done.request_id,
        verification: (done.verification as VerificationPayload | null | undefined) ?? m.verification,
      };
    }
    case "error":
      return { ...m, status: "error", error: event.data.message, requestId: event.data.request_id };
  }
}

export function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "new": {
      const current = state.chats.find((c) => c.id === state.activeId);
      if (current && current.messages.length === 0) return state; // reuse the empty one
      const chat = emptyChat();
      return { chats: [chat, ...state.chats], activeId: chat.id, persist: state.persist + 1 };
    }
    case "select":
      return { ...state, activeId: action.chatId };
    case "delete": {
      const chats = state.chats.filter((c) => c.id !== action.chatId);
      const activeId = state.activeId === action.chatId ? (chats[0]?.id ?? null) : state.activeId;
      return { chats, activeId, persist: state.persist + 1 };
    }
    case "ask":
      return updateChat(state, action.chatId, (chat) => ({
        ...chat,
        title: chat.messages.length === 0 ? titleFrom(action.question) : chat.title,
        updatedAt: Date.now(),
        messages: [
          ...chat.messages,
          { id: action.userId, role: "user", content: action.question },
          { id: action.answerId, role: "assistant", question: action.question, content: "", status: "streaming", citations: [] },
        ],
      }));
    case "regenerate":
      return updateAnswer(state, action.chatId, action.answerId, (m) => ({
        id: m.id,
        role: "assistant",
        question: m.question,
        content: "",
        status: "streaming",
        citations: [],
      }));
    case "session":
      return updateChat(state, action.chatId, (chat) => ({ ...chat, sessionId: action.sessionId }), true);
    case "event": {
      const finished = action.event.event === "done" || action.event.event === "error";
      return updateAnswer(state, action.chatId, action.answerId, (m) => applyEvent(m, action.event), finished);
    }
    case "stopped":
      return updateAnswer(state, action.chatId, action.answerId, (m) => ({ ...m, status: "stopped" }), true);
    case "failed":
      return updateAnswer(state, action.chatId, action.answerId, (m) => ({ ...m, status: "error", error: action.error }), true);
    case "feedback":
      return updateAnswer(state, action.chatId, action.answerId, (m) => ({ ...m, feedback: action.rating }), true);
  }
}

// ------------------------------------------------------------------ storage

const KEY = "lexai.chats.v2";
const MAX_CHATS = 50;

export function load(storage: Pick<Storage, "getItem"> = localStorage): State {
  try {
    const raw = storage.getItem(KEY);
    if (raw) {
      const chats = (JSON.parse(raw) as Chat[]).map((chat) => ({
        ...chat,
        // An answer still streaming when the page closed was cut off.
        messages: chat.messages.map((m) =>
          m.role === "assistant" && m.status === "streaming" ? { ...m, status: "stopped" as const } : m,
        ),
      }));
      if (chats.length) return { chats, activeId: chats[0]?.id ?? null, persist: 0 };
    }
  } catch {
    // corrupt or unavailable storage: start fresh
  }
  const chat = emptyChat();
  return { chats: [chat], activeId: chat.id, persist: 0 };
}

export function save(state: State, storage: Pick<Storage, "setItem"> = localStorage): void {
  try {
    const chats = [...state.chats].sort((a, b) => b.updatedAt - a.updatedAt).slice(0, MAX_CHATS);
    storage.setItem(KEY, JSON.stringify(chats));
  } catch {
    // quota exceeded or storage disabled: the chats stay in memory
  }
}
