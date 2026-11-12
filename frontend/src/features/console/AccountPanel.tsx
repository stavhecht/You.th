/**
 * The reviewer's own account: changing a password they know.
 *
 * A forgotten password is a different route: "Forgot your password?" on the
 * sign-in page mails a reset link (`ReviewerReset.tsx`). Changing it here signs
 * out every other session; this one continues on the fresh token the server
 * answers with.
 */

import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { changePassword } from "@/lib/api/console";
import { PasswordField } from "./PasswordField";
import { useAuth } from "./auth";
import { describeError } from "./errors";
import { SECTION_LABEL } from "./controls";
import {
  ACTION_ERROR_ALERT,
  BACK_TO_QUEUE,
  DECISION_BLOCK,
  DECISION_NOTE,
  FIELD_LABEL,
  LOGIN_CARD,
  LOGIN_COLUMN,
  LOGIN_FIELD_GROUP,
  LOGIN_PAGE,
  LOGIN_SUBMIT,
  LOGIN_TITLE,
} from "./styles";

export function AccountPanel() {
  const { reviewer, adopt } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setMessage(null);
    setBusy(true);
    try {
      await adopt(await changePassword(current, next));
      setCurrent("");
      setNext("");
      setMessage("Password changed. Every other session has been signed out.");
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className={LOGIN_PAGE}>
      <div className={LOGIN_COLUMN}>
        <Link to="/review" className={BACK_TO_QUEUE}>
          ← Queue
        </Link>
        <div className={LOGIN_CARD}>
          <h1 className={LOGIN_TITLE}>Your account</h1>
          <p className={DECISION_NOTE}>{reviewer?.email}</p>

          <form onSubmit={onSubmit} className={DECISION_BLOCK}>
            <span className={SECTION_LABEL}>Password</span>
            <div className={LOGIN_FIELD_GROUP}>
              <label htmlFor="account-current" className={FIELD_LABEL}>
                Current password
              </label>
              <PasswordField
                id="account-current"
                value={current}
                onChange={(event) => setCurrent(event.target.value)}
                required
                autoComplete="current-password"
              />
            </div>
            <div className={LOGIN_FIELD_GROUP}>
              <label htmlFor="account-new" className={FIELD_LABEL}>
                New password (12 characters or more)
              </label>
              <PasswordField
                id="account-new"
                value={next}
                onChange={(event) => setNext(event.target.value)}
                required
                minLength={12}
                autoComplete="new-password"
              />
            </div>
            <button type="submit" disabled={busy} className={LOGIN_SUBMIT}>
              Change password
            </button>
            <p className={DECISION_NOTE}>
              Forgot the current one? Sign out and use “Forgot your password?” on the
              sign-in page.
            </p>
          </form>

          {error ? (
            <p role="alert" className={ACTION_ERROR_ALERT}>
              {error}
            </p>
          ) : message ? (
            <p role="status" className={DECISION_NOTE}>
              {message}
            </p>
          ) : null}
        </div>
      </div>
    </main>
  );
}
