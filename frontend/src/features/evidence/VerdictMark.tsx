/**
 * The verdict mark — the single most important pixel in the product.
 *
 * It carries the judgment in **geometry, not colour**: a square that is filled,
 * half-filled, quarter-filled or empty. That is a deliberate reversal of the
 * obvious design, and the reasons are the ones that used to be written on the
 * old coloured badge:
 *
 * 1. **Never colour-only.** A verdict conveyed by hue fails silently for
 *    colour-blind readers and in greyscale screenshots — and this product gets
 *    screenshotted into arguments. Geometry survives both, and the text label
 *    still rides alongside it in every usage.
 * 2. **`no_evidence` is neutral, not alarming.** "We looked and found nothing"
 *    is an honest, useful result. It is an empty square drawn in the same ink
 *    family as the rest — a gap in the literature, not a warning.
 * 3. **A green/amber/red ramp turns the feed into a scoreboard.** Ranking
 *    products by hue is the thing the content rules forbid, and a grid of
 *    traffic lights does it whatever the copy says. Freeing the colour axis is
 *    what lets `subject` colour exist at all (see `./subject.ts`).
 *
 * The fill fraction is not decorative either — it reads as "how much of the
 * claim is supported", which is exactly what `mixed` and `weak` mean.
 */

import type { Verdict } from "@/types/api";
import {
  MARK_MIXED,
  MARK_MIXED_FILL,
  MARK_NO_EVIDENCE,
  MARK_SUPPORTED,
  MARK_WEAK,
  MARK_WEAK_FILL,
  markBox,
} from "./styles";

interface Props {
  verdict: Verdict;
  /**
   * `card` tracks `--ew-mark`, so verdict prominence retunes every feed card
   * from one token. `sm` and `lg` are fixed: dense console rows and the article
   * header have their own optical needs.
   */
  size?: "sm" | "card" | "lg";
}

/**
 * Decorative by design: every caller renders the verdict's wording
 * beside this, so announcing the shape would only duplicate the word.
 */
export function VerdictMark({ verdict, size = "card" }: Props) {
  const box = markBox(size);

  switch (verdict) {
    case "supported":
      return <span aria-hidden className={`${box} ${MARK_SUPPORTED}`} />;

    case "mixed":
      return (
        <span aria-hidden className={`${box} ${MARK_MIXED}`}>
          <span className={MARK_MIXED_FILL} />
        </span>
      );

    case "weak":
      return (
        <span aria-hidden className={`${box} ${MARK_WEAK}`}>
          <span className={MARK_WEAK_FILL} />
        </span>
      );

    case "no_evidence":
      return <span aria-hidden className={`${box} ${MARK_NO_EVIDENCE}`} />;
  }
}
