import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Scale, AlertTriangle } from "lucide-react";
import { Card } from "@/components/ui/card";
import { CitationsPanel } from "@/components/CitationsPanel";
import { cn } from "@/lib/utils";

function ThinkingIndicator() {
  return (
    <div className="flex items-center gap-1.5 py-1 text-muted-paper" aria-label="LexAI is researching">
      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-seal [animation-delay:-0.3s]" />
      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-seal [animation-delay:-0.15s]" />
      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-seal" />
    </div>
  );
}

function AssistantMessage({ message }) {
  const showThinking = message.isStreaming && !message.content;
  return (
    <div className="flex gap-3">
      <div className="mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-seal/15 text-seal">
        <Scale className="h-3.5 w-3.5" />
      </div>
      <Card className="max-w-[75ch] flex-1 px-4 py-3">
        {message.error ? (
          <div className="flex items-center gap-2 text-sm text-seal">
            <AlertTriangle className="h-4 w-4 shrink-0" />
            {message.error}
          </div>
        ) : showThinking ? (
          <ThinkingIndicator />
        ) : (
          <div className="prose prose-sm max-w-none prose-headings:font-display prose-headings:text-text-paper prose-p:text-text-paper prose-strong:text-text-paper prose-li:text-text-paper">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
          </div>
        )}
        {!message.error && <CitationsPanel citations={message.citations} />}
      </Card>
    </div>
  );
}

function UserMessage({ message }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[65ch] rounded-lg bg-ink-2 px-4 py-2.5 text-sm text-text-ink">{message.content}</div>
    </div>
  );
}

export function MessageList({ messages, bottomRef }) {
  if (messages.length === 0) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 text-center text-muted-ink">
        <Scale className="h-8 w-8 text-brass" />
        <div>
          <p className="font-display text-lg text-text-ink">LexAI Research Desk</p>
          <p className="mt-1 max-w-sm text-sm">
            Ask a question about Indian statute, procedure, or precedent — every answer is grounded in the retrieved
            source, cited below it.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-1 flex-col gap-5 overflow-y-auto scrollbar-thin px-1 py-4">
      {messages.map((m) => (
        <div key={m.id} className={cn(m.role === "user" ? "" : "")}>
          {m.role === "user" ? <UserMessage message={m} /> : <AssistantMessage message={m} />}
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
