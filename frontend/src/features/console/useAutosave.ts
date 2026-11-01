/**
 * Debounced autosave for the article editor.
 *
 * Writes to edited_content only; original_content is never in the payload and
 * the DB rejects any attempt to change it. That split is what gives the audit
 * trail its meaning — "what the model wrote vs. what went live" is only true
 * if one side genuinely cannot be rewritten.
 *
 * Design notes:
 *   - 800ms debounce: long enough that mid-sentence typing doesn't generate a
 *     request per keystroke, short enough that a reviewer closing the tab
 *     rarely loses a thought.
 *   - Flush on unmount and on `visibilitychange` — the tab-close case is where
 *     lost work is most annoying and least recoverable.
 *   - No validation here. Failing a reviewer's autosave mid-sentence because a
 *     citation is momentarily orphaned would be hostile; validation runs at
 *     approve time, where it blocks publication rather than typing.
 *   - On error the pending document is **kept**, not dropped, and the state
 *     goes to "error" so the UI can block Approve while edits are unsaved.
 *   - Each save names the document it started from. A 412 means another
 *     reviewer saved since: state "conflict", no retry, Approve stays blocked.
 *     Retrying would overwrite their edit with this older copy.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "@/lib/api/client";
import { saveContent } from "@/lib/api/console";
import type { TipTapDoc } from "@/types/api";

const DEBOUNCE_MS = 800;

export type SaveState = "idle" | "saving" | "saved" | "error" | "conflict";

export interface Autosave {
  state: SaveState;
  /** True while an edit has not yet been persisted. Gates Approve. */
  hasUnsavedChanges: boolean;
  schedule: (doc: TipTapDoc) => void;
  /**
   * Persist immediately. Resolves `true` when nothing is left pending.
   *
   * The boolean exists because `hasUnsavedChanges` is React state: a caller
   * that awaits `flush()` still holds the value from its own render and cannot
   * tell whether the save it just awaited actually landed.
   */
  flush: () => Promise<boolean>;
}

/** `loaded` is the server's edited content (null if unedited); undefined while loading. */
export function useAutosave(
  articleId: string,
  loaded: TipTapDoc | null | undefined,
): Autosave {
  const [state, setState] = useState<SaveState>("idle");
  const [hasUnsavedChanges, setHasUnsavedChanges] = useState(false);

  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pending = useRef<TipTapDoc | null>(null);
  const inFlight = useRef(false);
  const base = useRef<TipTapDoc | null>(loaded ?? null);
  const conflicted = useRef(false);

  // A (re)loaded document replaces the editor's content, so it is the new base,
  // and nothing typed before it is still on screen to save. Keeping `pending`
  // here let the older copy overwrite the reload it had conflicted with.
  useEffect(() => {
    if (loaded === undefined) return;
    base.current = loaded;
    conflicted.current = false;
    pending.current = null;
    if (timer.current) clearTimeout(timer.current);
    setHasUnsavedChanges(false);
    setState("idle");
  }, [loaded]);

  const flush = useCallback(async () => {
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
    const doc = pending.current;
    if (!doc) return true;
    // A save is already running; its own completion decides the outcome.
    if (inFlight.current || conflicted.current) return false;

    inFlight.current = true;
    setState("saving");
    try {
      await saveContent(articleId, doc, base.current);
      base.current = doc;
      // Only clear the buffer if nothing newer arrived while in flight.
      if (pending.current === doc) {
        pending.current = null;
        setHasUnsavedChanges(false);
      }
      setState("saved");
      return pending.current === null;
    } catch (error) {
      // Keep `pending` so the next schedule() or flush() retries the edit
      // rather than silently discarding the reviewer's work.
      if (error instanceof ApiError && error.status === 412) {
        conflicted.current = true;
        setState("conflict");
      } else {
        setState("error");
      }
      return false;
    } finally {
      inFlight.current = false;
    }
  }, [articleId]);

  const schedule = useCallback(
    (doc: TipTapDoc) => {
      pending.current = doc;
      setHasUnsavedChanges(true);
      if (conflicted.current) return;
      setState("saving");
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => void flush(), DEBOUNCE_MS);
    },
    [flush],
  );

  useEffect(() => {
    const onVisibilityChange = () => {
      if (document.visibilityState === "hidden") void flush();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      document.removeEventListener("visibilitychange", onVisibilityChange);
      if (timer.current) clearTimeout(timer.current);
      void flush();
    };
  }, [flush]);

  return { state, hasUnsavedChanges, schedule, flush };
}
