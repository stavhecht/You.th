/**
 * The new-password pieces sign-up and the password reset share: the eye that
 * shows the password, and the checklist that ticks off each rule while typing.
 * One copy, so the two forms cannot drift apart.
 */

import { Eye, EyeOff } from "lucide-react";

import {
  PASSWORD_RULE_LIST,
  PASSWORD_RULE_MARK,
  passwordRule,
} from "@/features/shell/pageStyles";
import { PASSWORD_RULES } from "@/lib/password";

/**
 * The eye beside a password field. A button, not a checkbox, so it reads as an
 * action; `aria-pressed` gives a screen reader the state without a second label.
 * It sits inside the field's `<label>`, and a click on an interactive child
 * does not re-focus the input, so the field keeps its own focus.
 */
export function RevealToggle({ shown, onToggle }: { shown: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-label={shown ? "Hide password" : "Show password"}
      aria-pressed={shown}
      className="absolute right-2 top-1/2 -translate-y-1/2 rounded-full p-2 text-ink-3 hover:text-ink"
    >
      {shown ? <Eye size={17} aria-hidden /> : <EyeOff size={17} aria-hidden />}
    </button>
  );
}

/**
 * The rules, checked off as they are met, so the reader finds out what is
 * missing while typing rather than after a submit. The two ids are what the
 * fields' `aria-describedby` point at.
 */
export function PasswordChecklist({ password, confirm }: { password: string; confirm: string }) {
  const mismatch = confirm !== "" && confirm !== password;
  return (
    <ul id="password-rules" className={PASSWORD_RULE_LIST}>
      {PASSWORD_RULES.map((rule) => {
        const met = rule.met(password);
        return (
          <li key={rule.label} className={passwordRule(met)}>
            <span aria-hidden className={PASSWORD_RULE_MARK}>
              {met ? "✓" : "·"}
            </span>
            {rule.label}
            <span className="sr-only">{met ? ", done" : ""}</span>
          </li>
        );
      })}
      <li id="password-match" className={passwordRule(!mismatch && confirm !== "")}>
        <span aria-hidden className={PASSWORD_RULE_MARK}>
          {mismatch ? "✕" : confirm !== "" ? "✓" : "·"}
        </span>
        {mismatch ? "Passwords do not match" : "Passwords match"}
      </li>
    </ul>
  );
}
