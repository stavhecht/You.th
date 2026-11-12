/**
 * "Forgot your password?" - both halves, on one route.
 *
 * Without `?token=` it asks for the email address and says the same thing
 * whether or not an account exists: the server answers the same way on purpose,
 * so this page cannot become a way to test which addresses are registered.
 *
 * With `?token=` (the link in the email) it asks for a new password. The link
 * lasts 30 minutes and stops working once used, because it is tied to the
 * password it replaces. On success the reader signs in with the new password.
 */

import { usePageMeta } from "@/lib/usePageMeta";
import { useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { PasswordChecklist, RevealToggle } from "@/features/reader/PasswordParts";
import { useToast } from "@/features/shell/toast";
import {
  FIELD,
  FIELD_LABEL,
  FORM_ERROR,
  PAGE_BACK_LINK,
  PAGE_KICKER,
  PAGE_STANDFIRST,
  PAGE_TITLE,
  SUBMIT_BUTTON,
  WIDE_PAGE,
} from "@/features/shell/pageStyles";
import { ApiError } from "@/lib/api/client";
import { forgotPassword, resetPassword } from "@/lib/api/reader";
import { passwordProblem } from "@/lib/password";

const LABEL = `${FIELD_LABEL} pl-2`;

export function ForgotPasswordRoute() {
  usePageMeta("Reset your password");
  const [params] = useSearchParams();
  const token = params.get("token");

  return (
    <main className={WIDE_PAGE}>
      <Link to="/signin" className={PAGE_BACK_LINK}>
        ← Sign in
      </Link>
      <div className="max-w-[440px]">{token ? <NewPassword token={token} /> : <AskForLink />}</div>
    </main>
  );
}

function AskForLink() {
  const [sentTo, setSentTo] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    const email = String(new FormData(event.currentTarget).get("email") ?? "").trim();
    setBusy(true);
    try {
      await forgotPassword(email);
      setSentTo(email);
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.status === 429
          ? "Too many attempts from here. Try again in a few minutes."
          : caught instanceof ApiError && caught.status === 422
            ? "Check the email address."
            : "Something went wrong. Try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (sentTo) {
    return (
      <>
        <div className={PAGE_KICKER}>Check your email</div>
        <h1 className={PAGE_TITLE}>We sent you a link.</h1>
        <p className={PAGE_STANDFIRST}>
          If {sentTo} has an account, an email with a link to choose a new password is on
          its way. The link works for 30 minutes. Nothing there? Check your spam folder, or
          try again.
        </p>
      </>
    );
  }

  return (
    <>
      <div className={PAGE_KICKER}>Forgot your password?</div>
      <h1 className={PAGE_TITLE}>We will email you a link.</h1>
      <p className={PAGE_STANDFIRST}>
        Enter the address you signed up with, and we will send a link to choose a new
        password.
      </p>
      <form onSubmit={onSubmit} className="mt-7">
        <label className="mb-5 block">
          <span className={LABEL}>Email</span>
          <input
            name="email"
            type="email"
            required
            autoComplete="email"
            placeholder="your@email.com"
            className={FIELD}
          />
        </label>
        <button type="submit" disabled={busy} className={SUBMIT_BUTTON}>
          {busy ? "One moment…" : "Send me a link →"}
        </button>
        {error ? (
          <p role="alert" className={FORM_ERROR}>
            {error}
          </p>
        ) : null}
      </form>
    </>
  );
}

function NewPassword({ token }: { token: string }) {
  const navigate = useNavigate();
  const flash = useToast();
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirm, setShowConfirm] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expired, setExpired] = useState(false);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    const problem = passwordProblem(password);
    if (problem) return setError(problem);
    if (password !== confirm) return setError("The two passwords do not match.");

    setBusy(true);
    try {
      await resetPassword(token, password);
      flash("Password changed. Sign in with the new one");
      navigate("/signin");
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 400) setExpired(true);
      else
        setError(
          caught instanceof ApiError && caught.status === 429
            ? "Too many attempts from here. Try again in a few minutes."
            : "Something went wrong. Try again.",
        );
    } finally {
      setBusy(false);
    }
  }

  if (expired) {
    return (
      <>
        <h1 className={PAGE_TITLE}>This link has expired.</h1>
        <p className={PAGE_STANDFIRST}>
          Reset links work for 30 minutes and only once.{" "}
          <Link to="/forgot-password" className="underline">
            Send a new one
          </Link>
          .
        </p>
      </>
    );
  }

  return (
    <>
      <div className={PAGE_KICKER}>Reset your password</div>
      <h1 className={PAGE_TITLE}>Choose a new password.</h1>
      <form onSubmit={onSubmit} className="mt-7">
        <label className="mb-4 block">
          <span className={LABEL}>New password</span>
          <div className="relative">
            <input
              name="password"
              type={showPassword ? "text" : "password"}
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete="new-password"
              aria-describedby="password-rules"
              className={`${FIELD} pr-12`}
            />
            <RevealToggle shown={showPassword} onToggle={() => setShowPassword((on) => !on)} />
          </div>
        </label>
        <label className="mb-4 block">
          <span className={LABEL}>Confirm new password</span>
          <div className="relative">
            <input
              name="confirmPassword"
              type={showConfirm ? "text" : "password"}
              required
              value={confirm}
              onChange={(event) => setConfirm(event.target.value)}
              autoComplete="new-password"
              aria-invalid={confirm !== "" && confirm !== password}
              aria-describedby="password-match"
              className={`${FIELD} pr-12`}
            />
            <RevealToggle shown={showConfirm} onToggle={() => setShowConfirm((on) => !on)} />
          </div>
        </label>
        <PasswordChecklist password={password} confirm={confirm} />
        <button type="submit" disabled={busy} className={SUBMIT_BUTTON}>
          {busy ? "One moment…" : "Save new password →"}
        </button>
        {error ? (
          <p role="alert" className={FORM_ERROR}>
            {error}
          </p>
        ) : null}
      </form>
    </>
  );
}
