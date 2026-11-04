/**
 * The evidence grade - nine rungs, weakest first, filled to the strongest
 * study type actually retrieved.
 *
 * This is the *warrant* for the verdict, and it is a separate signal on purpose:
 * "Supported" backed by a meta-analysis and "Supported" backed by one small RCT
 * are different claims, and a reader who can only see the verdict cannot tell
 * them apart. Server-side this same ordering caps how confident a verdict is
 * allowed to be (invariant #3, `evidence/grading.py`); showing the bar is what
 * makes that cap legible rather than mysterious.
 *
 * Filled segments are ink and unfilled are the hairline rule - no colour here
 * either, for the reasons in VerdictMark.tsx.
 */

import { GRADE_LABELS, GRADE_ORDER } from "./labels";
import {
  GRADE_BAR_LABEL,
  GRADE_BAR_ROW,
  GRADE_BAR_TRACK,
  gradeRung,
} from "./styles";
import type { StudyType } from "@/types/api";

interface Props {
  grade: StudyType;
  /** Renders the rung name beside the bar. */
  showLabel?: boolean;
  className?: string;
}

export function GradeBar({ grade, showLabel = false, className }: Props) {
  const reached = GRADE_ORDER.indexOf(grade);
  const position = `${reached + 1} of ${GRADE_ORDER.length}`;

  return (
    <span className={`${GRADE_BAR_ROW} ${className ?? ""}`}>
      <span
        className={GRADE_BAR_TRACK}
        role="img"
        // The bar is the only place the rung's *position* is visible, so the
        // accessible name has to carry it: "RCT" alone loses the ranking that
        // the eight segments are drawing.
        aria-label={`Strongest evidence: ${GRADE_LABELS[grade]}, rung ${position}`}
      >
        {GRADE_ORDER.map((rung, index) => (
          <span key={rung} className={gradeRung(index <= reached)} />
        ))}
      </span>
      {showLabel ? (
        <span className={GRADE_BAR_LABEL}>{GRADE_LABELS[grade]}</span>
      ) : null}
    </span>
  );
}
