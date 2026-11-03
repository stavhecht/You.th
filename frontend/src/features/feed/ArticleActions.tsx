/**
 * Save to a folder, and share.
 *
 * The folder picker is a native `<select>` restyled as a pill rather than a
 * custom listbox. It is a short list of short strings on a page where nothing
 * else is a dropdown, and the native control brings keyboard handling, mobile
 * pickers and a typeahead that a bespoke one would have to reimplement to be as
 * good.
 *
 * Signed out, the controls stay and become the prompt: saving is the main
 * reason to have an account here, and hiding the button removes the one place
 * where that reason is obvious.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";

import { useReader } from "@/features/reader/auth";
import { useSaved } from "@/features/reader/useSaved";
import { useToast } from "@/features/shell/toast";
import { fetchFolders, readerKeys } from "@/lib/api/reader";
import {
  ARTICLE_ACTIONS,
  FOLDER_SELECT,
  FOLDER_SELECT_CHEVRON,
  FOLDER_SELECT_WRAP,
  PRIMARY_PILL,
  SECONDARY_PILL,
} from "./styles";

export function ArticleActions({
  slug,
  headline,
}: {
  slug: string;
  headline: string;
}) {
  const { signedIn } = useReader();
  const { isSaved, folderOf, toggle, pending } = useSaved();
  const flash = useToast();
  const navigate = useNavigate();

  const { data: folders = [] } = useQuery({
    queryKey: readerKeys.folders,
    queryFn: fetchFolders,
    enabled: signedIn,
    staleTime: 5 * 60_000,
  });

  const saved = isSaved(slug);
  // Uncontrolled until the reader touches it, then controlled. The default is
  // wherever the article already sits, so the picker reports the truth rather
  // than offering to move it somewhere on first render.
  const [chosen, setChosen] = useState<string | null>(null);
  const current = folderOf(slug);
  const target = chosen ?? current ?? folders[0]?.id;
  const moving = saved && target !== undefined && target !== current;

  return (
    <div className={ARTICLE_ACTIONS}>
      {signedIn && folders.length > 0 ? (
        <span className={FOLDER_SELECT_WRAP}>
          <select
            value={target}
            onChange={(event) => setChosen(event.target.value)}
            aria-label="Folder to save into"
            className={FOLDER_SELECT}
          >
            {folders.map((folder) => (
              <option key={folder.id} value={folder.id}>
                {folder.name}
              </option>
            ))}
          </select>
          <span aria-hidden className={FOLDER_SELECT_CHEVRON} />
        </span>
      ) : null}

      {/*
        The label has to name what the press will actually do. Picking a
        different folder while the article is already saved turns the action
        from a removal into a move, and a button still reading "Remove" at that
        moment is a control that lies about itself.
      */}
      <button
        onClick={async () => flash(await toggle(slug, target))}
        disabled={pending}
        className={PRIMARY_PILL}
      >
        {!signedIn
          ? "Save this"
          : !saved
            ? "Save to folder"
            : moving
              ? "Move it here"
              : "Remove from folder"}
      </button>

      <button
        onClick={() => void share(slug, headline, flash)}
        className={SECONDARY_PILL}
      >
        Share with a friend
      </button>

      {!signedIn ? (
        <button onClick={() => navigate("/join")} className={SECONDARY_PILL}>
          Create an account
        </button>
      ) : null}
    </div>
  );
}

/**
 * The platform share sheet where there is one, the clipboard where there is not.
 *
 * `navigator.share` is the right thing on a phone and absent on most desktops;
 * `clipboard.writeText` needs a secure context and can be refused outright.
 * Every branch ends in the toast saying what actually happened - a share button
 * that silently does nothing is worse than one that says it could not.
 */
async function share(
  slug: string,
  headline: string,
  flash: (message: string) => void,
): Promise<void> {
  const url = `${window.location.origin}/a/${slug}`;

  if (navigator.share) {
    try {
      await navigator.share({ title: headline, url });
      return;
    } catch {
      // Includes the reader simply dismissing the sheet, which is not a
      // failure worth a message - fall through to the clipboard only if the
      // sheet was never usable.
      return;
    }
  }

  try {
    await navigator.clipboard.writeText(url);
    flash("Link copied");
  } catch {
    flash("Copy the address bar to share this");
  }
}
