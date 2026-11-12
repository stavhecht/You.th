/**
 * The review desk's sign-in.
 *
 * The server returns one identical 401 for unknown email and wrong password,
 * and this form shows that message verbatim - narrowing it to "no such user"
 * would hand back the account enumeration the API deliberately avoids.
 *
 * The standfirst says what signing in commits you to rather than welcoming you:
 * every approval is recorded against a name, and this is the screen where a
 * reviewer takes that on.
 *
 * Drawn in the You.th shapes - one rounded card on the paper, pill button,
 * rounded fields, wordmark centred in the bar above. The accent kicker is what
 * says which surface this is; the rest is the product a reviewer already knows.
 */

import { ApiError } from "@/lib/api/client";
import { useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { PasswordField } from "./PasswordField";
import { useAuth } from "./auth";
import {
  FIELD_LABEL,
  LOGIN_CARD,
  LOGIN_COLUMN,
  LOGIN_ERROR,
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

export function LoginForm() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const justReset = Boolean((location.state as { reset?: boolean } | null)?.reset);
  const destination =
    (location.state as { from?: { pathname: string } } | null)?.from?.pathname ??
    "/review";

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setPending(true);
    try {
      await login(email, password);
      void navigate(destination, { replace: true });
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.status === 429
          ? "Too many attempts. Wait a minute and try again."
          : "Incorrect email or password.",
      );
    } finally {
      setPending(false);
    }
  }

  return (
    <main className={LOGIN_PAGE}>
      <div className={LOGIN_COLUMN}>
        <div className={LOGIN_CARD}>
          <div className={LOGIN_KICKER}>Review desk</div>
          <h1 className={LOGIN_TITLE}>Sign in to the queue.</h1>
          <p className={LOGIN_STANDFIRST}>
            Reviewer access only. Every approval is recorded against the name you
            sign in with, and nothing in the queue reaches the public feed
            without one.
          </p>

          <form onSubmit={onSubmit} className={LOGIN_FORM}>
            <FieldLabel htmlFor="review-email">Work email</FieldLabel>
            <input
              id="review-email"
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              required
              autoComplete="username"
              placeholder="you@you.th"
              className={LOGIN_FIELD}
            />

            <div className={LOGIN_FIELD_GROUP}>
              <FieldLabel htmlFor="review-password">Password</FieldLabel>
              <PasswordField
                id="review-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                required
                autoComplete="current-password"
              />
            </div>


            {justReset && !error ? (
              <p role="status" className={LOGIN_STANDFIRST}>
                Your password is changed. Sign in with the new one.
              </p>
            ) : null}

            {error ? (
              <p role="alert" className={LOGIN_ERROR}>
                {error}
              </p>
            ) : null}

            <button type="submit" disabled={pending} className={LOGIN_SUBMIT}>
              {pending ? "Signing in…" : "Sign in →"}
            </button>
            <p className={LOGIN_FOOTNOTE}>
              <Link to="/review/reset" className={LOGIN_FOOTNOTE_LINK}>
                Forgot your password?
              </Link>
            </p>
          </form>
        </div>

        {/*
          The desk links out to the site; the site never links in. A reviewer
          who landed here by mistake needs a way back, and it is the only
          crossing that carries no information about what is unpublished.
        */}
        <p className={LOGIN_FOOTNOTE}>
          Not a reviewer?{" "}
          <Link to="/" className={LOGIN_FOOTNOTE_LINK}>
            Back to You.th
          </Link>
        </p>
      </div>
    </main>
  );
}

function FieldLabel({ htmlFor, children }: { htmlFor: string; children: string }) {
  return (
    <label htmlFor={htmlFor} className={FIELD_LABEL}>
      {children}
    </label>
  );
}

