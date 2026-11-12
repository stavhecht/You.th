/**
 * The public site as a signed-out reader uses it: the feed, an article, search,
 * browsing by subject, and every standing page - on desktop, and the feed on a
 * phone.
 */
import { DRAFTS, PUBLISHED, expect, test } from "./fixtures";

const card = (page: import("@playwright/test").Page, headline: string) =>
  page.getByRole("link", { name: headline, exact: true });

test("the feed shows published articles and never a draft", async ({ page }) => {
  await page.goto("/");
  for (const headline of Object.values(PUBLISHED)) {
    await expect(card(page, headline)).toBeVisible();
  }
  for (const headline of Object.values(DRAFTS)) {
    await expect(card(page, headline)).toHaveCount(0);
  }
});

test("a card opens its article, with the verdict and its sources", async ({ page }) => {
  await page.goto("/");
  await card(page, PUBLISHED.creatine).click();
  await expect(page).toHaveURL(/\/a\//);
  await expect(page.getByRole("heading", { level: 1, name: PUBLISHED.creatine })).toBeVisible();
  await expect(page.getByText("Supported", { exact: false }).first()).toBeVisible();
  // The byline names the reviewer who approved it, as an admin named them.
  await expect(page.getByText("Checked and approved by Browser Reviewer")).toBeVisible();
  await expect(page.getByText("Medical Team")).toHaveCount(0);
});

test("an unknown article is a not-found page, not a crash", async ({ page }) => {
  await page.goto("/a/no-such-article");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.getByRole("heading", { level: 1 })).not.toHaveText(PUBLISHED.creatine);
});

test("search finds by word and narrows the feed", async ({ page }) => {
  await page.goto("/");
  const search = page.getByRole("searchbox", { name: "Search published articles" }).or(
    page.getByLabel("Search published articles"),
  );
  await search.first().fill("creatine");
  await search.first().press("Enter");

  await expect(page).toHaveURL(/q=creatine/);
  await expect(card(page, PUBLISHED.creatine)).toBeVisible();
  await expect(card(page, PUBLISHED.foam)).toHaveCount(0);
});

test("a search with no match leaves no stale results on screen", async ({ page }) => {
  await page.goto("/?q=zzqxv");
  for (const headline of Object.values(PUBLISHED)) {
    await expect(card(page, headline)).toHaveCount(0);
  }
});

test("browsing by subject narrows the feed to that subject", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Browse categories" }).click();
  const drawer = page.getByRole("dialog");
  await expect(drawer).toBeVisible();
  await drawer.getByRole("button", { name: /Fitness/ }).click();

  await expect(page).toHaveURL(/subject=fitness/);
  await expect(card(page, PUBLISHED.foam)).toBeVisible();
  await expect(card(page, PUBLISHED.creatine)).toHaveCount(0);
});

for (const path of ["/about", "/contact", "/privacy", "/terms", "/accessibility", "/join"]) {
  test(`the ${path} page renders`, async ({ page }) => {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
  });
}

test("the feed fits a phone screen @phone", async ({ page }) => {
  await page.goto("/");
  await expect(card(page, PUBLISHED.creatine)).toBeVisible();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  );
  expect(overflow, "no sideways scrolling").toBeLessThanOrEqual(0);
});

test("an article fits a phone screen @phone", async ({ page }) => {
  await page.goto("/");
  await card(page, PUBLISHED.foam).click();
  await expect(page.getByRole("heading", { level: 1, name: PUBLISHED.foam })).toBeVisible();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  );
  expect(overflow, "no sideways scrolling").toBeLessThanOrEqual(0);
});

test("search opens and works on a phone @phone", async ({ page }) => {
  await page.goto("/");
  const search = page.getByLabel("Search published articles");
  await search.tap();
  await expect(search).toBeFocused();
  expect((await search.boundingBox())!.width, "room to type").toBeGreaterThan(150);
  await search.fill("creatine");
  await search.press("Enter");
  await expect(page).toHaveURL(/q=creatine/);
  await expect(card(page, PUBLISHED.creatine)).toBeVisible();
});

test("a citation card stays on a phone screen @phone", async ({ page }) => {
  // The seeded articles carry no citations, so add some, at varied line positions.
  await page.route(/\/api\/articles\//, async (route) => {
    const article = await (await route.fetch()).json();
    const words = (count: number) => ({ type: "text", text: "word ".repeat(count) });
    const cite = (handle: string) => ({ type: "citation", attrs: { sourceIds: [handle] } });
    article.content.content.push({
      type: "paragraph",
      content: [words(1), cite("S1"), words(3), cite("S2"), words(5), cite("S3"), words(7), cite("S4")],
    });
    await route.fulfill({ json: article });
  });
  await page.goto("/");
  await card(page, PUBLISHED.creatine).click();
  const chips = page.getByRole("link", { name: /^Source / });
  await expect(chips).toHaveCount(4);
  const width = page.viewportSize()!.width;
  for (let index = 0; index < 4; index += 1) {
    await chips.nth(index).tap();
    const box = await page.getByText(/^Source S\d/).locator("..").boundingBox();
    expect(box!.x, "left edge").toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width, "right edge").toBeLessThanOrEqual(width);
    await chips.nth(index).tap();
  }
});

test("an unknown address says so and offers a way back", async ({ page }) => {
  await page.goto("/no-such-page");
  await expect(page.getByRole("heading", { level: 1, name: "Page not found" })).toBeVisible();
  await page.getByRole("link", { name: "Back to the feed" }).click();
  await expect(page).toHaveURL("/");
});

test("an article names itself in the tab title", async ({ page }) => {
  await page.goto("/");
  await card(page, PUBLISHED.foam).click();
  await expect(page).toHaveTitle(`${PUBLISHED.foam} · You.th`);
  await page.goBack();
  await expect(page).toHaveTitle(/simplifying your way/);
});
