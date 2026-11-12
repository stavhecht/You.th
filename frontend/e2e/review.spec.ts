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
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in →" }).click();
}

test("the desk refuses a wrong password", async ({ page }) => {
  await signIn(page, "Not-the-password-1");
  await expect(page.getByText("Incorrect email or password.")).toBeVisible();
});

test("a reviewer who forgot their password can ask for a link", async ({ page }) => {
  await page.goto("/review/login");
  await page.getByRole("link", { name: "Forgot your password?" }).click();
  // The sign-in form has a "Work email" field too; fill the reset page's own.
  await expect(page.getByRole("heading", { name: "Forgot your password?" })).toBeVisible();
  await page.getByLabel("Work email").fill(REVIEWER.email);
  await page.getByRole("button", { name: "Email me a link →" }).click();
  await expect(page.getByRole("heading", { name: "Check your email." })).toBeVisible();
});

test("nothing on the public site links to the desk", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator('a[href^="/review"]')).toHaveCount(0);
});

test("the editor shows what the cursor is in, and opening a draft saves nothing", async ({
  page,
  request,
}) => {
  // Two TipTap 3 changes guarded in ArticleEditor.tsx: the toolbar re-renders on
  // every keystroke again, and no extra paragraph is appended on load (which
  // autosave would write back as an edit nobody made).
  const api = "http://localhost:8001/api/console";
  await signIn(page);
  await page.getByRole("link", { name: new RegExp(DRAFTS.ashwagandha.replace("?", "\\?")) }).click();
  const editor = page.locator(".ProseMirror");
  await expect(editor).toBeVisible();
  const id = page.url().split("/").pop() ?? "";

  await page.waitForTimeout(1500); // longer than the autosave debounce
  const login = await request.post(`${api}/auth/login`, {
    data: { email: REVIEWER.email, password: REVIEWER.password },
  });
  const auth = { Authorization: `Bearer ${(await login.json()).access_token}` };
  const detail = await (await request.get(`${api}/articles/${id}`, { headers: auth })).json();
  expect(detail.editedContent).toBeNull();

  const bold = page.getByRole("button", { name: "Bold" });
  await expect(bold).toHaveAttribute("aria-pressed", "false");
  await editor.click();
  await bold.click();
  await expect(bold).toHaveAttribute("aria-pressed", "true");
});

test("the social panel names a failed weekly run and fetches relations on demand", async ({
  page,
}) => {
  const failed = {
    id: "00000000-0000-0000-0000-000000000001",
    label: "social_weekly_crashed",
    kind: "social_weekly",
    status: "failed",
    collectionStatus: {},
    stats: {},
    notes: [],
    error: { message: "worker stopped reporting" },
    createdAt: "2026-11-05T10:00:00Z",
    startedAt: null,
    finishedAt: null,
  };
  const window = {
    start: "2026-10-29T00:00:00Z",
    end: "2026-11-05T00:00:00Z",
    covered: true,
    covered_days: 7,
    uploads: 4,
    creators: 3,
    truncated: false,
  };
  const topic = {
    candidate_id: "00000000-0000-0000-0000-000000000002",
    topic_id: "00000000-0000-0000-0000-000000000003",
    label: "Creatine for sleep",
    question: "Does creatine help sleep?",
    category: "supplements",
    rank: 1,
    status: "proposed",
    metrics: {
      windows: [window, window, window],
      median_views_per_day: null,
      view_velocity: null,
      velocity_posts: 0,
      engagement_rate: null,
      engagement_posts: 0,
    },
    momentum: { status: "steady", reason: "3 creators both weeks" },
    evidence: { status: "not_checked", source: null, counts: null, reason: null, query: null },
    supporting_posts: [],
    limitations: ["popularity is not evidence"],
  };
  let relatedAsked = 0;
  await page.route("**/api/console/collection/runs*", (route) => route.fulfill({ json: [failed] }));
  await page.route("**/api/console/social/result", (route) =>
    route.fulfill({
      json: { run_id: "x", status: "completed", collection: {}, topics: [topic], notes: [] },
    }),
  );
  await page.route("**/api/console/intel/related*", (route) => {
    relatedAsked += 1;
    return route.fulfill({
      json: [
        {
          kind: "product",
          id: "p1",
          label: "Oura Ring 4",
          relation: "named_in_posts",
          matched_by: "named in the same posts",
          own: { status: "confirmed", launch_status: "none" },
        },
      ],
    });
  });

  await signIn(page);
  await expect(page.getByText("The last weekly analysis failed: worker stopped reporting")).toBeVisible();

  await page.getByRole("button", { name: "Social & products" }).click();
  await expect(page.getByText("Does creatine help sleep?")).toBeVisible();
  expect(relatedAsked).toBe(0);

  await page.getByRole("button", { name: "Related", exact: true }).click();
  await expect(page.getByText(/Oura Ring 4/)).toBeVisible();
  expect(relatedAsked).toBe(1);
});

test("a reviewer publishes a draft and readers can then read it", async ({ page }) => {
  await signIn(page);
  await page.getByRole("link", { name: new RegExp(DRAFTS.magnesium.replace("?", "\\?")) }).click();
  await expect(page).toHaveURL(/\/review\/article\//);

  // Not public before it is approved.
  const publish = page.getByRole("button", { name: "Publish to the public feed" });
  await expect(publish).toBeEnabled();
  // Wait for the approve response itself. Waiting for "Publishing…" to be gone
  // passed before the button changed (Publish saves edits first), and the
  // goto below then aborted the request mid-flight: flaky in CI.
  const approved = page.waitForResponse(
    (r) => r.url().endsWith("/approve") && r.request().method() === "POST",
  );
  await publish.click();
  expect((await approved).ok()).toBe(true);

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

test("a reviewer fixes a draft's headline and the model's wording is kept", async ({ page }) => {
  await signIn(page);
  await page.getByRole("link", { name: new RegExp(DRAFTS.rhodiola.replace("?", "\\?")) }).click();

  const headline = page.getByLabel("Headline");
  await expect(headline).toHaveValue(DRAFTS.rhodiola);
  await headline.fill("Rhodiola and sleep: what the trials show");
  await headline.blur();
  await expect(page.getByText(`Edited. The model wrote: “${DRAFTS.rhodiola}”`)).toBeVisible();

  await page.reload();
  await expect(page.getByLabel("Headline")).toHaveValue("Rhodiola and sleep: what the trials show");
});

test("a published article is corrected with a note, then withdrawn", async ({ page }) => {
  // The draft the publish test above put live: written by the pipeline, so it
  // carries the validation report a hand-seeded article does not.
  await signIn(page);
  await page.getByRole("button", { name: "Published" }).click();
  await page.getByRole("link", { name: new RegExp(DRAFTS.magnesium.replace("?", "\\?")) }).click();
  await expect(page).toHaveURL(/\/review\/article\//);

  // Typing is a draft: readers still see the approved text.
  const editor = page.locator(".ProseMirror");
  await editor.click();
  await page.keyboard.type(" A corrected sentence.");
  await page.getByLabel("What the correction changes").fill("Added a missing sentence.");
  await page.getByRole("button", { name: "Publish correction" }).click();
  await expect(page.getByText(/Corrected .*: Added a missing sentence\./)).toBeVisible();

  await page.goto("/");
  await page.getByRole("link", { name: DRAFTS.magnesium, exact: true }).click();
  await expect(page.getByText("A corrected sentence.")).toBeVisible();
  await expect(page.getByLabel("Corrections")).toContainText("Added a missing sentence.");
  const articleUrl = page.url();

  // Withdrawing takes it off the feed and its own page.
  await page.goto("/review");
  await page.getByRole("button", { name: "Published" }).click();
  await page.getByRole("link", { name: new RegExp(DRAFTS.magnesium.replace("?", "\\?")) }).click();
  await page.getByLabel("Reason for withdrawing").fill("The main trial was retracted.");
  await page.getByRole("button", { name: "Withdraw from the site" }).click();
  await expect(page.getByText(/Withdrawn .*: The main trial was retracted\./)).toBeVisible();

  // Asked of the API directly: anonymous reads are cacheable for a minute, so
  // this browser may keep showing its own cached copy for that long.
  const feed = await (await page.request.get("/api/feed")).json();
  expect(feed.items.map((item: { headline: string }) => item.headline)).not.toContain(
    DRAFTS.magnesium,
  );
  const slug = new URL(articleUrl).pathname.split("/").pop();
  expect((await page.request.get(`/api/articles/${slug}`)).status()).toBe(404);
});

test("a reviewer changes their password from the account page", async ({ page }) => {
  // Last in the file on purpose: it changes the seeded reviewer's password.
  const next = "A-new-desk-password-2";
  await signIn(page);
  await page.getByTitle("Password and two-step sign-in").click();
  await page.getByLabel("Current password").fill(REVIEWER.password);
  await page.getByLabel("New password (12 characters or more)").fill(next);
  await page.getByRole("button", { name: "Change password" }).click();
  await expect(page.getByText("Password changed.")).toBeVisible();

  // Still signed in on the fresh token, and the old password no longer works.
  await page.getByRole("link", { name: "← Queue" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Review queue" })).toBeVisible();
  await page.getByRole("button", { name: "Sign out" }).click();
  await signIn(page);
  await expect(page.getByText("Incorrect email or password.")).toBeVisible();
  await signIn(page, next);
  await expect(page.getByRole("heading", { level: 1, name: "Review queue" })).toBeVisible();
});
