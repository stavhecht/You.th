/**
 * The email shape the API accepts — mirror of `EMAIL_PATTERN` in
 * backend/app/api/schemas_base.py. The browser's own `type="email"` check
 * passes `ada@gmail`, which the API then refuses with a generic error.
 * English (ASCII) addresses only, like the API.
 */
const EMAIL_RE =
  /^[a-z0-9_!#$%&'*+/=?^`{|}~-]+(\.[a-z0-9_!#$%&'*+/=?^`{|}~-]+)*@([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+([a-z]{2,}|xn--[a-z0-9-]+)$/i;

export function isEmail(value: string): boolean {
  const trimmed = value.trim();
  return trimmed.length <= 254 && EMAIL_RE.test(trimmed);
}

export const EMAIL_HINT =
  "That email address looks incomplete. Check it reads like name@example.com.";
