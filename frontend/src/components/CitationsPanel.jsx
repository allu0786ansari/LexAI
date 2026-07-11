import { useState } from "react";
import { ChevronDown, ScrollText } from "lucide-react";
import { Collapsible, CollapsibleTrigger, CollapsibleContent } from "@/components/ui/collapsible";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/**
 * Renders retrieved-chunk citations as a collapsible list of "index
 * cards" — the one place the ledger/gazette motif shows up beyond the
 * margin rule, since citations genuinely are reference-card-shaped data
 * (source, page, section, a relevance score).
 */
export function CitationsPanel({ citations }) {
  const [open, setOpen] = useState(false);
  if (!citations || citations.length === 0) return null;

  return (
    <Collapsible open={open} onOpenChange={setOpen} className="mt-3">
      <CollapsibleTrigger
        className={cn(
          "flex items-center gap-1.5 text-xs font-mono uppercase tracking-wide text-muted-paper",
          "hover:text-text-paper transition-colors"
        )}
      >
        <ScrollText className="h-3.5 w-3.5" />
        {citations.length} source{citations.length !== 1 ? "s" : ""}
        <ChevronDown className={cn("h-3.5 w-3.5 transition-transform", open && "rotate-180")} />
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-2 grid gap-2 sm:grid-cols-2">
        {citations.map((c, i) => (
          <div
            key={`${c.source_file}-${c.page}-${i}`}
            className="rounded-md border border-dashed border-muted-paper/30 bg-paper-2/60 p-2.5 text-xs"
          >
            <div className="flex items-start justify-between gap-2">
              <span className="font-medium text-text-paper break-all">{c.source_file}</span>
              {c.page != null && <span className="shrink-0 font-mono text-muted-paper">p.{c.page}</span>}
            </div>
            <div className="mt-1.5 flex flex-wrap items-center gap-1">
              <Badge variant="muted">{c.law_type}</Badge>
              {(c.sections ?? []).slice(0, 3).map((s) => (
                <Badge key={s} variant="brass">
                  {s}
                </Badge>
              ))}
            </div>
          </div>
        ))}
      </CollapsibleContent>
    </Collapsible>
  );
}
