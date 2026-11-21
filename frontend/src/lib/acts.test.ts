import { expect, it } from "vitest";
import type { DocumentInfo } from "@/api/types";
import { acts } from "./acts";

const doc = (d: Partial<DocumentInfo>): DocumentInfo =>
  ({ doc_id: d.short_name ?? "x", title: "t", doc_type: "statute", status: "in force", chunks: 1, has_pdf: true, ...d }) as DocumentInfo;

it("lists statutes once each, in force first, without judgments or manuals", () => {
  const out = acts([
    doc({ short_name: "IPC", repealed_on: "2024-07-01" }),
    doc({ short_name: "BNS" }),
    doc({ short_name: "BNS" }),
    doc({ short_name: "Constitution", doc_type: "constitution" }),
    doc({ short_name: "SC", doc_type: "judgment" }),
    doc({ short_name: null }),
  ]);
  expect(out.map((a) => a.shortName)).toEqual(["BNS", "Constitution", "IPC"]);
  expect(out[2]!.repealed).toBe(true);
});
