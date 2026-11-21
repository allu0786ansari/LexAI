import { readSse } from "./sse";
import type {
  ChatEvent,
  ChatRequest,
  ChunkResponse,
  DocumentInfo,
  ErrorResponse,
  FeedbackRequest,
  SessionResponse,
} from "./types";

/** Empty in development and behind nginx (same origin, /api proxied); set to call another host. */
const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId?: string,
    readonly retryAfter?: number,
  ) {
    super(message);
  }
}

async function failure(response: Response): Promise<ApiError> {
  let body: Partial<ErrorResponse> = {};
  try {
    body = (await response.json()) as ErrorResponse;
  } catch {
    // not JSON (e.g. a proxy error page)
  }
  const retry = Number(response.headers.get("Retry-After") ?? "");
  return new ApiError(
    response.status,
    body.code ?? `http_${response.status}`,
    body.message ?? (response.status >= 500 ? "The server is unavailable." : response.statusText),
    body.request_id,
    Number.isFinite(retry) && retry > 0 ? retry : undefined,
  );
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, init);
  } catch {
    throw new ApiError(0, "network_error", "Cannot reach the LexAI server.");
  }
  if (!response.ok) throw await failure(response);
  return (await response.json()) as T;
}

const post = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  createSession: () => json<SessionResponse>("/api/v1/sessions", { method: "POST" }),
  chunk: (chunkId: string) => json<ChunkResponse>(`/api/v1/chunks/${encodeURIComponent(chunkId)}`),
  documents: () => json<DocumentInfo[]>("/api/v1/documents"),
  feedback: (body: FeedbackRequest) => json<{ id: number }>("/api/v1/feedback", post(body)),
  pdfUrl: (pdfUrl: string) => `${BASE}${pdfUrl}`,

  /** Streams the answer; each parsed event is passed to `onEvent`. Throws ApiError before the stream starts. */
  async chat(body: ChatRequest, onEvent: (event: ChatEvent) => void, signal: AbortSignal): Promise<void> {
    let response: Response;
    try {
      response = await fetch(`${BASE}/api/v1/chat`, { ...post(body), signal });
    } catch (err) {
      if (signal.aborted) throw err;
      throw new ApiError(0, "network_error", "Cannot reach the LexAI server.");
    }
    if (!response.ok) throw await failure(response);
    if (!response.body) throw new ApiError(0, "no_stream", "The server sent no answer stream.");
    for await (const frame of readSse(response.body)) {
      onEvent({ event: frame.event, data: JSON.parse(frame.data) } as ChatEvent);
    }
  },
};

/** A sentence for the user, from any error the client can raise. */
export function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code === "rate_limited") return `Too many questions in a short time. Try again in ${err.retryAfter ?? 60} s.`;
    if (err.code === "network_error") return "Cannot reach the LexAI server. Is the API running?";
    if (err.code === "index_not_ready") return "The search index is not built yet.";
    if (err.code === "upstream_unavailable") return "The language model is unavailable. Try again shortly.";
    return err.message;
  }
  return err instanceof Error ? err.message : "Something went wrong.";
}
