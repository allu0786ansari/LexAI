/**
 * Server-Sent Events parser for a fetch() body (EventSource cannot POST).
 * Frames are separated by a blank line; `event:` names the frame, `data:` lines
 * are joined with "\n". Handles frames split across chunks and CRLF line ends.
 */
export interface SseFrame {
  event: string;
  data: string;
}

export function parseFrame(raw: string): SseFrame | null {
  let event = "message";
  const data: string[] = [];
  for (const line of raw.split("\n")) {
    if (line === "" || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  return data.length ? { event, data: data.join("\n") } : null;
}

export async function* readSse(body: ReadableStream<Uint8Array>): AsyncGenerator<SseFrame> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n?/g, "\n");
      let cut: number;
      while ((cut = buffer.indexOf("\n\n")) !== -1) {
        const frame = parseFrame(buffer.slice(0, cut));
        buffer = buffer.slice(cut + 2);
        if (frame) yield frame;
      }
    }
    const last = parseFrame((buffer + decoder.decode()).replace(/\r\n?/g, "\n"));
    if (last) yield last;
  } finally {
    reader.releaseLock();
  }
}
