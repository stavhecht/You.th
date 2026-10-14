/**
 * The feed's narrowing, read from and written to the URL.
 *
 * One module owns the parameter names, so the header that writes `?q=` and the
 * feed that reads it cannot drift apart — the failure mode being a search box
 * that appears to do nothing.
 *
 * Everything here lives in the query string rather than in React state because
 * all of it is worth bookmarking, sharing and reaching with the back button.
 * The drawer's open/closed deliberately does not — it is ephemeral UI state
 * nobody wants a history entry for, so `SiteHeader` keeps it in a `useState`
 * and passes it down. Putting it in this hook would give every caller its own
 * disconnected copy.
 *
 *   ?q=          server-side search over every published article
 *   ?verdict=    server-side filter
 *   ?subject=    server-side filter
 *   ?plain=1     turn off a signed-in reader's interest ordering
 */

import { useCallback } from "react";
import { useSearchParams } from "react-router-dom";

import { SUBJECTS } from "@/features/evidence/subject";
import type { Subject, Verdict } from "@/types/api";

const VERDICTS: readonly Verdict[] = ["supported", "mixed", "weak", "no_evidence"];

export interface BrowseState {
  query: string;
  verdict: Verdict | null;
  subject: Subject | null;
  /** True when the reader has asked for the plain chronological feed. */
  plain: boolean;
  /** Any narrowing at all — drives the "Clear" affordance. */
  filtered: boolean;
  setVerdict: (verdict: Verdict | null) => void;
  setSubject: (subject: Subject | null) => void;
  setPlain: (plain: boolean) => void;
  clear: () => void;
}

export function useBrowseState(): BrowseState {
  const [params, setParams] = useSearchParams();

  const query = params.get("q") ?? "";
  // Validated against the enum rather than cast: these come from the URL, so
  // `?verdict=<script>` is a thing a stranger can send someone. An unknown
  // value reads as "no filter", which is the safe interpretation and also the
  // one that keeps a mistyped link working.
  const verdict = asMember(params.get("verdict"), VERDICTS);
  const subject = asMember(params.get("subject"), SUBJECTS);
  const plain = params.get("plain") === "1";

  const patch = useCallback(
    (key: string, value: string | null) => {
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          if (value) next.set(key, value);
          else next.delete(key);
          return next;
        },
        // Replace, not push: narrowing a feed is refining one view rather than
        // moving to a new one, and pushing would make Back walk through every
        // chip the reader tried instead of leaving the page.
        { replace: true },
      );
    },
    [setParams],
  );

  return {
    query,
    verdict,
    subject,
    plain,
    filtered: Boolean(query || verdict || subject),
    setVerdict: useCallback((value) => patch("verdict", value), [patch]),
    setSubject: useCallback((value) => patch("subject", value), [patch]),
    setPlain: useCallback((value) => patch("plain", value ? "1" : null), [patch]),
    clear: useCallback(
      () => setParams(new URLSearchParams(), { replace: true }),
      [setParams],
    ),
  };
}

/** Narrow a raw URL value to a known enum member, or null. */
function asMember<T extends string>(value: string | null, allowed: readonly T[]): T | null {
  return value && (allowed as readonly string[]).includes(value) ? (value as T) : null;
}
