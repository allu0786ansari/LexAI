import { expect, test } from "@playwright/test";
import { citations, mockApi, sse } from "./mock-api";

const ASK = "What is the punishment for murder?";

async function ask(page: import("@playwright/test").Page, question = ASK) {
  await page.getByLabel("Your question about Indian law").fill(question);
  await page.getByLabel("Your question about Indian law").press("Enter");
}

test("an answer streams with its sources, and every citation opens its passage", async ({ page }) => {
  const calls = await mockApi(page);
  await page.goto("/");
  await ask(page);
  const answer = page.locator("article").last();
  await expect(answer).toContainText("imprisonment for life");
  await expect(answer.getByText("All provisions checked against the sources")).toBeVisible();
  expect(calls.sessions).toBe(1);
  expect(calls.chat[0]).toMatchObject({ question: ASK, session_id: expect.any(String) });

  for (const c of citations) {
    await answer.getByRole("button", { name: `Open source ${c.ref}` }).click();
    const panel = page.getByRole("dialog");
    await expect(panel.getByRole("heading")).toContainText(c.label);
    await expect(panel).toContainText(`${c.label} full text`);
    await expect(panel.getByRole("link", { name: /Open the PDF at page/ })).toHaveAttribute("href", `/api/v1/documents/${c.doc_id}/pdf#page=${c.page}`);
    await page.keyboard.press("Escape");
    await expect(panel).toBeHidden();
  }
  // The source list under the answer opens the same panel.
  await answer.getByRole("region", { name: "Sources" }).getByRole("button", { name: /IPC s.302/ }).click();
  await expect(page.getByRole("dialog")).toContainText("repealed 2024-07-01");
});

test("stop cancels a running answer", async ({ page }) => {
  await mockApi(page, () => new Promise(() => {})); // never answers
  await page.goto("/");
  await ask(page);
  await page.getByRole("button", { name: "Stop" }).click();
  await expect(page.getByText("Stopped.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Ask" })).toBeVisible();
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

test("an expired session is replaced and the question asked again", async ({ page }) => {
  const calls = await mockApi(page, (route, n) =>
    n === 1
      ? route.fulfill({ status: 404, json: { code: "session_not_found", message: "Unknown or expired session.", request_id: "r" } })
      : route.fulfill({ status: 200, headers: { "Content-Type": "text/event-stream" }, body: sse("Answer after a new session [1].") }),
  );
  await page.goto("/");
  await ask(page);
  await expect(page.locator("article").last()).toContainText("Answer after a new session");
  await expect(page.getByRole("status")).toContainText("session expired");
  expect(calls.sessions).toBe(2);
});

test("a rate limit shows when to try again", async ({ page }) => {
  await mockApi(page, (route) =>
    route.fulfill({ status: 429, headers: { "Retry-After": "12" }, json: { code: "rate_limited", message: "Too many", request_id: "r" } }),
  );
  await page.goto("/");
  await ask(page);
  await expect(page.getByRole("alert")).toContainText("Try again in 12 s");
});

test("conversations survive a reload; feedback is sent once", async ({ page }) => {
  const calls = await mockApi(page);
  await page.goto("/");
  await ask(page);
  const answer = page.locator("article").last();
  await expect(answer).toContainText("imprisonment for life");
  await answer.getByRole("button", { name: "Good answer" }).click();
  await expect(answer.getByRole("button", { name: "Good answer" })).toHaveAttribute("aria-pressed", "true");
  expect(calls.feedback).toEqual([{ request_id: "req-0001-abcdef", rating: "up" }]);
  await page.reload();
  await expect(page.locator("article").last()).toContainText("imprisonment for life");
  await expect(page.getByRole("button", { name: "Bad answer" })).toBeDisabled();
});

test("model output cannot inject HTML or script links", async ({ page }) => {
  await mockApi(page, (route) =>
    route.fulfill({
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
      body: sse('Text <img src=x onerror="window.pwned=1"> and [a link](javascript:alert(1)) and **bold** [1].'),
    }),
  );
  await page.goto("/");
  await ask(page);
  const answer = page.locator("article").last();
  await expect(answer.locator("strong")).toHaveText("bold");
  await expect(answer.locator("img")).toHaveCount(0);
  await expect(answer.locator('a[href^="javascript"]')).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { pwned?: number }).pwned)).toBeUndefined();
});

test("filters are sent with the question", async ({ page }) => {
  const calls = await mockApi(page);
  await page.goto("/");
  await page.getByRole("button", { name: "Filters" }).click();
  await page.getByLabel(/In force only/).check();
  await page.getByLabel("BNS").check();
  await page.keyboard.press("Escape");
  await ask(page);
  await expect(page.locator("article").last()).toContainText("imprisonment");
  expect(calls.chat[0]).toMatchObject({ filters: { in_force_only: true, acts: ["BNS"] } });
});

test("verification flags are shown on the answer", async ({ page }) => {
  await mockApi(page, (route) =>
    route.fulfill({
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
      body: sse("Murder [1]. Also IPC s.304B.", { flags: [{ kind: "unsupported_provision", detail: "IPC s.304B is not in the passages" }] }),
    }),
  );
  await page.goto("/");
  await ask(page);
  await expect(page.getByRole("note")).toContainText("IPC s.304B is not in the passages");
});

test("on a phone, the drawer holds the history and New chat", async ({ page, isMobile }) => {
  test.skip(!isMobile, "mobile layout only");
  await mockApi(page);
  await page.goto("/");
  await ask(page);
  await expect(page.locator("article").last()).toContainText("imprisonment");
  const nav = page.getByRole("navigation", { name: "Conversations" });
  await expect(nav).not.toBeInViewport();
  await page.getByRole("button", { name: "Open conversations" }).click();
  await expect(nav).toBeInViewport();
  await expect(nav.getByRole("button", { name: ASK, exact: true })).toBeVisible();
  await expect(nav.getByRole("button", { name: `Delete conversation: ${ASK}` })).toBeVisible(); // no hover on touch
  await nav.getByRole("button", { name: "New chat" }).click();
  await expect(nav).not.toBeInViewport();
  await expect(page.getByRole("heading", { name: "Ask about Indian law." })).toBeVisible();
});
