import { describe, expect, it } from "vitest";
import { parseFrame, readSse } from "./sse";

function stream(chunks: string[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  return new ReadableStream({
    start(c) {
      for (const chunk of chunks) c.enqueue(enc.encode(chunk));
      c.close();
    },
  });
}

async function collect(chunks: string[]) {
  const out = [];
  for await (const f of readSse(stream(chunks))) out.push(f);
  return out;
}

describe("SSE parser", () => {
  it("parses one frame", () => {
    expect(parseFrame('event: token\ndata: {"text":"hi"}')).toEqual({ event: "token", data: '{"text":"hi"}' });
  });

  it("joins multi-line data, ignores comments, defaults the event name", () => {
    expect(parseFrame(": keep-alive\ndata: a\ndata: b")).toEqual({ event: "message", data: "a\nb" });
    expect(parseFrame(": only a comment")).toBeNull();
  });

  it("handles frames split across chunks, CRLF and a trailing frame without a blank line", async () => {
    const frames = await collect([
      'event: meta\r\ndata: {"a":',
      '1}\r\n\r\nevent: tok',
      'en\ndata: {"text":"x"}\n\n',
      'event: done\ndata: {"ok":true}',
    ]);
    expect(frames.map((f) => f.event)).toEqual(["meta", "token", "done"]);
    expect(JSON.parse(frames[0]!.data)).toEqual({ a: 1 });
  });

  it("decodes multi-byte characters split between chunks", async () => {
    const bytes = new TextEncoder().encode('data: {"text":"धारा"}\n\n');
    const s = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(bytes.slice(0, 15));
        c.enqueue(bytes.slice(15));
        c.close();
      },
    });
    const out = [];
    for await (const f of readSse(s)) out.push(f);
    expect(JSON.parse(out[0]!.data).text).toBe("धारा");
  });
});
