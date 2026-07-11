import { Plus, MessageSquareText } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";

/**
 * "Chat history" here means locally-remembered sessions (see
 * api/apiClient.js's note on why this is client-side, not a server-fetched
 * history — the backend's session memory has no listing/replay endpoint,
 * by design, since it's a 30-minute-TTL memory store, not a database).
 */
export function Sidebar({ sessions, activeSessionId, onSelectSession, onNewChat }) {
  return (
    <aside className="flex h-full w-64 shrink-0 flex-col border-r border-ink-3 bg-ink-2 max-md:hidden">
      <div className="border-b border-ink-3 p-3">
        <Button variant="outline" className="w-full justify-start gap-2" onClick={onNewChat}>
          <Plus className="h-4 w-4" />
          New chat
        </Button>
      </div>
      <ScrollArea className="flex-1">
        <div className="flex flex-col gap-0.5 p-2">
          {sessions.length === 0 && (
            <p className="px-2 py-4 text-center text-xs text-muted-ink">Your questions will appear here.</p>
          )}
          {sessions.map((s) => (
            <button
              key={s.sessionId}
              onClick={() => onSelectSession(s.sessionId)}
              className={cn(
                "group flex items-start gap-2 rounded-md px-2.5 py-2 text-left text-sm transition-colors",
                s.sessionId === activeSessionId
                  ? "bg-ink-3 text-text-ink"
                  : "text-muted-ink hover:bg-ink-3/60 hover:text-text-ink"
              )}
            >
              <MessageSquareText
                className={cn(
                  "mt-0.5 h-3.5 w-3.5 shrink-0",
                  s.sessionId === activeSessionId ? "text-seal" : "text-muted-ink group-hover:text-brass"
                )}
              />
              <span className="line-clamp-2 leading-snug">{s.title}</span>
            </button>
          ))}
        </div>
      </ScrollArea>
    </aside>
  );
}
