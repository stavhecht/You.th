/**
 * The review desk in a browser: the human gate between a generated draft and
 * the public feed. The drafts were written by the real pipeline (with the
 * models faked) in browser_seed.py, so they carry sources like a real draft.
 */
import type { Page } from "@playwright/test";

import { DRAFTS, REVIEWER, expect, test } from "./fixtures";

async function signIn(page: Page, password = REVIEWER.password): Promise<void> {
  await page.goto("/review");
  await page.getByLabel("Work email").fill(REVIEWER.email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in →" }).click();
}

test("the desk refuses a wrong password", async ({ page }) => {
  await signIn(page, "Not-the-password-1");
  await expect(page.getByText("Incorrect email or password.")).toBeVisible();
});

test("nothing on the public site links to the desk", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator('a[href^="/review"]')).toHaveCount(0);
});

test("a reviewer publishes a draft and readers can then read it", async ({ page }) => {
  await signIn(page);
  await page.getByRole("link", { name: new RegExp(DRAFTS.magnesium.replace("?", "\\?")) }).click();
  await expect(page).toHaveURL(/\/review\/article\//);

  // Not public before it is approved.
  const publish = page.getByRole("button", { name: "Publish to the public feed" });
  await expect(publish).toBeEnabled();
  await publish.click();
  await expect(page.getByRole("button", { name: "Publishing…" })).toHaveCount(0);

  await page.goto("/");
  const card = page.getByRole("link", { name: DRAFTS.magnesium, exact: true });
  await expect(card).toBeVisible();
  await card.click();
  await expect(page.getByRole("heading", { level: 1, name: DRAFTS.magnesium })).toBeVisible();
});

test("rejecting needs a reason, and a rejected draft never reaches readers", async ({ page }) => {
  await signIn(page);
  await page.getByRole("link", { name: new RegExp(DRAFTS.ashwagandha.replace("?", "\\?")) }).click();

  await page.getByRole("button", { name: "Reject", exact: true }).click();
  await expect(page.getByText("A reason is required before a draft can be rejected.")).toBeVisible();

  await page.getByLabel("Reason for rejecting").fill("Headline overstates the trials.");
  await page.getByRole("button", { name: "Reject", exact: true }).click();

  // Back on the queue: gone from pending, kept (with its reason) under Rejected.
  await expect(page.getByRole("heading", { level: 1, name: "Review queue" })).toBeVisible();
  const draft = page.getByRole("link", { name: new RegExp(DRAFTS.ashwagandha.replace("?", "\\?")) });
  await expect(draft).toHaveCount(0);
  await page.getByRole("button", { name: "Rejected" }).click();
  await expect(draft).toBeVisible();

  await page.goto("/");
  await expect(page.getByRole("link", { name: DRAFTS.ashwagandha, exact: true })).toHaveCount(0);
});

test("an edit from an older copy never overwrites another reviewer's save", async ({
  page,
  request,
}) => {
  const api = "http://localhost:8001/api/console";
  await signIn(page);
  await page.getByRole("link", { name: new RegExp(DRAFTS.rhodiola.replace("?", "\\?")) }).click();
  await expect(page).toHaveURL(/\/review\/article\//);
  const id = page.url().split("/").pop() ?? "";
  const editor = page.locator(".ProseMirror");
  await expect(editor).toBeVisible();

  // Reviewer B saves from their own copy while this page is open.
  const login = await request.post(`${api}/auth/login`, {
    data: { email: REVIEWER.email, password: REVIEWER.password },
  });
  const auth = { Authorization: `Bearer ${(await login.json()).access_token}` };
  const theirs = {
    type: "doc",
    content: [{ type: "paragraph", content: [{ type: "text", text: "Reviewer B's edit." }] }],
  };
  const saved = await request.patch(`${api}/articles/${id}/content`, {
    headers: auth,
    data: { content: theirs, base: null },
  });
  expect(saved.status()).toBe(204);

  // This reviewer types and publishes at once: the flush inside Publish meets
  // the conflict, and nothing is published.
  await editor.click();
  await page.keyboard.type(" My edit.");
  await page.getByRole("button", { name: "Publish to the public feed" }).click();
  await expect(page.getByText("Your edits are not saved.")).toBeVisible();
  await expect(page.getByText("Another reviewer saved this draft.")).toBeVisible();
  const detail = async () =>
    (await request.get(`${api}/articles/${id}`, { headers: auth })).json();
  expect((await detail()).status).toBe("pending_review");

  // Reloading (a subject change refetches the article) shows B's text, and
  // leaving must not save the older copy over it.
  await page.getByRole("button", { name: "Sleep and recovery" }).click();
  await expect(editor).toContainText("Reviewer B's edit.");
  await page.getByRole("button", { name: "← Queue" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Review queue" })).toBeVisible();
  const after = JSON.stringify((await detail()).editedContent);
  expect(after).toContain("Reviewer B's edit.");
  expect(after).not.toContain("My edit.");
});
