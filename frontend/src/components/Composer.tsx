import { useRef, useState } from "react";
import { ArrowUp, Square } from "lucide-react";
import type { ReactNode } from "react";

interface Props {
  busy: boolean;
  onSend: (question: string) => void;
  onStop: () => void;
  filters: ReactNode;
}

const MAX = 2000;

export function Composer({ busy, onSend, onStop, filters }: Props) {
  const [text, setText] = useState("");
  const area = useRef<HTMLTextAreaElement>(null);

  const send = () => {
    const q = text.trim();
    if (!q || busy) return;
    onSend(q);
    setText("");
    area.current?.focus();
  };

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        send();
      }}
      className="rounded-2xl border border-paper-2 bg-paper p-2 shadow-lg"
    >
      <label htmlFor="question" className="sr-only">
        Your question about Indian law
      </label>
      <textarea
        id="question"
        ref={area}
        value={text}
        maxLength={MAX}
        rows={2}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            send();
          }
        }}
        placeholder="Ask about a section, an offence or a judgment — e.g. “What replaced section 420 IPC?”"
        className="block max-h-48 min-h-[3rem] w-full resize-y bg-transparent px-2 py-1.5 text-sm text-text-paper placeholder:text-muted-paper focus:outline-none"
      />
      <div className="flex items-center gap-2 px-1 pt-1">
        {filters}
        <span className="ml-auto text-[0.7rem] text-muted-paper" aria-live="polite">
          {text.length > MAX - 200 ? `${MAX - text.length} characters left` : ""}
        </span>
        {busy ? (
          <button
            type="button"
            onClick={onStop}
            className="flex items-center gap-1.5 rounded-lg bg-seal px-3 py-1.5 text-sm font-medium text-paper hover:bg-seal-soft"
          >
            <Square className="size-3.5 fill-current" aria-hidden /> Stop
          </button>
        ) : (
          <button
            type="submit"
            disabled={!text.trim()}
            className="flex items-center gap-1.5 rounded-lg bg-ink px-3 py-1.5 text-sm font-medium text-text-ink hover:bg-ink-2 disabled:opacity-40"
          >
            Ask <ArrowUp className="size-3.5" aria-hidden />
          </button>
        )}
      </div>
    </form>
  );
}
