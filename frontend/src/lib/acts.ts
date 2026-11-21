import type { DocumentInfo } from "@/api/types";

export interface ActOption {
  shortName: string;
  title: string;
  repealed: boolean;
}

/** Filterable acts from the corpus list: statutes and the Constitution, in force first. */
export function acts(documents: DocumentInfo[]): ActOption[] {
  const seen = new Map<string, ActOption>();
  for (const d of documents) {
    if (!d.short_name || d.doc_type === "judgment" || d.doc_type === "manual" || seen.has(d.short_name)) continue;
    seen.set(d.short_name, { shortName: d.short_name, title: d.title, repealed: Boolean(d.repealed_on) });
  }
  return [...seen.values()].sort((a, b) => Number(a.repealed) - Number(b.repealed) || a.shortName.localeCompare(b.shortName));
}

