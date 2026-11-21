import { expect, test } from "@playwright/test";
import { mockApi } from "./mock-api";

// Runs everywhere; meaningful against nginx (PW_BASE_URL), where the production CSP applies.
test("the app runs without Content-Security-Policy violations", async ({ page }) => {
  const violations: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error" && /Content Security Policy|Refused to/i.test(m.text())) violations.push(m.text());
  });
  await mockApi(page);
  await page.goto("/");
  await page.getByLabel("Your question about Indian law").fill("What is the punishment for murder?");
  await page.keyboard.press("Enter");
  await expect(page.locator("article").last()).toContainText("imprisonment for life");
  await page.getByRole("button", { name: "Open source 1" }).click();
  await expect(page.getByRole("dialog")).toContainText("full text");
  expect(violations).toEqual([]);
});
