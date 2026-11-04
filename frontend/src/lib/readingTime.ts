/**
 * How long an article takes to read, from the document the reader is given.
 *
 * Computed here rather than stored on the article row, for one reason: a
 * reviewer can cut a section or add three paragraphs in the console, and a
 * number written at publish time would then describe a draft nobody sees. The
 * body is already on the wire - `Article.content` - so deriving it costs one
 * walk and cannot disagree with the page.
 *
 * Deliberately not exact. It is a rough expectation-setter above the headline,
 * so it rounds to whole minutes and floors at one: "0 min read" is not a
 * useful thing to tell anyone.
 */

import type { TipTapDoc, TipTapNode } from "@/types/api";

/**
 * Mid-range for adult reading of non-fiction prose. These articles are denser
 * than a news story and lighter than a paper, and the estimate is a courtesy
 * rather than a measurement - a faster or slower constant would move a typical
 * article by well under a minute.
 */
const WORDS_PER_MINUTE = 225;

/**
 * Every word in the document, headings included.
 *
 * `citation` nodes are skipped: they carry no `text` of their own - the `[S1]`
 * a reader sees is drawn by `.ew-chip`'s `::before`/`::after` - but skipping
 * them explicitly says that a chip is a mark on the page rather than something
 * to be read aloud, so a future chip that *does* carry text stays excluded.
 */
function countWords(node: TipTapNode): number {
  if (node.type === "citation") return 0;

  let words = node.text ? node.text.trim().split(/\s+/).filter(Boolean).length : 0;
  for (const child of node.content ?? []) {
    words += countWords(child);
  }
  return words;
}

export function readingMinutes(doc: TipTapDoc): number {
  return Math.max(1, Math.round(countWords(doc) / WORDS_PER_MINUTE));
}

export function readingTimeLabel(doc: TipTapDoc): string {
  return `${readingMinutes(doc)} min read`;
}
