/**
 * Putting a picture or a video into the draft.
 *
 * Three ways in, one path through: the toolbar's Image button, a paste, and a
 * drop all end at `insertFiles`, which uploads the bytes and inserts a node
 * pointing at what the server stored. Nothing here ever puts a link to someone
 * else's server into the document - see `lib/media.ts` for why that is the
 * rule rather than a preference.
 *
 * The editor arrives after this hook does (`useEditor` needs the paste and drop
 * handlers at construction, and those handlers need to insert), so the editor
 * is held in a ref and `bind` closes the loop. The alternative is putting the
 * upload logic inside the `useEditor` options object, where it re-runs on every
 * render and cannot hold state.
 *
 * Failures are reported, never swallowed. An upload that fails silently looks
 * exactly like a slow one, and the reviewer's next move is to try again - which
 * is the one thing that cannot help.
 */

import { useCallback, useRef, useState } from "react";
import type { Editor } from "@tiptap/react";

import { ApiError } from "@/lib/api/client";
import { uploadMedia } from "@/lib/api/console";
import { youtubeIdFromUrl } from "@/lib/media";

/** Mirrors the server's allowlist, which is enforced on the bytes themselves. */
export const ACCEPTED_IMAGE_TYPES = "image/png,image/jpeg,image/gif,image/webp";

export interface MediaInsert {
  /** True while an upload is in flight - the toolbar disables itself. */
  uploading: boolean;
  /** The last failure, shown under the toolbar until the next attempt. */
  error: string | null;
  /** Hand over the editor once `useEditor` has produced it. */
  bind: (editor: Editor | null) => void;
  /** Upload each image and insert it at the cursor, in the order given. */
  insertFiles: (files: readonly File[]) => Promise<void>;
  /** Insert a YouTube embed. False when the URL yielded no video id. */
  insertYouTube: (url: string) => boolean;
}

export function useMediaInsert(): MediaInsert {
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const editorRef = useRef<Editor | null>(null);

  const bind = useCallback((editor: Editor | null) => {
    editorRef.current = editor;
  }, []);

  const insertFiles = useCallback(async (files: readonly File[]) => {
    const editor = editorRef.current;
    const images = files.filter((file) => file.type.startsWith("image/"));
    if (!editor || images.length === 0) return;

    setError(null);
    setUploading(true);
    try {
      for (const file of images) {
        const { src } = await uploadMedia(file);
        // Asked once per image, and after the upload rather than before, so a
        // failed upload never costs the reviewer an answer. Empty alt is a
        // real answer - it marks the image decorative - which is why the
        // prompt says what an empty one means rather than nagging.
        const alt = window.prompt(
          `Describe "${file.name}" for readers using a screen reader.\n` +
            "Leave this empty if the image is decorative.",
          "",
        );
        editor
          .chain()
          .focus()
          .insertContent({ type: "image", attrs: { src, alt: alt?.trim() ?? "" } })
          .run();
      }
    } catch (failure) {
      setError(describeUploadError(failure));
    } finally {
      setUploading(false);
    }
  }, []);

  const insertYouTube = useCallback((url: string) => {
    const editor = editorRef.current;
    if (!editor) return false;

    const videoId = youtubeIdFromUrl(url);
    if (!videoId) {
      setError(
        "That is not a YouTube link. Paste the address from the browser's bar " +
          "on the video's page, or a youtu.be share link.",
      );
      return false;
    }

    setError(null);
    editor.chain().focus().insertContent({ type: "youtube", attrs: { videoId } }).run();
    return true;
  }, []);

  // No dismiss control: the message clears when the reviewer next tries the
  // thing it is about, which is the only move that can resolve it.
  return { uploading, error, bind, insertFiles, insertYouTube };
}

/**
 * The server's own words where it has any.
 *
 * A 415 says which formats are accepted and a 413 names the size ceiling,
 * both are things the reviewer can act on, and both are lost by a house
 * "Upload failed".
 */
function describeUploadError(failure: unknown): string {
  if (failure instanceof ApiError) {
    const detail = (failure.detail as { detail?: string } | undefined)?.detail;
    if (detail) return detail;
  }
  return failure instanceof Error
    ? `Could not upload the image: ${failure.message}`
    : "Could not upload the image.";
}
