import { useState } from "react";
import { Check, Copy, RotateCcw, Scale, ThumbsDown, ThumbsUp } from "lucide-react";
import type { Citation } from "@/api/types";
import { cn } from "@/lib/cn";
import type { AssistantMessage, Message } from "@/state/chats";
import { AnswerMarkdown } from "./AnswerMarkdown";
import { VerificationBadge } from "./VerificationBadge";

interface Props {
  message: Message;
  isLast: boolean;
  busy: boolean;
  onCite: (citation: Citation) => void;
  onRegenerate: (answerId: string) => void;
  onFeedback: (answer: AssistantMessage, rating: "up" | "down") => void;
}

export function MessageView({ message, isLast, busy, onCite, onRegenerate, onFeedback }: Props) {
  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <p className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-sm bg-ink-2 px-4 py-2.5 text-sm text-text-ink">
          {message.content}
        </p>
      </div>
    );
  }
  return <Answer message={message} isLast={isLast} busy={busy} onCite={onCite} onRegenerate={onRegenerate} onFeedback={onFeedback} />;
}

function Answer({ message, isLast, busy, onCite, onRegenerate, onFeedback }: Props & { message: AssistantMessage }) {
  const [copied, setCopied] = useState(false);
  const streaming = message.status === "streaming";
  const byRef = new Map(message.citations.map((c) => [c.ref, c]));
  const cite = (ref: number) => {
    const c = byRef.get(ref);
    if (c) onCite(c);
  };

  const copy = async () => {
    await navigator.clipboard.writeText(message.content);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <article className="relative rounded-xl border border-paper-2 bg-paper px-5 py-4 text-text-paper shadow-sm" aria-busy={streaming}>
      <header className="mb-1 flex items-center gap-2 text-xs text-muted-paper">
        <Scale className="size-3.5 text-seal" aria-hidden />
        <span className="font-medium text-text-paper">LexAI</span>
        {message.meta?.applicable_code && message.status !== "error" && (
          <span title={message.meta.applicable_code_reason}>
            · {message.meta.applicable_code === "old" ? "IPC / CrPC / IEA apply" : "BNS / BNSS / BSA apply"}
          </span>
        )}
        {message.cached && <span>· from cache</span>}
      </header>

      {/* Screen readers hear the answer as it streams. */}
      <div aria-live="polite" aria-atomic="false">
        {message.content ? (
          <AnswerMarkdown text={message.content} citationCount={message.citations.length} onCite={cite} />
        ) : streaming ? (
          <p className="animate-pulse text-sm text-muted-paper">Searching the sources…</p>
        ) : null}
      </div>

      {message.status === "stopped" && <p className="mt-2 text-xs text-muted-paper">Stopped.</p>}
      {message.status === "error" && (
        <p className="mt-2 text-sm text-seal" role="alert">
          {message.error ?? "The answer failed."}
          {message.requestId ? <span className="ml-1 font-mono text-xs text-muted-paper">(request {message.requestId.slice(0, 8)})</span> : null}
        </p>
      )}

      {message.citations.length > 0 && (
        <section aria-label="Sources" className="mt-3 border-t border-paper-2 pt-3">
          <h3 className="mb-1.5 text-[0.7rem] font-semibold uppercase tracking-wider text-muted-paper">Sources</h3>
          <ol className="flex flex-wrap gap-1.5">
            {message.citations.map((c) => (
              <li key={`${c.ref}-${c.chunk_id}`}>
                <button
                  type="button"
                  onClick={() => onCite(c)}
                  className={cn(
                    "flex max-w-[16rem] items-center gap-1.5 rounded-md border border-paper-2 bg-paper-2/40 px-2 py-1 text-left text-xs hover:border-seal/40 hover:bg-paper-2",
                    c.status.startsWith("repealed") && "text-muted-paper",
                  )}
                  title={c.unit_title ?? c.doc_title}
                >
                  <span className="font-mono text-seal">[{c.ref}]</span>
                  <span className="truncate">{c.label}</span>
                </button>
              </li>
            ))}
          </ol>
        </section>
      )}

      <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
        <VerificationBadge message={message} />
        {!streaming && (
          <div className="ml-auto flex items-center gap-0.5">
            <IconButton label={copied ? "Copied" : "Copy answer"} onClick={copy} disabled={!message.content}>
              {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
            </IconButton>
            {isLast && (
              <IconButton label="Regenerate answer" onClick={() => onRegenerate(message.id)} disabled={busy}>
                <RotateCcw className="size-4" />
              </IconButton>
            )}
            {message.status === "done" && message.requestId && (
              <>
                <IconButton
                  label="Good answer"
                  pressed={message.feedback === "up"}
                  onClick={() => onFeedback(message, "up")}
                  disabled={message.feedback !== undefined}
                >
                  <ThumbsUp className="size-4" />
                </IconButton>
                <IconButton
                  label="Bad answer"
                  pressed={message.feedback === "down"}
                  onClick={() => onFeedback(message, "down")}
                  disabled={message.feedback !== undefined}
                >
                  <ThumbsDown className="size-4" />
                </IconButton>
              </>
            )}
          </div>
        )}
      </div>
    </article>
  );
}

function IconButton({
  label,
  onClick,
  disabled,
  pressed,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  pressed?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      aria-pressed={pressed}
      title={label}
      className={cn(
        "rounded-md p-1.5 text-muted-paper transition-colors hover:bg-paper-2 hover:text-text-paper disabled:cursor-not-allowed disabled:opacity-40",
        pressed && "bg-paper-2 text-seal disabled:opacity-100",
      )}
    >
      {children}
    </button>
  );
}
