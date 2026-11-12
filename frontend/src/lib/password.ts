/**
 * What a new password must contain. The server refuses anything short of this
 * (`PASSWORD_RULES` in `backend/app/api/public/schemas.py`, used by signup and
 * by a password reset); the two lists must change together, or a form passes a
 * password the server refuses - or blocks one it would have taken.
 */
export const PASSWORD_RULES: { label: string; met: (password: string) => boolean }[] = [
  { label: "At least 8 characters", met: (pw) => pw.length >= 8 },
  { label: "A lowercase letter", met: (pw) => /[a-z]/.test(pw) },
  { label: "A capital letter", met: (pw) => /[A-Z]/.test(pw) },
  { label: "A number", met: (pw) => /[0-9]/.test(pw) },
  { label: "A special character", met: (pw) => /[^A-Za-z0-9]/.test(pw) },
];

/** "Your password needs a number, a capital letter." or null when all are met. */
export function passwordProblem(password: string): string | null {
  const unmet = PASSWORD_RULES.filter((rule) => !rule.met(password));
  return unmet.length
    ? `Your password needs ${unmet.map((rule) => rule.label.toLowerCase()).join(", ")}.`
    : null;
}
