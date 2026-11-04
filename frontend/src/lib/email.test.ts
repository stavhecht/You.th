import { describe, expect, it } from "vitest";

import { isEmail } from "./email";

// Same cases as backend/tests/unitTest/test_email_address.py, so the two stay in step.
describe("isEmail", () => {
  it.each([
    "ada@example.com",
    "  Ada.Lovelace+news@Mail.Example.CO.UK ",
    "o'brien@my-site.ie",
    "ada@xn--80ak6aa92e.com",
    "a@b.xn--p1ai",
  ])("accepts %s", (value) => expect(isEmail(value)).toBe(true));

  it.each([
    "ada",
    "ada@gmail",
    "ada@@example.com",
    "@example.com",
    "ada@",
    ".ada@example.com",
    "ada.@example.com",
    "ad..a@example.com",
    "ada@example..com",
    "ada@-example.com",
    "ada@example-.com",
    "ada@example.c",
    "ada@example.c0m",
    "ada lovelace@example.com",
    "ada@exa mple.com",
    "ada,b@example.com",
    "ada@example.com.",
    "हिंदी@example.com",
    "josé@example.com",
    "ada@exämple.com",
    "\u017fam@example.com",
    "\u212aate@example.com",
  ])("refuses %s", (value) => expect(isEmail(value)).toBe(false));
});
