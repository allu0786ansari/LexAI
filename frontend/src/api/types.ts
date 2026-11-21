// Names for the generated OpenAPI types (src/api/schema.d.ts, from the backend's schema).
import type { components } from "./schema";

type S = components["schemas"];
export type Citation = S["Citation"];
export type MetaPayload = S["MetaPayload"];
export type DonePayload = S["DonePayload"];
export type VerificationPayload = S["VerificationPayload"];
export type ErrorPayload = S["ErrorPayload"];
export type TokenPayload = S["TokenPayload"];
export type ChatEvents = S["ChatEvents"];
export type ChatRequest = S["ChatRequest"];
export type ChatFilters = S["ChatFilters"];
export type ChunkResponse = S["ChunkResponse"];
export type DocumentInfo = S["DocumentInfo"];
export type FeedbackRequest = S["FeedbackRequest"];
export type SessionResponse = S["SessionResponse"];
export type ErrorResponse = S["ErrorResponse"];

/** One server-sent event of POST /api/v1/chat, typed by name. */
export type ChatEvent = {
  [K in keyof ChatEvents]: { event: K; data: ChatEvents[K] };
}[keyof ChatEvents];
