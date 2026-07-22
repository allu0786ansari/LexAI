import { useRef, useState } from "react";
import { ArrowUp } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export function InputBox({ onSend, disabled }) {
  const [value, setValue] = useState("");
  const textareaRef = useRef(null);

  const handleSubmit = () => {
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setValue("");
    if (textareaRef.current) textareaRef.current.style.height = "auto";
  };

  const handleKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const handleInput = (e) => {
    setValue(e.target.value);
    const el = textareaRef.current;
    if (el) {
      el.style.height = "auto";
      el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
    }
  };

  return (
    <div className="flex items-end gap-2 rounded-lg border border-ink-3 bg-ink-2 p-2 focus-within:border-brass/50 transition-colors">
      <textarea
        ref={textareaRef}
        value={value}
        onChange={handleInput}
        onKeyDown={handleKeyDown}
        disabled={disabled}
        rows={1}
        placeholder="Ask about the Constitution, IPC, CrPC, or the IT Act…"
        className={cn(
          "flex-1 resize-none bg-transparent px-2 py-2 text-sm text-text-ink placeholder:text-muted-ink",
          "focus:outline-none disabled:opacity-50"
        )}
      />
      <Button type="button" size="icon" onClick={handleSubmit} disabled={disabled || !value.trim()} aria-label="Send question">
        <ArrowUp className="h-4 w-4" />
      </Button>
    </div>
  );
}
