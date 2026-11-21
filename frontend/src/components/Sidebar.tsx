import { useEffect, useRef } from "react";
import { MessageSquarePlus, Scale, Trash2, X } from "lucide-react";
import { cn } from "@/lib/cn";
import type { Chat } from "@/state/chats";

interface Props {
  chats: Chat[];
  activeId: string | null;
  open: boolean;
  onClose: () => void;
  onNew: () => void;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
}

/** Chat history and "New chat": a column on wide screens, a drawer on phones (B13). */
export function Sidebar({ chats, activeId, open, onClose, onNew, onSelect, onDelete }: Props) {
  const panel = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!open) return;
    panel.current?.querySelector<HTMLElement>("button")?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const sorted = [...chats].sort((a, b) => b.updatedAt - a.updatedAt);
  return (
    <>
      {open && <div className="fixed inset-0 z-30 bg-black/50 md:hidden" onClick={onClose} aria-hidden />}
      <nav
        ref={panel}
        aria-label="Conversations"
        className={cn(
          "fixed inset-y-0 left-0 z-40 flex w-72 flex-col bg-ink-2 transition-transform md:static md:z-auto md:translate-x-0",
          open ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <div className="flex items-center gap-2 px-4 pb-3 pt-4">
          <Scale className="size-5 text-brass" aria-hidden />
          <span className="font-display text-xl tracking-tight text-text-ink">LexAI</span>
          <button type="button" onClick={onClose} className="ml-auto rounded p-1 text-muted-ink hover:text-text-ink md:hidden" aria-label="Close conversations">
            <X className="size-5" />
          </button>
        </div>
        <div className="px-3">
          <button
            type="button"
            onClick={onNew}
            className="flex w-full items-center gap-2 rounded-lg border border-ink-3 px-3 py-2 text-sm text-text-ink hover:bg-ink-3"
          >
            <MessageSquarePlus className="size-4" aria-hidden /> New chat
          </button>
        </div>
        <ul className="scrollbar-thin mt-3 flex-1 space-y-0.5 overflow-y-auto px-2 pb-3">
          {sorted.map((chat) => (
            <li key={chat.id} className="group relative">
              <button
                type="button"
                onClick={() => onSelect(chat.id)}
                aria-current={chat.id === activeId ? "page" : undefined}
                className={cn(
                  "w-full truncate rounded-md py-2 pl-3 pr-9 text-left text-sm text-muted-ink hover:bg-ink-3 hover:text-text-ink",
                  chat.id === activeId && "bg-ink-3 text-text-ink",
                )}
              >
                {chat.title}
              </button>
              <button
                type="button"
                onClick={() => onDelete(chat.id)}
                aria-label={`Delete conversation: ${chat.title}`}
                className="absolute right-1 top-1/2 -translate-y-1/2 rounded p-1.5 text-muted-ink hover:text-seal-soft focus:opacity-100 md:opacity-0 md:group-hover:opacity-100"
              >
                <Trash2 className="size-3.5" />
              </button>
            </li>
          ))}
        </ul>
        <p className="border-t border-ink-3 px-4 py-3 text-[0.7rem] leading-snug text-muted-ink">
          Legal information from the indexed sources, not legal advice.
        </p>
      </nav>
    </>
  );
}
