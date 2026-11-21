import { useEffect, useRef, useState } from "react";
import { ExternalLink, X } from "lucide-react";
import { api, describeError } from "@/api/client";
import type { Citation, ChunkResponse } from "@/api/types";

interface Props {
  citation: Citation;
  onClose: () => void;
}

/** The cited passage: whole provision text, act, section, status, page and a link to that PDF page. */
export function SourcePanel({ citation, onClose }: Props) {
  const [chunk, setChunk] = useState<ChunkResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    let live = true;
    setChunk(null);
    setError(null);
    api
      .chunk(citation.chunk_id)
      .then((c) => live && setChunk(c))
      .catch((e: unknown) => live && setError(describeError(e)));
    return () => {
      live = false;
    };
  }, [citation.chunk_id]);

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    heading.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      previous?.focus?.();
    };
  }, [citation.chunk_id, onClose]);

  const page = chunk?.page_start ?? citation.page;
  return (
    <aside
      role="dialog"
      aria-labelledby="source-title"
      className="fixed inset-0 z-40 flex flex-col bg-paper text-text-paper shadow-2xl md:static md:z-auto md:w-[26rem] md:shrink-0 md:border-l md:border-paper-2"
    >
      <header className="flex items-start gap-3 border-b border-paper-2 px-5 py-4">
        <span className="mt-0.5 rounded bg-seal px-1.5 py-0.5 font-mono text-xs text-paper">[{citation.ref}]</span>
        <div className="min-w-0 flex-1">
          <h2 id="source-title" ref={heading} tabIndex={-1} className="font-display text-lg leading-tight outline-none">
            {citation.label}
            {citation.unit_title ? ` — ${citation.unit_title}` : ""}
          </h2>
          <p className="mt-0.5 text-xs text-muted-paper">{citation.doc_title}</p>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded p-1 text-muted-paper hover:bg-paper-2 hover:text-text-paper"
          aria-label="Close source"
        >
          <X className="size-5" />
        </button>
      </header>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 border-b border-paper-2 px-5 py-3 text-xs">
        {citation.status && (
          <>
            <dt className="text-muted-paper">Status</dt>
            <dd className={citation.status.startsWith("repealed") ? "text-seal" : ""}>{citation.status}</dd>
          </>
        )}
        <dt className="text-muted-paper">Source</dt>
        <dd className="truncate">{citation.source_file}</dd>
        {page != null && (
          <>
            <dt className="text-muted-paper">Page</dt>
            <dd>
              {page}
              {chunk?.page_end && chunk.page_end !== page ? `–${chunk.page_end}` : ""}
            </dd>
          </>
        )}
        {citation.exact && (
          <>
            <dt className="text-muted-paper">Why</dt>
            <dd>The question named this provision</dd>
          </>
        )}
      </dl>
      <div className="scrollbar-thin flex-1 overflow-y-auto px-5 py-4" aria-busy={!chunk && !error}>
        {error && <p className="text-sm text-seal">{error}</p>}
        {!chunk && !error && <p className="text-sm leading-relaxed text-muted-paper">{citation.snippet}…</p>}
        {chunk && <p className="whitespace-pre-wrap font-display text-[0.95rem] leading-relaxed">{chunk.text}</p>}
      </div>
      {chunk?.pdf_url && (
        <footer className="border-t border-paper-2 px-5 py-3">
          <a
            href={api.pdfUrl(chunk.pdf_url)}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 text-sm font-medium text-seal hover:underline"
          >
            Open the PDF{page != null ? ` at page ${page}` : ""} <ExternalLink className="size-3.5" aria-hidden />
          </a>
        </footer>
      )}
    </aside>
  );
}
