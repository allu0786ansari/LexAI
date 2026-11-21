import { CircleAlert, CircleCheck, CircleHelp } from "lucide-react";
import type { AssistantMessage } from "@/state/chats";

const KIND: Record<string, string> = {
  unsupported_provision: "Mentions a provision that is not in its sources",
  invalid_citation: "A citation number pointed to no source and was removed",
  no_citations: "Makes claims without citing a source",
};

export function VerificationBadge({ message }: { message: AssistantMessage }) {
  if (message.status !== "done" || !message.verification) return null;
  if (message.abstained) {
    return (
      <p className="flex items-center gap-1.5 text-xs text-muted-paper">
        <CircleHelp className="size-3.5" aria-hidden /> Not found in the indexed sources
      </p>
    );
  }
  const { valid, coverage } = message.verification;
  const flags = message.verification.flags ?? [];
  if (valid && flags.length === 0) {
    return (
      <p className="flex items-center gap-1.5 text-xs text-emerald-800">
        <CircleCheck className="size-3.5" aria-hidden /> All provisions checked against the sources
        {coverage != null && coverage < 1 ? ` · ${Math.round(coverage * 100)}% of statements cited` : ""}
      </p>
    );
  }
  return (
    <div className="rounded-md border border-amber-300 bg-amber-50 px-2.5 py-1.5 text-xs text-amber-900" role="note">
      <p className="flex items-center gap-1.5 font-medium">
        <CircleAlert className="size-3.5" aria-hidden /> Check this answer against its sources
      </p>
      <ul className="mt-1 list-disc pl-5">
        {flags.map((f, i) => (
          <li key={i}>
            {KIND[f.kind] ?? f.kind}: {f.detail}
          </li>
        ))}
      </ul>
    </div>
  );
}
