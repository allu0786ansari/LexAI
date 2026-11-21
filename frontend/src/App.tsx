import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { Menu, MessageSquarePlus } from "lucide-react";
import { ApiError, api, describeError } from "@/api/client";
import type { ChatFilters, Citation } from "@/api/types";
import { Composer } from "@/components/Composer";
import { Filters } from "@/components/Filters";
import { MessageView } from "@/components/MessageView";
import { Sidebar } from "@/components/Sidebar";
import { SourcePanel } from "@/components/SourcePanel";
import { type AssistantMessage, load, newId, reducer, save } from "@/state/chats";

const EXAMPLES = [
  "What is the punishment for murder under the BNS?",
  "Which BNSS section replaced section 438 CrPC (anticipatory bail)?",
  "What does Article 21 of the Constitution protect?",
  "Is an electronic record admissible without a certificate?",
];
const FILTERS_KEY = "lexai.filters.v1";

function loadFilters(): ChatFilters {
  try {
    const saved = JSON.parse(localStorage.getItem(FILTERS_KEY) ?? "{}") as Partial<ChatFilters>;
    return { acts: saved.acts ?? [], in_force_only: saved.in_force_only ?? false };
  } catch {
    return { acts: [], in_force_only: false };
  }
}

export default function App() {
  const [state, dispatch] = useReducer(reducer, undefined, () => load());
  const [drawer, setDrawer] = useState(false);
  const [source, setSource] = useState<Citation | null>(null);
  const [filters, setFilters] = useState<ChatFilters>(loadFilters);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const stateRef = useRef(state);
  stateRef.current = state;

  const chat = state.chats.find((c) => c.id === state.activeId) ?? state.chats[0];

  // Saved when an answer finishes, stops or fails, or a chat is added/removed — not per token (B12).
  useEffect(() => save(stateRef.current), [state.persist]);
  useEffect(() => {
    try {
      localStorage.setItem(FILTERS_KEY, JSON.stringify(filters));
    } catch {
      // storage unavailable
    }
  }, [filters]);

  // Follow the stream unless the reader scrolled up.
  useEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  });

  const run = useCallback(
    async (chatId: string, question: string, answerId: string) => {
      const controller = new AbortController();
      abort.current = controller;
      setBusy(true);
      setNotice(null);
      stick.current = true;
      let finished = false;
      let sessionId = stateRef.current.chats.find((c) => c.id === chatId)?.sessionId ?? null;
      const active = (filters.acts?.length ?? 0) > 0 || filters.in_force_only;
      try {
        for (let attempt = 0; ; attempt++) {
          try {
            if (!sessionId) {
              sessionId = (await api.createSession()).session_id;
              dispatch({ type: "session", chatId, sessionId });
            }
            await api.chat(
              { question, session_id: sessionId, filters: active ? filters : null },
              (event) => {
                if (event.event === "done" || event.event === "error") finished = true;
                dispatch({ type: "event", chatId, answerId, event });
              },
              controller.signal,
            );
            break;
          } catch (err) {
            // Sessions expire after 30 idle minutes: start a new one once and ask again.
            if (err instanceof ApiError && err.code === "session_not_found" && attempt === 0) {
              sessionId = null;
              setNotice("The previous session expired; the question was asked in a new one.");
              continue;
            }
            throw err;
          }
        }
        if (!finished) dispatch({ type: "failed", chatId, answerId, error: "The answer was cut off. Try again." });
      } catch (err) {
        if (controller.signal.aborted) dispatch({ type: "stopped", chatId, answerId });
        else dispatch({ type: "failed", chatId, answerId, error: describeError(err) });
      } finally {
        abort.current = null;
        setBusy(false);
      }
    },
    [filters],
  );

  const ask = (question: string) => {
    if (!chat || busy) return;
    const answerId = newId();
    dispatch({ type: "ask", chatId: chat.id, question, userId: newId(), answerId });
    void run(chat.id, question, answerId);
  };

  const regenerate = (answerId: string) => {
    const m = chat?.messages.find((x) => x.id === answerId);
    if (!chat || busy || !m || m.role !== "assistant") return;
    dispatch({ type: "regenerate", chatId: chat.id, answerId });
    void run(chat.id, m.question, answerId);
  };

  const feedback = async (answer: AssistantMessage, rating: "up" | "down") => {
    if (!chat || !answer.requestId) return;
    try {
      await api.feedback({ request_id: answer.requestId, rating });
      dispatch({ type: "feedback", chatId: chat.id, answerId: answer.id, rating });
    } catch (err) {
      setNotice(`Feedback not sent: ${describeError(err)}`);
    }
  };

  const stop = () => abort.current?.abort();
  const closeSource = useCallback(() => setSource(null), []);
  const closeDrawer = useCallback(() => setDrawer(false), []);
  const newChat = () => {
    stop();
    dispatch({ type: "new" });
    setSource(null);
    setDrawer(false);
  };

  return (
    <div className="flex h-dvh overflow-hidden">
      <Sidebar
        chats={state.chats}
        activeId={chat?.id ?? null}
        open={drawer}
        onClose={closeDrawer}
        onNew={newChat}
        onSelect={(id) => {
          if (busy) stop();
          dispatch({ type: "select", chatId: id });
          setSource(null);
          setDrawer(false);
        }}
        onDelete={(id) => {
          if (busy && id === chat?.id) stop();
          dispatch({ type: "delete", chatId: id });
        }}
      />
      <main className="ruled-margin flex min-w-0 flex-1 flex-col bg-paper-2">
        <header className="flex items-center gap-2 border-b border-paper-2 bg-paper px-3 py-2 md:hidden">
          <button type="button" onClick={() => setDrawer(true)} className="rounded p-1.5 text-text-paper hover:bg-paper-2" aria-label="Open conversations">
            <Menu className="size-5" />
          </button>
          <span className="truncate font-display text-lg text-text-paper">{chat?.title ?? "LexAI"}</span>
          <button type="button" onClick={newChat} className="ml-auto rounded p-1.5 text-text-paper hover:bg-paper-2" aria-label="New chat">
            <MessageSquarePlus className="size-5" />
          </button>
        </header>

        <div
          ref={scroller}
          onScroll={(e) => {
            const el = e.currentTarget;
            stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
          }}
          className="scrollbar-thin flex-1 overflow-y-auto"
        >
          <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
            {chat && chat.messages.length === 0 ? (
              <section className="mt-[10vh] text-text-paper">
                <h1 className="font-display text-4xl tracking-tight">Ask about Indian law.</h1>
                <p className="mt-2 max-w-xl text-sm text-muted-paper">
                  Answers come from the Constitution, the criminal codes old and new (IPC/BNS, CrPC/BNSS, IEA/BSA), related Acts and
                  Supreme Court judgments — and every statement cites the passage it rests on.
                </p>
                <ul className="mt-6 grid gap-2 sm:grid-cols-2">
                  {EXAMPLES.map((q) => (
                    <li key={q}>
                      <button
                        type="button"
                        onClick={() => ask(q)}
                        className="h-full w-full rounded-xl border border-paper-2 bg-paper px-4 py-3 text-left text-sm hover:border-seal/40"
                      >
                        {q}
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            ) : (
              chat?.messages.map((m, i) => (
                <MessageView
                  key={m.id}
                  message={m}
                  isLast={i === chat.messages.length - 1}
                  busy={busy}
                  onCite={setSource}
                  onRegenerate={regenerate}
                  onFeedback={feedback}
                />
              ))
            )}
          </div>
        </div>

        <div className="mx-auto w-full max-w-3xl px-4 pb-4">
          {notice && (
            <p className="mb-2 rounded-lg bg-paper px-3 py-1.5 text-xs text-muted-paper" role="status">
              {notice}
            </p>
          )}
          <Composer busy={busy} onSend={ask} onStop={stop} filters={<Filters value={filters} onChange={setFilters} />} />
        </div>
      </main>
      {source && <SourcePanel citation={source} onClose={closeSource} />}
    </div>
  );
}
