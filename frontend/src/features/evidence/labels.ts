/**
 * The vocabulary of the two signal systems, in one place.
 *
 * Verdict is the judgment. Evidence grade is the warrant for it. Both were
 * previously spelled out separately in the feed and in the console, which is
 * how "RCT" in one panel and "Randomised controlled trial" in the other came to
 * mean the same thing while looking like different claims. A reviewer comparing
 * the two screens has to see the same words.
 */

import type { Stance, StudyType, Verdict } from "@/types/api";

export const VERDICT_LABELS: Record<Verdict, string> = {
  supported: "Supported",
  mixed: "Mixed",
  weak: "Weak",
  no_evidence: "No evidence found",
};

/**
 * Read under the verdict on an article, and on the design-system reference.
 *
 * `no_evidence` gets the longest gloss on purpose: it is the one verdict a
 * reader is likely to misread as a judgment on the product rather than a report
 * on the literature.
 */
export const VERDICT_GLOSS: Record<Verdict, string> = {
  supported:
    "Multiple independent trials point the same way for the claim named above.",
  mixed: "Trials disagree, or they support one claim on the label and not the others.",
  weak: "Some evidence exists, but it is small, indirect, or measures the wrong endpoint.",
  no_evidence:
    "We searched and found no study that tests this claim. That is a gap in the literature, not a judgment on the product.",
};

/**
 * The four verdicts, weakest first. Order is load-bearing in the same way
 * `GRADE_ORDER` is: `VERDICT_ORDER.indexOf` is how the review desk compares a
 * draft's verdict against the ceiling its sources allowed, and it mirrors
 * `VERDICT_STRENGTH` in `evidence/grading.py`.
 *
 * Note this is *not* the declaration order of the `Verdict` type, which runs
 * strongest-first for display. Reading strength off that would invert every
 * comparison, which is why the ranking is written out here rather than derived.
 */
export const VERDICT_ORDER: readonly Verdict[] = [
  "no_evidence",
  "weak",
  "mixed",
  "supported",
];

/**
 * The nine rungs, weakest first. Order is load-bearing: `GRADE_ORDER.indexOf`
 * is what fills the grade bar, and it mirrors the server-side ranking in
 * `evidence/grading.py` that caps verdict confidence (invariant #3).
 *
 * `unknown` sits at the bottom rather than off to one side, because an
 * unclassifiable study should cap confidence rather than sit neutrally beside a
 * cell-culture result.
 */
export const GRADE_ORDER: readonly StudyType[] = [
  "unknown",
  "in_vitro",
  "animal",
  "case_report",
  "narrative_review",
  "observational",
  "rct",
  "systematic_review",
  "meta_analysis",
];

/** Short form, for the grade bar and dense console rows. */
export const GRADE_LABELS: Record<StudyType, string> = {
  unknown: "Unknown",
  in_vitro: "In vitro",
  animal: "Animal",
  case_report: "Case report",
  narrative_review: "Narrative review",
  observational: "Observational",
  rct: "RCT",
  systematic_review: "Systematic review",
  meta_analysis: "Meta-analysis",
};

/** What that rung actually buys you. Shown beside the grade bar. */
export const GRADE_NOTES: Record<StudyType, string> = {
  unknown:
    "Study type could not be classified. Treated as the weakest tier, because uncertainty caps confidence.",
  in_vitro:
    "Cells or tissue in a dish. Tells you a mechanism is possible, not that it happens in a person.",
  animal: "Whole organism, wrong species. Dose and physiology rarely transfer cleanly.",
  case_report:
    "One person, no control. Useful for spotting harms, not for establishing benefit.",
  narrative_review:
    "A survey of other people's findings with no stated search method. A pointer to the literature, not evidence in itself.",
  observational:
    "Real people, no randomisation. Confounding is the standing objection.",
  rct: "Randomised and controlled. The first rung where causation is on the table.",
  systematic_review:
    "All the trials found by a declared method, appraised together.",
  meta_analysis: "Those trials pooled into one estimate. The strongest thing we can cite.",
};

/** Long form, for a source row where the type is the reader's calibration. */
export const STUDY_TYPE_LABELS: Record<StudyType, string> = {
  unknown: "Study type unclear",
  in_vitro: "In-vitro study",
  animal: "Animal study",
  case_report: "Case report",
  narrative_review: "Narrative review",
  observational: "Observational study",
  rct: "Randomised controlled trial",
  systematic_review: "Systematic review",
  meta_analysis: "Meta-analysis",
};

/**
 * What APPRAISE read a source as finding, for one claim - shown on the desk's
 * source rows. Written as what the *study* found, so a reviewer reads it as a
 * claim about the paper they can check, not as a verdict.
 *
 * These labels come from a model and are not yet measured for accuracy, which
 * is why the desk shows them as text beside the source and nothing reads them
 * to decide a verdict.
 */
export const STANCE_LABELS: Record<Stance, string> = {
  supports: "found the effect",
  no_effect: "found no effect",
  contradicts: "found the opposite",
  unclear: "result unclear",
  off_topic: "does not test this claim",
};

/** Everything below an observational study. Mirrors `is_weak_evidence` server-side. */
const WEAK: ReadonlySet<StudyType> = new Set<StudyType>([
  "unknown",
  "in_vitro",
  "animal",
  "case_report",
  "narrative_review",
]);

export function isWeakStudyType(type: StudyType): boolean {
  return WEAK.has(type);
}
