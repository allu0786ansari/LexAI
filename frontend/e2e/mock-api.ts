import type { Page, Route } from "@playwright/test";

export const SESSION = "8f14e45f-ceea-4e7a-9c9d-6f3b1c0a2b11";

export const citations = [
  { ref: 1, chunk_id: "c-bns-103", label: "BNS s.103", doc_id: "bns-2023", doc_title: "The Bharatiya Nyaya Sanhita, 2023",
    unit_label: "Section", unit: "103", unit_title: "Punishment for murder", status: "in force", source_file: "bns-2023.pdf",
    page: 31, doc_type: "statute", sections: ["BNS s.103"], relevance_score: 0.91, snippet: "Whoever commits murder shall be punished", exact: false },
  { ref: 2, chunk_id: "c-ipc-302", label: "IPC s.302", doc_id: "ipc-1860", doc_title: "The Indian Penal Code, 1860",
    unit_label: "Section", unit: "302", unit_title: "Punishment for murder", status: "repealed 2024-07-01; replaced by BNS; applies to earlier offences",
    source_file: "ipc-1860.pdf", page: 78, doc_type: "statute", sections: ["IPC s.302"], relevance_score: 0.88, snippet: "Whoever commits murder", exact: false },
]; // prettier-ignore

export function sse(answer: string, opts: { requestId?: string; flags?: { kind: string; detail: string }[] } = {}): string {
  const requestId = opts.requestId ?? "req-0001-abcdef";
  const flags = opts.flags ?? [];
  const verification = { valid: flags.length === 0, coverage: 1, flags, removed_markers: [] };
  const frames: [string, unknown][] = [
    ["meta", { request_id: requestId, session_id: SESSION, trace_id: "", standalone_query: "q", intent: "concept",
      references: [], applicable_code: "new", applicable_code_reason: "no offence date given" }], // prettier-ignore
    ["citations", citations],
    ...answer.match(/.{1,12}/gs)!.map((t): [string, unknown] => ["token", { text: t }]),
    ["verification", verification],
    ["done", { answer, session_id: SESSION, request_id: requestId, citations, latency: { total_ms: 1234 },
      prompt_version: "answer_v4", index_version: "test", usage: null, cached: false, abstained: false, notes: [], verification }], // prettier-ignore
  ];
  return frames.map(([e, d]) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`).join("");
}

export interface Calls {
  chat: Record<string, unknown>[];
  feedback: Record<string, unknown>[];
  sessions: number;
}

export async function mockApi(page: Page, chat?: (route: Route, n: number) => Promise<void> | void): Promise<Calls> {
  const calls: Calls = { chat: [], feedback: [], sessions: 0 };
  await page.route("**/api/v1/sessions", (route) => {
    calls.sessions += 1;
    return route.fulfill({ status: 201, json: { session_id: SESSION, expires_in_s: 1800 } });
  });
  await page.route("**/api/v1/chat", async (route) => {
    calls.chat.push(route.request().postDataJSON() as Record<string, unknown>);
    if (chat) return chat(route, calls.chat.length);
    return route.fulfill({
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
      body: sse("Murder is punished with death or imprisonment for life [1]. It replaced IPC s.302 [2]."),
    });
  });
  await page.route("**/api/v1/chunks/*", (route) => {
    const id = decodeURIComponent(route.request().url().split("/").pop()!);
    const c = citations.find((x) => x.chunk_id === id);
    if (!c) return route.fulfill({ status: 404, json: { code: "chunk_not_found", message: "No passage", request_id: "r" } });
    return route.fulfill({
      json: { chunk_id: c.chunk_id, label: c.label, doc_id: c.doc_id, doc_title: c.doc_title, doc_type: "statute",
        unit_label: "Section", unit: c.unit, unit_title: c.unit_title, status: c.status,
        text: `${c.label} full text. Whoever commits murder shall be punished with death or imprisonment for life.`,
        page_start: c.page, page_end: c.page, source_file: c.source_file, pdf_url: `/api/v1/documents/${c.doc_id}/pdf#page=${c.page}` }, // prettier-ignore
    });
  });
  await page.route("**/api/v1/documents", (route) =>
    route.fulfill({
      json: [
        { doc_id: "bns-2023", title: "The Bharatiya Nyaya Sanhita, 2023", short_name: "BNS", doc_type: "statute", status: "in force", chunks: 900, has_pdf: true },
        { doc_id: "ipc-1860", title: "The Indian Penal Code", short_name: "IPC", doc_type: "statute", status: "repealed 2024-07-01", repealed_on: "2024-07-01", chunks: 800, has_pdf: true },
      ],
    }),
  );
  await page.route("**/api/v1/feedback", (route) => {
    calls.feedback.push(route.request().postDataJSON() as Record<string, unknown>);
    return route.fulfill({ status: 201, json: { id: calls.feedback.length } });
  });
  return calls;
}
