/**
 * "Forgot your password?" for the review desk.
 *
 * Without a token: ask for the work email, and the server mails a 30-minute
 * link (it answers the same whether or not the address is a reviewer's).
 * With `?token=` from that link: choose a new password. Setting it signs out
 * every session that used the old one, and the link then stops working.
 *
 * The desk's own page, not the public `/forgot-password`: the two surfaces
 * share no chrome, and a reader's link cannot reset a reviewer.
 */

import { useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { forgotReviewerPassword, resetReviewerPassword } from "@/lib/api/console";
import { PasswordField } from "./PasswordField";
import { describeError } from "./errors";
import {
  ACTION_ERROR_ALERT,
  DECISION_NOTE,
  FIELD_LABEL,
  LOGIN_CARD,
  LOGIN_COLUMN,
  LOGIN_FIELD,
  LOGIN_FIELD_GROUP,
  LOGIN_FOOTNOTE,
  LOGIN_FOOTNOTE_LINK,
  LOGIN_FORM,
  LOGIN_KICKER,
  LOGIN_PAGE,
  LOGIN_STANDFIRST,
  LOGIN_SUBMIT,
  LOGIN_TITLE,
} from "./styles";

export function ReviewerReset() {
  const [params] = useSearchParams();
  const token = params.get("token");

  return (
    <main className={LOGIN_PAGE}>
      <div className={LOGIN_COLUMN}>
        <div className={LOGIN_CARD}>
          <div className={LOGIN_KICKER}>Review desk</div>
          {token ? <ChooseNew token={token} /> : <AskForLink />}
        </div>
        <p className={LOGIN_FOOTNOTE}>
          <Link to="/review/login" className={LOGIN_FOOTNOTE_LINK}>
            Back to sign in
          </Link>
        </p>
      </div>
    </main>
  );
}

function AskForLink() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await forgotReviewerPassword(email);
      setSent(true);
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  if (sent) {
    return (
      <>
        <h1 className={LOGIN_TITLE}>Check your email.</h1>
        <p className={LOGIN_STANDFIRST}>
          If {email} is a reviewer account, a link to choose a new password is on its
          way. It works for 30 minutes. Nothing there? Check the spam folder.
        </p>
      </>
    );
  }

  return (
    <>
      <h1 className={LOGIN_TITLE}>Forgot your password?</h1>
      <p className={LOGIN_STANDFIRST}>
        We will email a link to choose a new one. You do not need the old one.
      </p>
      <form onSubmit={onSubmit} className={LOGIN_FORM}>
        <label htmlFor="reset-email" className={FIELD_LABEL}>
          Work email
        </label>
        <input
          id="reset-email"
          type="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          required
          autoComplete="username"
          className={LOGIN_FIELD}
        />
        {error ? (
          <p role="alert" className={ACTION_ERROR_ALERT}>
            {error}
          </p>
        ) : null}
        <button type="submit" disabled={busy} className={LOGIN_SUBMIT}>
          {busy ? "Sending…" : "Email me a link →"}
        </button>
      </form>
    </>
  );
}

function ChooseNew({ token }: { token: string }) {
  const navigate = useNavigate();
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (password !== confirm) return setError("The two passwords do not match.");
    setError(null);
    setBusy(true);
    try {
      await resetReviewerPassword(token, password);
      void navigate("/review/login", { replace: true, state: { reset: true } });
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h1 className={LOGIN_TITLE}>Choose a new password.</h1>
      <p className={LOGIN_STANDFIRST}>
        Twelve characters or more. Every session signed in with the old one ends.
      </p>
      <form onSubmit={onSubmit} className={LOGIN_FORM}>
        <label htmlFor="reset-new" className={FIELD_LABEL}>
          New password
        </label>
        <PasswordField
          id="reset-new"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          required
          minLength={12}
          autoComplete="new-password"
        />
        <div className={LOGIN_FIELD_GROUP}>
          <label htmlFor="reset-confirm" className={FIELD_LABEL}>
            The same again
          </label>
          <PasswordField
            id="reset-confirm"
            value={confirm}
            onChange={(event) => setConfirm(event.target.value)}
            required
            minLength={12}
            autoComplete="new-password"
          />
        </div>
        {error ? (
          <p role="alert" className={ACTION_ERROR_ALERT}>
            {error}
          </p>
        ) : null}
        <button type="submit" disabled={busy} className={LOGIN_SUBMIT}>
          {busy ? "Saving…" : "Set new password →"}
        </button>
        <p className={DECISION_NOTE}>The link works once, for 30 minutes.</p>
      </form>
    </>
  );
}
