import { describe, expect, it } from "vitest";
import { citedRef, linkCitations, safeUrl } from "./citations";

describe("citations", () => {
  it("links markers that point to a passage, and only those", () => {
    expect(linkCitations("Death or life [1][2]. See [9].", 2)).toBe(
      "Death or life [\\[1\\]](#cite-1)[\\[2\\]](#cite-2). See [9].",
    );
  });

  it("leaves inline code alone", () => {
    expect(linkCitations("use `a[1]` then [1]", 1)).toBe("use `a[1]` then [\\[1\\]](#cite-1)");
  });

  it("allows http(s) and citation anchors only", () => {
    expect(safeUrl("https://indiacode.nic.in/x")).toBe("https://indiacode.nic.in/x");
    expect(safeUrl("#cite-3")).toBe("#cite-3");
    expect(safeUrl("javascript:alert(1)")).toBe("");
    expect(safeUrl("data:text/html,<script>")).toBe("");
    expect(safeUrl("#cite-x")).toBe("");
    expect(safeUrl("/relative")).toBe("");
  });

  it("reads the cited reference", () => {
    expect(citedRef("#cite-12")).toBe(12);
    expect(citedRef("https://x")).toBeNull();
    expect(citedRef(undefined)).toBeNull();
  });
});
