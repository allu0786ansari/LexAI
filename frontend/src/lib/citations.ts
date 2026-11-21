/**
 * Citation markers in answers. "[2]" becomes the Markdown link "[\[2\]](#cite-2)",
 * which AnswerMarkdown renders as a button opening passage 2. Text inside inline
 * code spans is left alone.
 */
const MARKER = /\[(\d{1,3})\]/g;
export const CITE_PREFIX = "#cite-";

export function linkCitations(answer: string, maxRef: number): string {
  return answer
    .split(/(`[^`]*`)/)
    .map((part, i) =>
      i % 2 === 1
        ? part
        : part.replace(MARKER, (whole, n: string) => {
            const ref = Number(n);
            return ref >= 1 && ref <= maxRef ? `[\\[${ref}\\]](${CITE_PREFIX}${ref})` : whole;
          }),
    )
    .join("");
}

/** Only http(s) links and citation anchors survive; anything else (javascript:, data:, ...) is dropped. */
export function safeUrl(url: string): string {
  if (url.startsWith(CITE_PREFIX) && /^\d{1,3}$/.test(url.slice(CITE_PREFIX.length))) return url;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.href : "";
  } catch {
    return "";
  }
}

export function citedRef(href: string | undefined): number | null {
  if (!href?.startsWith(CITE_PREFIX)) return null;
  const n = Number(href.slice(CITE_PREFIX.length));
  return Number.isInteger(n) && n > 0 ? n : null;
}
