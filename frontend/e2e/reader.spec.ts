/**
 * A reader's account, from the sign-up form to deleting it: the only part of
 * the public site that writes anything.
 */
import { PUBLISHED, READER_PASSWORD, expect, freshEmail, signUp, test } from "./fixtures";

test("signing up checks the password as it is typed", async ({ page }) => {
  await page.goto("/join");
  await page.locator('input[name="password"]').fill("short");
  await page.locator('input[name="confirmPassword"]').fill("different");
  await expect(page.getByText("Passwords do not match")).toBeVisible();

  await page.locator('input[name="confirmPassword"]').fill("short");
  await expect(page.getByText("Passwords match")).toBeVisible();
});

test("a taken address is refused with a way forward", async ({ page }) => {
  const email = freshEmail("taken");
  await signUp(page, email);
  await page.context().clearCookies();
  await page.evaluate(() => window.localStorage.clear());

  await page.goto("/join");
  await page.locator('input[name="displayName"]').fill("Twin");
  await page.locator('input[name="email"]').fill(email.toUpperCase());
  await page.locator('input[name="password"]').fill(READER_PASSWORD);
  await page.locator('input[name="confirmPassword"]').fill(READER_PASSWORD);
  await page.getByRole("button", { name: "Build my feed →" }).click();
  await expect(page.getByText(/already an account with that email/i)).toBeVisible();
});

test("a signed-out reader who tries to save is told why and offered an account", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("link", { name: PUBLISHED.foam, exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: PUBLISHED.foam })).toBeVisible();
  await page.getByRole("button", { name: "Save this", exact: true }).click();
  await expect(page.getByText("Create an account to keep this")).toBeVisible();
  await page.getByRole("button", { name: "Create an account", exact: true }).click();
  await expect(page).toHaveURL(/\/join/);
});

test("sign up, save an article, find it on the shelf, then delete the account", async ({
  page,
}) => {
  await signUp(page, freshEmail("journey"));
  await expect(page.getByRole("link", { name: "Your profile" })).toBeVisible();

  await page.getByRole("link", { name: PUBLISHED.creatine, exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: PUBLISHED.creatine })).toBeVisible();
  await page.getByRole("button", { name: "Save to folder", exact: true }).click();
  await expect(page.getByRole("button", { name: "Remove from folder", exact: true })).toBeVisible();

  await page.goto("/you");
  await expect(page.getByRole("link", { name: PUBLISHED.creatine, exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Delete my account" }).click();
  await page.locator('input[autocomplete="current-password"]').fill("Wrong-password-9");
  await page.getByRole("button", { name: "Delete everything" }).click();
  await expect(page.getByText(/password/i).first()).toBeVisible(); // refused, still here

  await page.locator('input[autocomplete="current-password"]').fill(READER_PASSWORD);
  await page.getByRole("button", { name: "Delete everything" }).click();
  await expect(page.getByRole("link", { name: "Create an account" })).toBeVisible();
});
