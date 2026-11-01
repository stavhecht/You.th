/**
 * Names for the evidence axis's styling — the verdict mark, its wording, and
 * the grade bar.
 *
 * Read `VerdictMark.tsx` before changing anything here. Every value in this
 * file is constrained by one rule: the verdict is carried by **geometry, not
 * colour**. The fills below are all `bg-ink` for that reason, and a hue
 * appearing on any of them would undo the decision rather than restyle it.
 *
 * The conditional helpers are functions, matching `subject.ts`, so the branch
 * that picks a class stays next to the reason for it.
 */

import type { Verdict } from "@/types/api";

type MarkSize = "sm" | "card" | "lg";

/**
 * `card` tracks `--ew-mark`, so verdict prominence across the whole feed
 * retunes from one token. `sm` and `lg` are fixed — dense console rows and the
 * article header have optical needs the feed token cannot serve.
 */
const MARK_BOX: Record<MarkSize, string> = {
  sm: "h-[13px] w-[13px] border-2",
  card: "h-[var(--ew-mark)] w-[var(--ew-mark)] border-2",
  lg: "h-[26px] w-[26px] border-[3px]",
};

/** The square itself, at the requested size. Never shrinks in a flex row. */
export function markBox(size: MarkSize): string {
  return `${MARK_BOX[size]} flex-none`;
}

/** Filled: multiple independent trials point the same way. */
export const MARK_SUPPORTED = "border-ink bg-ink";
/** Half-filled: trials disagree, or back one claim on the label and not the rest. */
export const MARK_MIXED = "flex border-ink";
export const MARK_MIXED_FILL = "w-1/2 bg-ink";
/** A quarter, on the floor: something exists, but small, indirect or off-endpoint. */
export const MARK_WEAK = "flex items-end border-ink";
export const MARK_WEAK_FILL = "h-[30%] w-full bg-ink";
/** Empty, one ink step back — absence, not a fifth and worse verdict. */
export const MARK_NO_EVIDENCE = "border-ink-3";

/**
 * `no_evidence` steps back one ink everywhere it appears, so a feed full of
 * genuine gaps does not shout at the reader.
 */
function verdictInk(verdict: Verdict): string {
  return verdict === "no_evidence" ? "text-ink-3" : "text-ink";
}

/**
 * At `lg` the wording is display type in its own right; at every other size it
 * rides the shared `.ew-verdict-label` prominence tokens, which keep it matched
 * to the card headline.
 */
export function verdictWording(verdict: Verdict, size: MarkSize): string {
  const type =
    size === "lg"
      ? "font-heading text-[22px] font-extrabold leading-none tracking-[-0.01em]"
      : "ew-verdict-label font-body";
  return `${type} ${verdictInk(verdict)}`;
}

/**
 * The scope limit on a verdict — "for strength and lean mass". Lighter ink, but
 * the same size as the wording it qualifies, because it is a limit rather than
 * a footnote.
 */
export function verdictQualifier(size: MarkSize): string {
  return size === "lg"
    ? "font-body text-[15px] font-normal leading-tight text-ink-3"
    : "font-body text-meta font-normal text-ink-3";
}

export const GRADE_BAR_ROW = "flex items-center gap-2.5";
export const GRADE_BAR_TRACK = "flex h-[22px] items-end gap-[2px]";

/** Filled to the strongest study type retrieved; the rest is hairline rule. */
export function gradeRung(reached: boolean): string {
  return `h-[6px] w-[9px] flex-none ${reached ? "bg-ink" : "bg-rule-soft"}`;
}

export const GRADE_BAR_LABEL =
  "font-heading text-[14px] font-semibold leading-tight text-ink";
