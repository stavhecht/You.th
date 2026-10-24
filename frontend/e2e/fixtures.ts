/**
 * Shared fixtures for the browser tests.
 *
 * Every test fails on an uncaught exception or a console error, not only on the
 * assertion it makes: a page can render the element under test while a crashed
 * component elsewhere leaves it unusable. "Failed to load resource" is allowed —
 * the browser logs one for every deliberate 4xx (a missing article, a wrong
 * password), and the tests assert those responses directly.
 */
import { expect, test as base, type Page } from "@playwright/test";

// Mirrors backend/tests/e2e/browser_seed.py.
export const REVIEWER = { email: "reviewer@e2e.test", password: "Reviewer-password-1" };
export const PUBLISHED = {
  creatine: "Creatine reliably adds strength",
  foam: "Foam rolling before exercise",
  cold: "Cold showers and immunity",
};
export const DRAFTS = {
  magnesium: "Does Magnesium glycinate help you sleep?",
  ashwagandha: "Does Ashwagandha root extract help you sleep?",
  rhodiola: "Does Rhodiola rosea help you sleep?",
};
export const READER_PASSWORD = "Reader-password-1";

export const test = base.extend<{ page: Page }>({
  page: async ({ page }, use) => {
    const problems: string[] = [];
    page.on("pageerror", (error) => problems.push(`uncaught: ${error.message}`));
    page.on("console", (message) => {
      if (message.type() === "error" && !message.text().startsWith("Failed to load resource")) {
        problems.push(`console: ${message.text()}`);
      }
    });
    await use(page);
    expect(problems, "the page logged errors").toEqual([]);
  },
});

export { expect };

/** A unique address per test, so reruns and retries never collide. */
export function freshEmail(label: string): string {
  return `${label}-${Date.now()}-${Math.floor(Math.random() * 1e6)}@e2e.test`;
}

export async function signUp(page: Page, email: string, name = "Ada"): Promise<void> {
  await page.goto("/join");
  await page.locator('input[name="displayName"]').fill(name);
  await page.locator('input[name="email"]').fill(email);
  await page.locator('input[name="password"]').fill(READER_PASSWORD);
  await page.locator('input[name="confirmPassword"]').fill(READER_PASSWORD);
  await page.getByRole("button", { name: "Build my feed →" }).click();
  await expect(page).toHaveURL("/");
}
