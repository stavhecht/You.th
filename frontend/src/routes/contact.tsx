/**
 * "Let us know" — send a claim to check, or a topic to cover.
 *
 * The submission lands in `contact_requests` and is read in the review desk's
 * inbox. It does **not** create a pipeline run: nothing reaches generation
 * because a stranger asked for it, which is the same principle as invariant #1
 * pointed at the other end of the pipeline. The copy says so rather than
 * implying an article is on its way.
 *
 * Email is required even though the form calls the name optional — without a
 * way to reply, a request we can answer looks exactly like one we cannot.
 */

import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { submitContact } from "@/lib/api/reader";
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

const KINDS: { value: ContactKind; label: string }[] = [
  { value: "fact_check", label: "Fact-check a claim" },
  { value: "topic", label: "Suggest a topic" },
  { value: "other", label: "Something else" },
];

export function ContactRoute() {
  const { reader } = useReader();
  const [kind, setKind] = useState<ContactKind>("fact_check");
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);

    const form = new FormData(event.currentTarget);
    setBusy(true);
    try {
      await submitContact({
        kind,
        name: String(form.get("name") ?? "").trim() || null,
        email: String(form.get("email") ?? "").trim(),
        link: String(form.get("link") ?? "").trim() || null,
        note: String(form.get("note") ?? "").trim(),
      });
      setSent(true);
      window.scrollTo(0, 0);
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.status === 429
          ? "That is a lot of messages from one place. Try again in a few minutes."
          : "Could not send that. Check the email address and try again.",
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

      <div className={PAGE_KICKER}>Let us know</div>
      <h1 className={PAGE_TITLE}>Saw something that needs checking?</h1>
      <p className={PAGE_STANDFIRST}>
        Send us a claim, a video, or a topic you want covered. Requests go to the
        editorial team — they do not start an article on their own, and a person
        decides what gets researched.
      </p>

      <form onSubmit={onSubmit} className="mt-7 max-w-[640px]">
        <div className={FIELD_LABEL}>What is this about?</div>
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
            <span className={FIELD_LABEL}>Your name</span>
            <input
              name="name"
              defaultValue={reader?.displayName ?? ""}
              placeholder="Optional"
              autoComplete="name"
              className={FIELD}
            />
          </label>
          <label>
            <span className={FIELD_LABEL}>Email</span>
            <input
              name="email"
              type="email"
              required
              defaultValue={reader?.email ?? ""}
              placeholder="so we can reply"
              autoComplete="email"
              className={FIELD}
            />
          </label>
        </div>

        {/* Only asked for where it means something. A "link" field on a topic
            suggestion is a field most people leave blank and some people fill
            with the wrong thing. */}
        {kind === "fact_check" ? (
          <label className="mb-3.5 block">
            <span className={FIELD_LABEL}>Link to the content</span>
            <input
              name="link"
              type="url"
              placeholder="Paste a post, video or article URL"
              className={FIELD}
            />
          </label>
        ) : null}

        <label className="mb-5 block">
          <span className={FIELD_LABEL}>
            {kind === "topic" ? "What should we look into?" : "Tell us more"}
          </span>
          <textarea
            name="note"
            required
            rows={5}
            placeholder={
              kind === "fact_check"
                ? "What is the claim, and where did you see it?"
                : "A sentence or two is plenty."
            }
            className={TEXTAREA}
          />
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
