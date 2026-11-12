/**
 * The Save control's state and its two mutations.
 *
 * One query for a whole page of tiles, not one per tile: `/readers/saved/slugs`
 * returns `{ slug: folderId }` for everything on the shelf, which is a
 * human-sized list by construction. A per-card query would be forty requests
 * for a feed of forty and would refetch all of them on every toggle.
 *
 * Signed out, `isSaved` is always false and `toggle` reports what is missing
 * rather than failing. Saving is the main reason to have an account, so the
 * control stays visible and becomes the prompt - hiding it would remove the
 * one place the reason for signing up is obvious.
 */

import { useCallback, useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useReader } from "./auth";
import {
  fetchSavedIndex,
  readerKeys,
  saveArticle,
  unsaveArticle,
} from "@/lib/api/reader";
import type { SavedIndex } from "@/types/api";

export interface SavedState {
  index: SavedIndex;
  isSaved: (slug: string) => boolean;
  /** Which folder a slug is on, or null. Drives the article page's picker. */
  folderOf: (slug: string) => string | null;
  /** Save, unsave, or move. Returns what to tell the reader. */
  toggle: (slug: string, folderId?: string) => Promise<string>;
  pending: boolean;
}

export function useSaved(): SavedState {
  const { signedIn } = useReader();
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: readerKeys.savedIndex,
    queryFn: fetchSavedIndex,
    enabled: signedIn,
    // The shelf only changes through this hook's own mutations, which
    // invalidate it directly. Refetching on focus would spend a request per
    // tab switch to learn nothing.
    staleTime: 5 * 60_000,
  });

  // Memoised: a fresh `{}` per render made isSaved, folderOf and toggle new
  // functions on every render, so every tile using them re-rendered too.
  const index = useMemo(() => query.data ?? {}, [query.data]);

  const mutation = useMutation({
    mutationFn: async ({
      slug,
      folderId,
      saved,
    }: {
      slug: string;
      folderId?: string;
      saved: boolean;
    }) => {
      if (saved) await unsaveArticle(slug);
      else await saveArticle(slug, folderId);
    },
    // Every saved query at once - the index, the shelf, and the folder counts
    // above it all move together, and invalidating them separately is how a
    // tab label ends up disagreeing with the grid under it.
    onSettled: () => queryClient.invalidateQueries({ queryKey: readerKeys.saved }),
  });

  const isSaved = useCallback((slug: string) => slug in index, [index]);
  const folderOf = useCallback((slug: string) => index[slug] ?? null, [index]);

  const toggle = useCallback(
    async (slug: string, folderId?: string) => {
      if (!signedIn) return "Create an account to keep this";

      const current = index[slug];
      const saved = current !== undefined;
      // Re-saving into a different folder is a move, not an unsave. Without
      // this the folder picker on an article page would silently remove the
      // article the first time it was used.
      const moving = saved && folderId !== undefined && folderId !== current;

      if (moving) {
        await mutation.mutateAsync({ slug, folderId, saved: false });
        return "Moved";
      }

      await mutation.mutateAsync({ slug, folderId, saved });
      return saved ? "Removed from your folders" : "Saved";
    },
    [index, mutation, signedIn],
  );

  return { index, isSaved, folderOf, toggle, pending: mutation.isPending };
}
