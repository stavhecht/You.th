/**
 * "Let us know" — suggest a topic to cover, or send anything else.
 *
 * The submission lands in `contact_requests` and is read in the review desk's
 * inbox. It does **not** create a pipeline run: nothing reaches generation
 * because a stranger asked for it, which is the same principle as invariant #1
 * pointed at the other end of the pipeline. The copy says so rather than
 * implying an article is on its way.
 *
 * Name and email are required — without a way to reply, a request we can
 * answer looks exactly like one we cannot. A topic needs a link or a
 * description (either will do); anything else needs a description.
 */

import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { submitContact } from "@/lib/api/reader";
import { EMAIL_HINT, isEmail } from "@/lib/email";
import { useReader } from "@/features/reader/auth";
import { ApiError } from "@/lib/api/client";
import {
  CHIP_ROW,
  COLLECTION_NOTICE,
  DONE_ACTION,
  DONE_BODY,
  DONE_CARD,
  DONE_TITLE,
  FIELD,
  FIELD_ERROR,
  FIELD_LABEL,
  FORM_ERROR,
  FORM_GRID,
  FORM_NOTE,
  PAGE_BACK_LINK,
  PAGE_KICKER,
  PAGE_STANDFIRST,
  PAGE_TITLE,
  SUBMIT_BUTTON,
  TEXTAREA,
  WIDE_PAGE,
  chip,
} from "@/features/shell/pageStyles";
import type { ContactKind } from "@/types/api";

/** The mark on a required label; the input's `required` is what a screen reader hears. */
const REQUIRED_MARK = (
  <span aria-hidden="true" className="text-accent-ink">
    {" *"}
  </span>
);

/** Labels and field messages sit over the fields' straight edge, not their rounded corner. */
const INDENT = "pl-[10px]";
const LABEL = `${FIELD_LABEL} ${INDENT}`;
const ERROR = `${FIELD_ERROR} ${INDENT}`;

const NAME_HINT = "Tell us your name so we know who is asking.";
const EMAIL_MISSING = "We need your email address to reply.";
const LINK_HINT = "Paste the full link, starting with https://";
const NOTE_MISSING = "Tell us what this is about.";
// A topic needs one of the two; both fields say so when both are empty.
const LINK_OR_NOTE = "Add a link, or describe the topic below.";
const NOTE_OR_LINK = "Describe what we should look into, or add a link above.";

/** An empty email and a malformed one are different mistakes. */
function emailMessage(input: HTMLInputElement): string | null {
  if (!input.validationMessage) return null;
  return input.value.trim() ? EMAIL_HINT : EMAIL_MISSING;
}

/** The server's own reason for a 409 ("that link is too long"), as a sentence. */
function refusalReason(caught: unknown): string | null {
  if (!(caught instanceof ApiError) || caught.status !== 409) return null;
  const reason = (caught.detail as { detail?: unknown } | undefined)?.detail;
  return typeof reason === "string" && reason
    ? `${reason.charAt(0).toUpperCase()}${reason.slice(1)}.`
    : null;
}

// `fact_check` stays in ContactKind: older inbox rows carry it.
const KINDS: { value: ContactKind; label: string }[] = [
  { value: "topic", label: "Suggest a topic" },
  { value: "other", label: "Something else" },
];

export function ContactRoute() {
  const { reader } = useReader();
  const [kind, setKind] = useState<ContactKind>("topic");
  const [nameError, setNameError] = useState<string | null>(null);
  const [emailError, setEmailError] = useState<string | null>(null);
  const [linkError, setLinkError] = useState<string | null>(null);
  const [noteError, setNoteError] = useState<string | null>(null);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    // The form is `noValidate`, so this is the browser's check without its
    // popup: each invalid field fires `invalid`, and its handler shows the
    // message under it instead.
    const valid = event.currentTarget.checkValidity();

    const form = new FormData(event.currentTarget);
    const link = String(form.get("link") ?? "").trim();
    const note = String(form.get("note") ?? "").trim();
    if (kind === "topic" && !link && !note) {
      setLinkError(LINK_OR_NOTE);
      setNoteError(NOTE_OR_LINK);
      return;
    }
    if (!valid) return;

    setBusy(true);
    try {
      await submitContact({
        kind,
        name: String(form.get("name") ?? "").trim(),
        email: String(form.get("email") ?? "").trim(),
        link: link || null,
        note,
      });
      setSent(true);
      window.scrollTo(0, 0);
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.status === 429
          ? "That is a lot of messages from one place. Try again in a few minutes."
          : (refusalReason(caught) ??
              "Could not send that. Check the email address and try again."),
      );
    } finally {
      setBusy(false);
    }
  }

  if (sent) {
    return (
      <main className={WIDE_PAGE}>
        <Link to="/" className={PAGE_BACK_LINK}>
          ← Feed
        </Link>
        <div className={DONE_CARD}>
          <p className={DONE_TITLE}>Got it — thank you.</p>
          <p className={DONE_BODY}>
            Your message is with the editorial team. A person reads every one.
            If a study exists either way, you will hear back at the address you
            gave us.
          </p>
          <button onClick={() => setSent(false)} className={DONE_ACTION}>
            Send another
          </button>
        </div>
      </main>
    );
  }

  return (
    <main className={WIDE_PAGE}>
      <Link to="/" className={PAGE_BACK_LINK}>
        ← Feed
      </Link>

      <header>
        <div className={PAGE_KICKER}>Let us know</div>
        <h1 className={PAGE_TITLE}>Saw something that needs checking?</h1>
        <p className={PAGE_STANDFIRST}>
          Send us a claim, a video, or a topic you want covered. Requests go to the
          editorial team — they do not start an article on their own, and a person
          decides what gets researched.
        </p>
      </header>

      <form onSubmit={onSubmit} noValidate className="mt-8 max-w-[640px]">
        <div className={LABEL}>What is this about?</div>
        <div className={`${CHIP_ROW} mb-7`}>
          {KINDS.map((option) => (
            <button
              key={option.value}
              type="button"
              onClick={() => setKind(option.value)}
              aria-pressed={kind === option.value}
              className={chip(kind === option.value)}
            >
              {option.label}
            </button>
          ))}
        </div>

        <div className={`${FORM_GRID} mb-3.5`}>
          <label>
            <span className={LABEL}>Your name{REQUIRED_MARK}</span>
            <input
              name="name"
              required
              maxLength={80}
              defaultValue={reader?.displayName ?? ""}
              placeholder="So we know who is asking"
              autoComplete="name"
              aria-invalid={nameError ? true : undefined}
              aria-describedby={nameError ? "contact-name-error" : undefined}
              // Spaces alone pass `required` but not the API, so they are invalid too.
              onInput={(event) => {
                const value = event.currentTarget.value;
                event.currentTarget.setCustomValidity(value && !value.trim() ? NAME_HINT : "");
                if (value.trim()) setNameError(null);
              }}
              onBlur={(event) =>
                setNameError(event.currentTarget.value.trim() ? null : NAME_HINT)
              }
              onInvalid={() => setNameError(NAME_HINT)}
              className={FIELD}
            />
            {nameError ? (
              <span id="contact-name-error" className={ERROR}>
                {nameError}
              </span>
            ) : null}
          </label>
          <label>
            <span className={LABEL}>Email{REQUIRED_MARK}</span>
            <input
              name="email"
              type="email"
              required
              defaultValue={reader?.email ?? ""}
              placeholder="so we can reply"
              autoComplete="email"
              aria-invalid={emailError ? true : undefined}
              aria-describedby={emailError ? "contact-email-error" : undefined}
              // The custom validity makes `checkValidity()` refuse an address the
              // API would; the inline hint shows it on leaving the field.
              onInput={(event) => {
                const value = event.currentTarget.value;
                const message = value.trim() && !isEmail(value) ? EMAIL_HINT : "";
                event.currentTarget.setCustomValidity(message);
                if (!message) setEmailError(null);
              }}
              onBlur={(event) => setEmailError(emailMessage(event.currentTarget))}
              onInvalid={(event) => setEmailError(emailMessage(event.currentTarget))}
              className={FIELD}
            />
            {emailError ? (
              <span id="contact-email-error" className={ERROR}>
                {emailError}
              </span>
            ) : null}
          </label>
        </div>

        {/* A topic needs a link or a description, either one: the post or video
            that prompted it is often the fastest way to see what people are told. */}
        {kind === "topic" ? (
          <>
            <p className="mb-3.5 font-body text-[12.5px] text-ink-3">
              Add a link, a description, or both. At least one is needed.
            </p>
            <label className="mb-3.5 block">
              <span className={LABEL}>Link to the content{REQUIRED_MARK}</span>
              <input
                name="link"
                type="url"
                maxLength={2000}
                placeholder="A post, video or article URL"
                aria-invalid={linkError ? true : undefined}
                aria-describedby={linkError ? "contact-link-error" : undefined}
                onInput={(event) => {
                  if (!event.currentTarget.validity.valid) return;
                  setLinkError(null);
                  if (event.currentTarget.value.trim() && noteError === NOTE_OR_LINK) {
                    setNoteError(null);
                  }
                }}
                onBlur={(event) =>
                  setLinkError(event.currentTarget.validity.valid ? null : LINK_HINT)
                }
                onInvalid={() => setLinkError(LINK_HINT)}
                className={FIELD}
              />
              {linkError ? (
                <span id="contact-link-error" className={ERROR}>
                  {linkError}
                </span>
              ) : null}
            </label>
          </>
        ) : null}

        <label className="mb-5 block">
          <span className={LABEL}>
            {kind === "topic" ? "What should we look into?" : "Tell us more"}
            {REQUIRED_MARK}
          </span>
          <textarea
            name="note"
            required={kind !== "topic"}
            maxLength={4000}
            rows={5}
            placeholder="Describe in a sentence or two what you want us to look into"
            aria-invalid={noteError ? true : undefined}
            aria-describedby={noteError ? "contact-note-error" : undefined}
            onInput={(event) => {
              if (!event.currentTarget.value.trim()) return;
              setNoteError(null);
              if (linkError === LINK_OR_NOTE) setLinkError(null);
            }}
            onInvalid={() => setNoteError(NOTE_MISSING)}
            className={TEXTAREA}
          />
          {noteError ? (
            <span id="contact-note-error" className={ERROR}>
              {noteError}
            </span>
          ) : null}
        </label>

        <button type="submit" disabled={busy} className={SUBMIT_BUTTON}>
          {busy ? "Sending…" : "Send it over →"}
        </button>

        {error ? (
          <p role="alert" className={FORM_ERROR}>
            {error}
          </p>
        ) : null}

        <p className={FORM_NOTE}>
          We read everything. We answer the ones we can check against published
          research.
        </p>

        {/* Notice at collection — PPL s.11, GDPR Art 13. Asks the sender to
            leave health details out because the note is free text that the
            editorial team reads, and nothing it is for needs them. */}
        <p className={COLLECTION_NOTICE}>
          Your email, name and message are read only by our editorial team, to
          consider your request and reply. Please do not include details about your
          own health. See the{" "}
          <Link to="/privacy" className="underline">
            privacy policy
          </Link>{" "}
          for how long we keep messages and how to have yours deleted.
        </p>
      </form>
    </main>
  );
}
