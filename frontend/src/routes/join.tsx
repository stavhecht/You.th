/**
 * "Choose your feed" — sign up, or sign in.
 *
 * One screen for both, because the second one is a link away and a separate
 * `/login` route would be a page whose only content is two fields. The
 * interests are picked *before* the account exists and posted with it, so the
 * first feed a new reader sees is already ordered around what they asked for —
 * which is the only thing that makes picking them feel like it did anything.
 *
 * The panel on the right says what an account does, in three claims that are
 * all literally true of the implementation: interests reorder the feed
 * (`FeedService.page`), folders persist server-side (`reader_folders`), and the
 * newsletter is a stored opt-in. Nothing here promises a notification, because
 * nothing sends one.
 */

import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { SUBJECTS, SUBJECT_LABELS } from "@/features/evidence/subject";
import { useReader } from "@/features/reader/auth";
import { useToast } from "@/features/shell/toast";
import {
  CHECKBOX,
  CHECKBOX_LABEL,
  CHECKBOX_ROW,
  CHIP_ROW,
  COLLECTION_NOTICE,
  FIELD,
  FIELD_LABEL,
  FORM_ERROR,
  FORM_GRID,
  FORM_NOTE,
  PAGE_BACK_LINK,
  PAGE_KICKER,
  PAGE_STANDFIRST,
  PAGE_TITLE,
  PANEL,
  PANEL_DIVIDER,
  PANEL_ITEM_BODY,
  PANEL_ITEM_TITLE,
  PANEL_LABEL,
  PANEL_LIST,
  PASSWORD_RULE_LIST,
  PASSWORD_RULE_MARK,
  SPLIT,
  SUBMIT_BUTTON,
  WIDE_PAGE,
  chip,
  passwordRule,
} from "@/features/shell/pageStyles";
import { ApiError } from "@/lib/api/client";
import type { Subject } from "@/types/api";

const WHAT_IT_DOES = [
  {
    title: "Orders your feed",
    body: "Your subjects move to the top. Nothing is hidden — the rest is still one scroll away.",
  },
  {
    title: "Keeps your folders",
    body: "Save an article into a folder and it is still there on your next device.",
  },
  {
    title: "Records the newsletter",
    body: "One opt-in, stored against your account. We are not sending it yet.",
  },
];

/**
 * What a new password must contain. The server refuses anything short of this
 * (`SignupRequest` in `backend/app/api/public/schemas.py`, `PASSWORD_RULES`);
 * the two lists must change together, or the form passes a password the server
 * refuses — or blocks one it would have taken.
 */
const PASSWORD_RULES: { label: string; met: (password: string) => boolean }[] =
  [
    { label: "At least 8 characters", met: (pw) => pw.length >= 8 },
    { label: "A lowercase letter", met: (pw) => /[a-z]/.test(pw) },
    { label: "A capital letter", met: (pw) => /[A-Z]/.test(pw) },
    { label: "A number", met: (pw) => /[0-9]/.test(pw) },
    { label: "A special character", met: (pw) => /[^A-Za-z0-9]/.test(pw) },
  ];

export function JoinRoute() {
  const { signup, login, signedIn } = useReader();
  const navigate = useNavigate();
  const flash = useToast();

  const [mode, setMode] = useState<"signup" | "login">("signup");
  const [interests, setInterests] = useState<Subject[]>([]);
  // Unticked until the reader ticks it. A pre-ticked box is not consent —
  // not under Israel's anti-spam law (s.30A requires explicit consent) and not
  // under the GDPR (CJEU, Planet49) — so a newsletter sent on it would be sent
  // to people who never asked. COMPLIANCE_AUDIT.md F5.
  const [newsletter, setNewsletter] = useState(false);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const signingUp = mode === "signup";
  const unmet = PASSWORD_RULES.filter((rule) => !rule.met(password));
  const mismatch = confirm !== "" && confirm !== password;

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);

    const form = new FormData(event.currentTarget);
    const email = String(form.get("email") ?? "").trim();
    const displayName = String(form.get("displayName") ?? "").trim();

    if (signingUp) {
      if (unmet.length) {
        setError(
          `Your password needs ${unmet
            .map((rule) => rule.label.toLowerCase())
            .join(", ")}.`,
        );
        return;
      }
      if (password !== confirm) {
        setError("The two passwords do not match.");
        return;
      }
    }

    setBusy(true);
    try {
      if (signingUp) {
        await signup({ email, password, displayName, interests, newsletter });
        flash(
          interests.length
            ? `Your feed is ordered around ${interests.length} ${
                interests.length === 1 ? "subject" : "subjects"
              }`
            : "Welcome — your feed is everything, newest first",
        );
      } else {
        await login(email, password);
        flash("Signed in");
      }
      navigate("/");
    } catch (caught) {
      setError(readable(caught, signingUp));
    } finally {
      setBusy(false);
    }
  }

  if (signedIn) {
    return (
      <main className={WIDE_PAGE}>
        <Link to="/" className={PAGE_BACK_LINK}>
          ← Feed
        </Link>
        <h1 className={PAGE_TITLE}>You are signed in.</h1>
        <p className={PAGE_STANDFIRST}>
          Your interests and folders live on{" "}
          <Link to="/you" className="underline">
            your profile
          </Link>
          .
        </p>
      </main>
    );
  }

  return (
    <main className={WIDE_PAGE}>
      <div className={SPLIT}>
        <div>
          <Link to="/" className={PAGE_BACK_LINK}>
            ← Feed
          </Link>

          <div className={PAGE_KICKER}>
            {signingUp ? "Choose your feed" : "Welcome back"}
          </div>
          <h1 className={PAGE_TITLE}>
            {signingUp
              ? "Tell us what you want to be exposed to."
              : "Sign in to your feed."}
          </h1>
          <p className={PAGE_STANDFIRST}>
            {signingUp
              ? "Pick the subjects you actually care about. Your answers order your feed, and your account keeps what you save."
              : "Your interests, folders and saved articles are waiting where you left them."}
          </p>

          <form onSubmit={onSubmit} className="mt-7">
            {signingUp ? (
              <>
                <div className={FIELD_LABEL}>Interests</div>
                <div className={`${CHIP_ROW} mb-7`}>
                  {SUBJECTS.map((subject) => {
                    const on = interests.includes(subject);
                    return (
                      <button
                        key={subject}
                        type="button"
                        onClick={() =>
                          setInterests((current) =>
                            on
                              ? current.filter((value) => value !== subject)
                              : [...current, subject],
                          )
                        }
                        aria-pressed={on}
                        className={chip(on)}
                      >
                        {SUBJECT_LABELS[subject]}
                      </button>
                    );
                  })}
                </div>
              </>
            ) : null}

            <div className={`${FORM_GRID} mb-4`}>
              {signingUp ? (
                <label>
                  <span className={FIELD_LABEL}>First name</span>
                  <input
                    name="displayName"
                    required
                    autoComplete="given-name"
                    placeholder="Your name"
                    className={FIELD}
                  />
                </label>
              ) : null}

              <label>
                <span className={FIELD_LABEL}>Email</span>
                <input
                  name="email"
                  type="email"
                  required
                  autoComplete="email"
                  placeholder="your@email.com"
                  className={FIELD}
                />
              </label>

              <label>
                <span className={FIELD_LABEL}>Password</span>
                <input
                  name="password"
                  type="password"
                  required
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  autoComplete={signingUp ? "new-password" : "current-password"}
                  aria-describedby={signingUp ? "password-rules" : undefined}
                  className={FIELD}
                />
              </label>

              {signingUp ? (
                <label>
                  <span className={FIELD_LABEL}>Confirm password</span>
                  <input
                    name="confirmPassword"
                    type="password"
                    required
                    value={confirm}
                    onChange={(event) => setConfirm(event.target.value)}
                    autoComplete="new-password"
                    aria-invalid={mismatch}
                    aria-describedby="password-match"
                    className={FIELD}
                  />
                </label>
              ) : null}
            </div>

            {signingUp ? (
              // The rules are checked off as they are met, so the reader finds
              // out what is missing while typing rather than after a submit.
              <ul id="password-rules" className={PASSWORD_RULE_LIST}>
                {PASSWORD_RULES.map((rule) => {
                  const met = rule.met(password);
                  return (
                    <li key={rule.label} className={passwordRule(met)}>
                      <span aria-hidden className={PASSWORD_RULE_MARK}>
                        {met ? "✓" : "·"}
                      </span>
                      {rule.label}
                      <span className="sr-only">{met ? " — done" : ""}</span>
                    </li>
                  );
                })}
                <li
                  id="password-match"
                  className={passwordRule(!mismatch && confirm !== "")}
                >
                  <span aria-hidden className={PASSWORD_RULE_MARK}>
                    {mismatch ? "✕" : confirm !== "" ? "✓" : "·"}
                  </span>
                  {mismatch ? "Passwords do not match" : "Passwords match"}
                </li>
              </ul>
            ) : null}

            {signingUp ? (
              <label className={`${CHECKBOX_ROW} mb-6`}>
                <input
                  type="checkbox"
                  checked={newsletter}
                  onChange={(event) => setNewsletter(event.target.checked)}
                  className={CHECKBOX}
                />
                <span className={CHECKBOX_LABEL}>
                  Put me on the weekly newsletter — one email, the studies that
                  landed that week.
                </span>
              </label>
            ) : null}

            <button type="submit" disabled={busy} className={SUBMIT_BUTTON}>
              {busy
                ? "One moment…"
                : signingUp
                  ? "Build my feed →"
                  : "Sign in →"}
            </button>

            {error ? (
              <p role="alert" className={FORM_ERROR}>
                {error}
              </p>
            ) : null}

            {signingUp ? (
              // The notice at the point of collection (Israeli PPL s.11, GDPR
              // Art 13): what is collected, what for, and where the rest is.
              <p className={COLLECTION_NOTICE}>
                We use your email, name and password to run your account, and your
                subjects and saves to order your feed — nothing else, and we never
                sell them. You can download or delete it all from your profile.
                By creating an account you agree to the{" "}
                <Link to="/terms" className="underline">
                  terms
                </Link>
                ; see the{" "}
                <Link to="/privacy" className="underline">
                  privacy policy
                </Link>{" "}
                for the details.
              </p>
            ) : null}

            <p className={FORM_NOTE}>
              {signingUp ? "Free account. " : ""}
              <button
                type="button"
                onClick={() => {
                  setMode(signingUp ? "login" : "signup");
                  setError(null);
                }}
                className="underline"
              >
                {signingUp
                  ? "Already have an account? Sign in"
                  : "Need an account? Sign up"}
              </button>
            </p>
          </form>
        </div>

        <aside className={PANEL}>
          <div className={PANEL_LABEL}>What the account does</div>
          <div className={PANEL_LIST}>
            {WHAT_IT_DOES.map((item, index) => (
              <div key={item.title}>
                {index > 0 ? <div className={`${PANEL_DIVIDER} mb-5`} /> : null}
                <div className={PANEL_ITEM_TITLE}>{item.title}</div>
                <div className={PANEL_ITEM_BODY}>{item.body}</div>
              </div>
            ))}
          </div>
        </aside>
      </div>
    </main>
  );
}

/**
 * Turn a failed request into something a person can act on.
 *
 * The 401 stays vague on purpose — the server refuses to say whether the email
 * or the password was wrong, and repeating that distinction here would give
 * back the account enumeration the equal-timing login exists to prevent. The
 * 409 is the opposite: signup cannot hide that an address is taken, because
 * saying so is the endpoint's whole job.
 */
function readable(caught: unknown, signingUp: boolean): string {
  if (caught instanceof ApiError) {
    if (caught.status === 409) {
      return "There is already an account with that email. Sign in instead.";
    }
    if (caught.status === 401) return "That email and password do not match.";
    if (caught.status === 429) {
      return "Too many attempts from here. Try again in a few minutes.";
    }
    if (caught.status === 422) {
      return signingUp
        ? "Check the email address, and that the password meets every requirement."
        : "Check the email address.";
    }
  }
  return "Something went wrong. Try again.";
}
