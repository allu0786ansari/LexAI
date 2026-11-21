import { describe, expect, it } from "vitest";
import type { ChatEvent, Citation, DonePayload } from "@/api/types";
import { type AssistantMessage, type State, load, reducer, save } from "./chats";

const citation = { ref: 1, chunk_id: "c1", label: "BNS s.103", doc_id: "bns-2023", doc_title: "BNS", unit_label: "Section",
  status: "in force", source_file: "bns.pdf", doc_type: "statute", relevance_score: 1 } as Citation; // prettier-ignore

function start(): { state: State; chatId: string } {
  const state = load({ getItem: () => null });
  const chatId = state.activeId!;
  return { state: reducer(state, { type: "ask", chatId, question: "Punishment for murder?", userId: "u1", answerId: "a1" }), chatId };
}

function answer(state: State): AssistantMessage {
  return state.chats[0]!.messages.find((m) => m.id === "a1") as AssistantMessage;
}

const ev = (e: ChatEvent) => ({ type: "event" as const, chatId: "", answerId: "a1", event: e });

describe("chat reducer", () => {
  it("streams tokens in memory and marks a save only when the answer finishes (B12)", () => {
    let { state, chatId } = start();
    const step = (e: ChatEvent) => (state = reducer(state, { ...ev(e), chatId }));
    expect(state.chats[0]!.title).toBe("Punishment for murder?");
    const before = state.persist;
    step({ event: "citations", data: [citation] });
    for (const t of ["Death ", "or life [1]", "[7]."]) step({ event: "token", data: { text: t } });
    expect(answer(state).content).toBe("Death or life [1][7].");
    expect(state.persist).toBe(before); // 3 tokens, no save
    step({
      event: "done",
      data: { answer: "Death or life [1].", citations: [citation], request_id: "r1", session_id: "s",
        latency: { total_ms: 900 }, abstained: false, cached: false } as DonePayload, // prettier-ignore
    });
    expect(state.persist).toBe(before + 1);
    expect(answer(state)).toMatchObject({ status: "done", content: "Death or life [1].", requestId: "r1", latencyMs: 900 });
  });

  it("records stop, failure, errors from the stream and feedback", () => {
    let { state, chatId } = start();
    state = reducer(state, { type: "stopped", chatId, answerId: "a1" });
    expect(answer(state).status).toBe("stopped");
    state = reducer(state, { type: "regenerate", chatId, answerId: "a1" });
    expect(answer(state)).toMatchObject({ status: "streaming", content: "", question: "Punishment for murder?" });
    state = reducer(state, { ...ev({ event: "error", data: { code: "upstream_unavailable", message: "down", request_id: "r2" } }), chatId });
    expect(answer(state)).toMatchObject({ status: "error", error: "down", requestId: "r2" });
    state = reducer(state, { type: "feedback", chatId, answerId: "a1", rating: "down" });
    expect(answer(state).feedback).toBe("down");
  });

  it("reuses an empty chat, deletes and selects", () => {
    let state = load({ getItem: () => null });
    const first = state.activeId;
    expect(reducer(state, { type: "new" }).chats).toHaveLength(1); // empty chat reused
    state = start().state;
    state = reducer(state, { type: "new" });
    expect(state.chats).toHaveLength(2);
    const newest = state.activeId!;
    state = reducer(state, { type: "delete", chatId: newest });
    expect(state.chats).toHaveLength(1);
    expect(state.activeId).toBe(state.chats[0]!.id);
    expect(first).toBeTruthy();
  });

  it("saves and restores; an answer cut off mid-stream comes back as stopped", () => {
    const { state } = start();
    const store = new Map<string, string>();
    save(state, { setItem: (k, v) => store.set(k, v) });
    const restored = load({ getItem: (k) => store.get(k) ?? null });
    expect(restored.chats[0]!.messages).toHaveLength(2);
    expect((restored.chats[0]!.messages[1] as AssistantMessage).status).toBe("stopped");
    expect(load({ getItem: () => "{not json" }).chats).toHaveLength(1); // corrupt storage: fresh start
  });
});
