import { expect, test } from "@playwright/test";

// Against the real stack only (CI: nginx -> API -> Qdrant + Ollama with a tiny model):
//   PW_BASE_URL=http://localhost:8080 PW_LIVE=1 npx playwright test e2e/live.spec.ts
test.skip(!process.env.PW_LIVE, "needs a running LexAI deployment (PW_LIVE=1)");
test.setTimeout(240_000);

test("a real question is answered from the index through nginx", async ({ page }, testInfo) => {
  // One real generation is enough: in both projects at once, two answers would compete for
  // the one model on a laptop. Layout on phones is covered by the mocked tests.
  test.skip(testInfo.project.name !== "desktop", "runs once, in the desktop project");
  await page.goto("/");
  const input = page.getByLabel("Your question about Indian law");
  await input.fill("What does Article 21 of the Constitution protect?");
  await input.press("Enter");
  const answer = page.locator("article").last();
  await expect(answer).toHaveAttribute("aria-busy", "false", { timeout: 200_000 });
  const alert = answer.getByRole("alert");
  const alertText = (await alert.count()) ? await alert.first().innerText() : "";
  expect(alertText, "the answer shows an error").toBe("");
  expect((await answer.innerText()).trim().length).toBeGreaterThan(20);
  // The passages behind the answer can be opened, with the passage text from the index.
  const sources = answer.getByRole("region", { name: "Sources" }).getByRole("button");
  if ((await sources.count()) > 0) {
    await sources.first().click();
    await expect(page.getByRole("dialog")).toContainText(/Article|Constitution/, { timeout: 15_000 });
  }
});
