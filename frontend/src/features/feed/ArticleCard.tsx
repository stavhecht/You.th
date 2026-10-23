/**
 * One tile in the masonry feed.
 *
 * Every field here is derived server-side from the approved article
 * (services/card.py) — there is no card-specific model output, and the picture
 * is the article's own first image rather than a separately-chosen thumbnail.
 * That is what guarantees a tile cannot promise something the article walks
 * back.
 *
 * **The verdict stays on the tile.** The comp's tile carries a category kicker
 * and a headline and nothing else, and that is the one place this
 * implementation departs from it: a feed of health claims where the judgment is
 * only visible after a click is a feed that has made the click the point. The
 * kicker line therefore reads `SUPPORTED · Topical`, with the verdict's scope
 * limit under it and the headline last — the same reading order the previous
 * design used, and the same honest half first.
 */

import { Heart } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import { VERDICT_LABELS } from "@/features/evidence/labels";
import { subjectLabel } from "@/features/evidence/subject";
import { useReader } from "@/features/reader/auth";
import { useSaved } from "@/features/reader/useSaved";
import { useToast } from "@/features/shell/toast";
import {
  TILE,
  TILE_HEADLINE,
  TILE_IMAGE,
  TILE_KICKER,
  TILE_LINK,
  TILE_QUALIFIER,
  TILE_SCRIM,
  TILE_TEXT,
  TILE_TYPESET,
  TILE_TYPESET_HEADLINE,
  TILE_TYPESET_KICKER,
  tileSaveButton,
} from "./styles";
import type { FeedCard } from "@/types/api";

/**
 * The four tile shapes, and why they are chosen from the slug.
 *
 * The comp gives each tile its own aspect ratio, which is what stops the grid
 * reading as a spreadsheet. Random would reshuffle on every render and make the
 * masonry jump; a field on the article would be a layout decision asked of a
 * reviewer who is there to check evidence. Hashing the slug gives each article
 * one stable shape for as long as it exists, decided by nobody.
 */
const RATIOS = ["3 / 4", "1 / 1", "4 / 5", "2 / 3"] as const;

export function tileRatio(slug: string): string {
  let hash = 0;
  for (let index = 0; index < slug.length; index += 1) {
    hash = (hash * 31 + slug.charCodeAt(index)) >>> 0;
  }
  return RATIOS[hash % RATIOS.length]!;
}

export function ArticleCard({ card }: { card: FeedCard }) {
  const { signedIn } = useReader();
  const { isSaved, toggle, pending } = useSaved();
  const flash = useToast();

  const saved = isSaved(card.slug);
  const subject = subjectLabel(card.subject);
  // Bumped on each save so the heart remounts and its pop plays again; zero on
  // first render, so a tile that is already saved does not animate on load.
  const [pops, setPops] = useState(0);

  async function onSave() {
    if (!saved) setPops((count) => count + 1);
    flash(await toggle(card.slug));
  }

  return (
    <div className={TILE} style={{ aspectRatio: tileRatio(card.slug) }}>
      {card.image ? (
        <img
          src={card.image}
          // Empty alt, deliberately: the headline is right there in the scrim
          // and is the link's accessible name, so describing the picture again
          // makes a screen reader read the tile twice. A decorative image with
          // its meaning in adjacent text is exactly the case for `alt=""`.
          alt=""
          loading="lazy"
          className={TILE_IMAGE}
        />
      ) : null}

      {signedIn ? (
        <button
          onClick={onSave}
          disabled={pending}
          aria-label={`${saved ? "Remove" : "Save"} “${card.headline}”`}
          aria-pressed={saved}
          className={tileSaveButton(saved)}
        >
          <Heart
            key={pops}
            aria-hidden
            size={16}
            fill={saved ? "currentColor" : "none"}
            className={pops ? "motion-safe:animate-heart-pop" : undefined}
          />
        </button>
      ) : null}

      {/*
        The whole tile is the link, laid under the scrim so a press anywhere on
        the picture opens the article — everything except the Save button, which
        sits above it. The headline is the accessible name; nothing else in here
        needs to be reachable on its own.
      */}
      <Link to={`/a/${card.slug}`} className={TILE_LINK} aria-label={card.headline}>
        {card.image ? null : (
          <span className={TILE_TYPESET}>
            <span className={TILE_TYPESET_KICKER}>
              {subject ?? VERDICT_LABELS[card.verdict]}
            </span>
            <span className={TILE_TYPESET_HEADLINE}>{card.headline}</span>
          </span>
        )}
      </Link>

      {card.image ? (
        <div className={TILE_SCRIM}>
          <div className={TILE_TEXT}>
            {/*
              No verdict mark on an image tile, and that is a deliberate
              exception to the "geometry carries the verdict" rule rather than
              an oversight. The mark is drawn in the ink ramp; over a scrimmed
              photograph the ink is invisible and a white version would be a
              second mark meaning the same thing in a different colour. The
              wording carries it here — which it can, because on a tile the
              verdict is a word rather than a value to compare across a row.
            */}
            <div className={TILE_KICKER}>
              <span>{VERDICT_LABELS[card.verdict]}</span>
              {subject ? <span>· {subject}</span> : null}
            </div>

            {card.verdictQualifier ? (
              <div className={TILE_QUALIFIER}>{card.verdictQualifier}</div>
            ) : null}

            <div className={TILE_HEADLINE}>{card.headline}</div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

/**
 * `<time>` rather than a plain string, so the machine-readable date survives
 * formatting — this is the element a future Article JSON-LD block reads from.
 */
export function PublishedDate({ iso }: { iso: string }) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return <>{iso}</>;

  return (
    <time dateTime={iso}>
      {date.toLocaleDateString("en-GB", {
        day: "numeric",
        month: "short",
        year: "numeric",
      })}
    </time>
  );
}
