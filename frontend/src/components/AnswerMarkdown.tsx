import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { citedRef, linkCitations, safeUrl } from "@/lib/citations";

interface Props {
  text: string;
  citationCount: number;
  onCite: (ref: number) => void;
}

/**
 * Model output as Markdown. Raw HTML is never rendered (`skipHtml`), links are limited to
 * http(s) (`safeUrl`), and [n] markers become buttons that open the cited passage.
 */
export function AnswerMarkdown({ text, citationCount, onCite }: Props) {
  return (
    <div className="prose prose-sm max-w-none font-body text-text-paper prose-p:my-2 prose-li:my-0.5 prose-strong:text-text-paper">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        urlTransform={safeUrl}
        components={{
          a({ href, children }) {
            const ref = citedRef(href);
            if (ref !== null) {
              return (
                <button
                  type="button"
                  onClick={() => onCite(ref)}
                  className="mx-0.5 inline-flex items-center rounded bg-seal/10 px-1 font-mono text-[0.78em] font-medium text-seal no-underline hover:bg-seal/20 focus-visible:outline-2"
                  aria-label={`Open source ${ref}`}
                >
                  {children}
                </button>
              );
            }
            return href ? (
              <a href={href} target="_blank" rel="noopener noreferrer nofollow" className="text-seal underline">
                {children}
              </a>
            ) : (
              <span>{children}</span>
            );
          },
          img: () => null, // no remote images from model output
        }}
      >
        {linkCitations(text, citationCount)}
      </ReactMarkdown>
    </div>
  );
}
