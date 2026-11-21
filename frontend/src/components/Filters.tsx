import { useEffect, useRef, useState } from "react";
import { SlidersHorizontal } from "lucide-react";
import { api } from "@/api/client";
import type { ChatFilters } from "@/api/types";
import { type ActOption, acts } from "@/lib/acts";
import { cn } from "@/lib/cn";

interface Props {
  value: ChatFilters;
  onChange: (value: ChatFilters) => void;
}

/** Restrict the search to some acts and/or the law in force. Named provisions are still looked up. */
export function Filters({ value, onChange }: Props) {
  const [open, setOpen] = useState(false);
  const [options, setOptions] = useState<ActOption[]>([]);
  const box = useRef<HTMLDivElement>(null);
  const selected = value.acts ?? [];
  const active = selected.length > 0 || value.in_force_only;

  useEffect(() => {
    if (!open || options.length) return;
    api.documents().then((d) => setOptions(acts(d)), () => setOptions([]));
  }, [open, options.length]);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent ? e.key === "Escape" : !box.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [open]);

  const toggle = (name: string) =>
    onChange({ ...value, acts: selected.includes(name) ? selected.filter((a) => a !== name) : [...selected, name] });

  return (
    <div className="relative" ref={box}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-controls="filters-panel"
        className={cn(
          "flex items-center gap-1.5 rounded-lg px-2 py-1 text-xs text-muted-paper hover:bg-paper-2 hover:text-text-paper",
          active && "bg-seal/10 text-seal",
        )}
      >
        <SlidersHorizontal className="size-3.5" aria-hidden />
        {active
          ? [value.in_force_only ? "In force only" : "", selected.length ? selected.join(", ") : ""].filter(Boolean).join(" · ")
          : "Filters"}
      </button>
      {open && (
        <div
          id="filters-panel"
          role="group"
          aria-label="Search filters"
          className="absolute bottom-full left-0 z-30 mb-2 w-72 rounded-xl border border-paper-2 bg-paper p-3 text-sm text-text-paper shadow-xl"
        >
          <label className="flex items-start gap-2">
            <input
              type="checkbox"
              checked={value.in_force_only ?? false}
              onChange={(e) => onChange({ ...value, in_force_only: e.target.checked })}
              className="mt-1 accent-seal"
            />
            <span>
              In force only
              <span className="block text-xs text-muted-paper">Skip the repealed IPC, CrPC and IEA</span>
            </span>
          </label>
          <p className="mt-3 mb-1 text-[0.7rem] font-semibold uppercase tracking-wider text-muted-paper">Acts</p>
          {options.length === 0 && <p className="text-xs text-muted-paper">Loading…</p>}
          <ul className="scrollbar-thin max-h-56 space-y-1 overflow-y-auto">
            {options.map((o) => (
              <li key={o.shortName}>
                <label className="flex items-start gap-2" title={o.title}>
                  <input type="checkbox" checked={selected.includes(o.shortName)} onChange={() => toggle(o.shortName)} className="mt-0.5 accent-seal" />
                  <span>
                    {o.shortName}
                    {o.repealed && <span className="ml-1 text-xs text-muted-paper">(repealed)</span>}
                  </span>
                </label>
              </li>
            ))}
          </ul>
          {active && (
            <button type="button" onClick={() => onChange({ acts: [], in_force_only: false })} className="mt-3 text-xs text-seal hover:underline">
              Clear filters
            </button>
          )}
        </div>
      )}
    </div>
  );
}
