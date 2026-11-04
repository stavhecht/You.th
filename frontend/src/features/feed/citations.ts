/**
 * Reader-facing citation numbers.
 *
 * The pipeline's handles are `S1`, `S4`, `S9` - positions in the *ranked
 * retrieval set*, which is a fact about how the article was made and not one a
 * reader has any use for. Worse, the public page shows only the sources the
 * article actually cites, so those handles arrive with gaps: a reference list
 * numbered S1, S4, S9, S12 reads as a list with three entries missing.
 *
 * So the reader gets `[1]`, `[2]`, `[3]` - sequential, in the order the source
 * list presents them.
 *
 * **The handle stays the identity.** Anchors (`#source-S4`), popover keys and
 * the active-row highlight all still key on it; only the *label* changes. That
 * is what keeps this a rendering concern and not a second numbering the server
 * would have to agree with.
 *
 * **One map, both components.** `ArticleContent`'s chips and `SourceList`'s
 * pills are the same numbering seen twice, and a reader checking a chip against
 * the list is the whole point of the citation. Two derivations would be two
 * things free to drift, and the drift would be silent - the numbers would
 * simply stop pointing at the right paper.
 */

import type { Source } from "@/types/api";

/** Handle → the number a reader sees. Order is the source list's own. */
export function citationNumbers(sources: Source[]): Map<string, number> {
  return new Map(sources.map((source, index) => [source.citationHandle, index + 1]));
}

/**
 * The label for one handle.
 *
 * Falls back to the handle itself when it resolves to no source. Validation
 * guarantees that cannot happen in a published article (invariant #2), so the
 * fallback exists to make a broken guarantee visible rather than blank.
 */
export function citationLabel(numbers: Map<string, number>, handle: string): string {
  return String(numbers.get(handle) ?? handle);
}
