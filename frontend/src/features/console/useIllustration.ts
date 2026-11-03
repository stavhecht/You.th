/**
 * Regenerating an article's generated pictures - either of them, or both.
 *
 * An article has two: the `lead` in its body, and the portrait `cover` that
 * only ever appears on the feed tile. They are seen in different places and
 * judged separately, so this hook exposes three targets rather than one button.
 * `picture` and `tile` cost one render each; `both` costs two and gives a pair
 * drawn from one seed.
 *
 * The server draws them and records them on the article, then hands both back.
 * **It does not touch the document** - `original_content` is trigger-protected
 * and `edited_content` is this editor's live buffer, so a server-side rewrite
 * would either fail or clobber whatever the reviewer is typing. The swap
 * happens here instead, which has a second benefit: the new `src` reaches the
 * server through the ordinary autosave, so it passes the same media check as
 * any other edit.
 *
 * The existing node's `width` and `align` are preserved. A reviewer who sized
 * and wrapped a picture was making a layout decision about the article, not
 * about that particular render, and resetting it on every press would make the
 * button feel destructive.
 *
 * If the document has no picture - the reviewer deleted it, or the pipeline
 * never made one - the new lead is inserted at the top rather than refused.
 * That is also what re-pairs the tile: the cover is only used while the
 * document's first image is the lead it was drawn beside.
 */

import { useCallback, useState } from "react";
import type { Editor } from "@tiptap/react";

import { ApiError } from "@/lib/api/client";
import { regenerateIllustration } from "@/lib/api/console";
import type { IllustrationFrame } from "@/types/api";

/** What a press means, in the reviewer's words rather than the wire's. */
export type RegenerateTarget = "picture" | "tile" | "both";

const FRAMES: Record<RegenerateTarget, IllustrationFrame[]> = {
  picture: ["lead"],
  tile: ["cover"],
  both: ["lead", "cover"],
};

export interface Illustration {
  /** Which press is in flight, so only that button says so. */
  working: RegenerateTarget | null;
  error: string | null;
  /**
   * Returns whether the article changed, so a caller showing the result - the
   * feed preview - knows when to refetch. An editor is required for any target
   * that redraws the lead; see the guard below for why.
   */
  regenerate: (target: RegenerateTarget, editor?: Editor | null) => Promise<boolean>;
}

export function useIllustration(articleId: string): Illustration {
  const [working, setWorking] = useState<RegenerateTarget | null>(null);
  const [error, setError] = useState<string | null>(null);

  const regenerate = useCallback(
    async (target: RegenerateTarget, editor?: Editor | null) => {
      if (!articleId) return false;

      const frames = FRAMES[target];
      const touchesDocument = frames.includes("lead");
      // A new lead is written to the article row by the server and into the
      // document by us. Doing the first without the second leaves the row
      // naming a picture the document does not contain, which breaks the
      // pairing rule and silently drops the tile back to typographic. Refusing
      // is the safe half: nothing is drawn, so nothing is billed either.
      if (touchesDocument && !editor) return false;

      setWorking(target);
      setError(null);
      try {
        const { lead } = await regenerateIllustration(articleId, frames);
        if (touchesDocument && editor) swapLeadImage(editor, lead.src, lead.alt);
        return true;
      } catch (failure) {
        setError(describeError(failure));
        return false;
      } finally {
        setWorking(null);
      }
    },
    [articleId],
  );

  // No dismiss control, matching `useMediaInsert`: the message clears when the
  // reviewer next tries the thing it is about.
  return { working, error, regenerate };
}

/** Point the document's first picture at the new render, or add one. */
function swapLeadImage(editor: Editor, src: string, alt: string): void {
  const { state } = editor;

  let target: number | null = null;
  state.doc.descendants((node, pos) => {
    if (target !== null) return false;
    if (node.type.name === "image") {
      target = pos;
      return false;
    }
    return true;
  });

  if (target === null) {
    editor.chain().focus().insertContentAt(0, { type: "image", attrs: { src, alt } }).run();
    return;
  }

  const existing = state.doc.nodeAt(target);
  if (!existing) return;
  // setNodeMarkup over updateAttributes: this edits a node by position rather
  // than by whatever happens to be selected, so it works while the reviewer's
  // cursor is in a paragraph. Spreading the old attrs keeps width and align.
  editor.view.dispatch(
    state.tr.setNodeMarkup(target, undefined, { ...existing.attrs, src, alt }),
  );
}

/**
 * The server's own words where it has any.
 *
 * A 429 names the wait, a 503 says the key is missing, a 502 carries the
 * provider's message, and a 409 on a one-frame press says there is no other
 * frame to keep. All four are things a reviewer can act on, and all four are
 * lost by a house "Could not regenerate".
 */
function describeError(failure: unknown): string {
  if (failure instanceof ApiError) {
    const detail = (failure.detail as { detail?: string } | undefined)?.detail;
    if (detail) return detail;
  }
  return failure instanceof Error
    ? `Could not regenerate the picture: ${failure.message}`
    : "Could not regenerate the picture.";
}
