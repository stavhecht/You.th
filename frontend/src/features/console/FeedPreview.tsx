/**
 * How this draft will look on the feed - the real tile, among real neighbours.
 *
 * Two decisions carry the whole component.
 *
 * **It renders the public feed's own `ArticleCard`, not a copy of it.** A
 * console-local tile would be a second definition of a tile, free to drift from
 * the one that ships, and a preview that drifts from what publishes is worse
 * than no preview - it is reassuring instead of useful. The card's data comes
 * from `GET /console/articles/:id/card`, which runs the same `derive_card()`
 * the publish path runs, so neither half is a reimplementation.
 *
 * The import direction is console → feed. `App.tsx`'s rule is that *public*
 * routes must never import from `features/console`; this is the other way, and
 * it is the direction that makes the preview honest. Every hook `ArticleCard`
 * needs resolves here: `App.tsx` wraps both route groups in
 * `ReaderAuthProvider > AuthProvider > ToastProvider`, and `main.tsx` mounts
 * `QueryClientProvider > BrowserRouter` above everything.
 *
 * **Neighbours come from the live feed.** Scale is comparative. Whether a
 * headline survives three lines of clamp, whether a picture reads at 200px,
 * whether the verdict kicker holds against a photograph - none of that is
 * answerable from a tile on its own, which is why this is an overlay showing a
 * few cells of the real grid rather than a swatch in the sidebar.
 *
 * Every tile is `inert`. `ArticleCard` renders a live Save control and wraps
 * itself in a link to `/a/{slug}` that resolves to nothing until the draft is
 * published, and a preview must not be pressable into either.
 *
 * **The one live control is Redraw tile**, and it is here rather than in the
 * editor's toolbar because this is the only screen where its effect is visible.
 * A button that spends money and changes nothing you can see is a button
 * reviewers press twice. It redraws the cover alone - the article's own picture
 * is untouched, which is the entire point of the two being separable.
 */

import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";

import { ArticleCard } from "@/features/feed/ArticleCard";
import { consoleKeys, fetchCardPreview } from "@/lib/api/console";
import { feedKeys, fetchFeed } from "@/lib/api/feed";
import {
  MEDIA_ERROR,
  PREVIEW_ACTIONS,
  PREVIEW_CLOSE,
  PREVIEW_GRID,
  PREVIEW_HEAD,
  PREVIEW_MESSAGE,
  PREVIEW_MINE,
  PREVIEW_MINE_TAG,
  PREVIEW_NOTE,
  PREVIEW_OVERLAY,
  PREVIEW_PANEL,
  PREVIEW_TITLE,
  TOP_BAR_ACTION,
} from "./styles";
import { useIllustration } from "./useIllustration";
import type { CardPreview, FeedCard } from "@/types/api";

/** How many published tiles to stand the draft among. */
const NEIGHBOURS = 11;

/** Where the draft's own tile sits in the grid - a row down, not first. */
const MY_POSITION = 3;

interface Props {
  articleId: string;
  onClose: () => void;
}

export function FeedPreview({ articleId, onClose }: Props) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const illustration = useIllustration(articleId);

  const preview = useQuery({
    queryKey: consoleKeys.card(articleId),
    queryFn: () => fetchCardPreview(articleId),
    // Never reuse a tile derived before the reviewer's last edit. The whole
    // value of this panel is that it is current; a cached one is a screenshot.
    staleTime: 0,
    gcTime: 0,
  });

  // Published tiles for scale. Unauthenticated, so it works from the desk, and
  // a failure here costs the neighbours rather than the preview.
  const neighbours = useQuery({
    queryKey: [...feedKeys.all, "preview-neighbours"] as const,
    queryFn: () => fetchFeed({ limit: NEIGHBOURS, personalise: false }),
    staleTime: 5 * 60 * 1000,
  });

  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const card = preview.data;
  const others = (neighbours.data?.items ?? []).filter(
    (item) => item.slug !== card?.slug,
  );

  /**
   * Redraw the cover, then re-derive the tile from the server.
   *
   * No editor is passed and none is needed: the cover is not in the document.
   * That is what makes this the cheap, self-contained half of the pair - the
   * article the reviewer has been editing is not touched, so there is nothing
   * to flush and nothing that can be clobbered.
   */
  async function redrawTile() {
    if (await illustration.regenerate("tile")) await preview.refetch();
  }

  return (
    <div
      className={PREVIEW_OVERLAY}
      // Clicking the scrim closes; clicking inside the panel must not, so the
      // handler checks the press landed on the scrim itself.
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="How this draft looks on the feed"
        className={PREVIEW_PANEL}
      >
        <div className={PREVIEW_HEAD}>
          <h2 className={PREVIEW_TITLE}>On the feed</h2>
          <div className={PREVIEW_ACTIONS}>
            {/*
              Only offered when a new cover would actually show. With no
              generated imagery there is no other frame to keep and the server
              answers 409; with the pairing already broken by a reviewer's own
              picture, a redraw would succeed, bill a render, and change
              nothing on screen. Both are states the note below explains.
            */}
            {card?.imageIsGeneratedCover ? (
              <button
                type="button"
                onClick={() => void redrawTile()}
                disabled={illustration.working !== null}
                title="Draw this tile's picture again, with a new seed. One render; the article's own picture is untouched."
                className={TOP_BAR_ACTION}
              >
                {illustration.working === "tile" ? "Generating…" : "Redraw tile"}
              </button>
            ) : null}
            <button ref={closeRef} onClick={onClose} className={PREVIEW_CLOSE}>
              Close ✕
            </button>
          </div>
        </div>

        {illustration.error ? (
          <p role="alert" className={MEDIA_ERROR}>
            {illustration.error}
          </p>
        ) : null}

        {card ? (
          <>
            <div className={PREVIEW_GRID}>
              {interleave(card, others).map((entry) =>
                entry.mine ? (
                  <div key="mine" inert className={PREVIEW_MINE}>
                    <span className={PREVIEW_MINE_TAG}>This draft</span>
                    <ArticleCard card={entry.card} />
                  </div>
                ) : (
                  <div key={entry.card.slug} inert aria-hidden>
                    <ArticleCard card={entry.card} />
                  </div>
                ),
              )}
            </div>
            <p className={PREVIEW_NOTE}>
              {describePicture(card)} Not published yet. The tiles around it are
              live, and shown at the width the feed gives them.
            </p>
          </>
        ) : (
          <p className={PREVIEW_MESSAGE}>
            {preview.status === "error"
              ? "Could not derive the tile for this draft."
              : "Deriving the tile…"}
          </p>
        )}
      </div>
    </div>
  );
}

/** Says which of the tile's three picture states this is, and what breaks it. */
function describePicture(card: CardPreview): string {
  if (card.image === null) {
    return "No picture, so the feed draws a typographic tile. That is the design's resting state, not a missing image.";
  }
  if (card.imageIsGeneratedCover) {
    return "The tile shows the portrait frame drawn for this article. Redrawing it here leaves the article's own picture alone. Replace or remove that picture in the editor and the pairing breaks, so the tile falls back to the article's own first image.";
  }
  return "The tile shows the article's own first picture, cropped to fit, so it is changed by changing the article, not the tile.";
}

type Entry = { card: FeedCard; mine: boolean };

/**
 * Put the draft's tile a row into the grid rather than first.
 *
 * First position is the one place a tile never has to hold its own - there is
 * nothing above it and nothing beside it yet. A few cells in is where the
 * question actually gets answered.
 */
function interleave(mine: CardPreview, others: FeedCard[]): Entry[] {
  const at = Math.min(MY_POSITION, others.length);
  return [
    ...others.slice(0, at).map((card) => ({ card, mine: false })),
    { card: mine, mine: true },
    ...others.slice(at).map((card) => ({ card, mine: false })),
  ];
}
