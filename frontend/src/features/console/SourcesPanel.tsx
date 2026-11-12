/**
 * The sources panel - "fast source access", the core review requirement.
 *
 * The reviewer's job is to answer "does this article's evidence actually say
 * what it claims?" as fast as possible. Four design consequences:
 *
 * 1. **Grouped by claim, not by source.** The reviewer reads a claim in the
 *    editor and needs its backing evidence next to it. A flat source list makes
 *    them do the join in their head. (The design comp draws this list flat;
 *    that is the one place the review screen departs from it, because the comp
 *    was built against sample data where every article had a single claim.)
 * 2. **Study type and year on every row.** These are what caps the verdict, so
 *    they belong at a glance, not behind a click.
 * 3. **Weak evidence flagged inline.** A confident verdict resting on two
 *    cell-culture studies has to be visible without opening anything. It is
 *    drawn in the accent ink - the one colour on the evidence axis, and it
 *    marks the *source*, never the verdict.
 * 4. **Uncited sources shown, greyed, not hidden.** What the model chose to
 *    leave out is the failure mode human review exists to catch. If retrieval
 *    surfaced a contradicting trial and the draft ignored it, this panel is the
 *    only place that becomes visible.
 *
 * Everything here comes from one request (`article_sources` joined to
 * `sources`, server-side) - no client-side resolution, no waterfall.
 */

import { useEffect, useRef } from "react";

import { GradeBar } from "@/features/evidence/GradeBar";
import {
  GRADE_NOTES,
  STANCE_LABELS,
  STUDY_TYPE_LABELS,
  VERDICT_LABELS,
  VERDICT_ORDER,
} from "@/features/evidence/labels";
import type { ReviewSource, StudyType, ValidationReport, Verdict } from "@/types/api";
import { SECTION_LABEL } from "./controls";
import {
  CLAIM_GROUP,
  CLAIM_HEADING,
  PANEL_BLOCK,
  PANEL_EXCERPT,
  PANEL_EXCERPT_SECTION,
  PANEL_EXCERPTS,
  PANEL_EXCERPTS_SUMMARY,
  PANEL_GRADE_BAR,
  PANEL_GRADE_NOTE,
  PANEL_SOURCE_HANDLE,
  PANEL_SOURCE_HEAD,
  PANEL_SOURCE_LIST,
  PANEL_SOURCE_META,
  PANEL_SOURCE_TITLE_LINK,
  VALIDATION_FAILURE,
  VALIDATION_FAILURES,
  VALIDATION_HEADLINE,
  WEAK_EVIDENCE_WARNING,
  panelSourceRow,
  panelSourceStudyType,
  RETRACTED_FLAG,
} from "./styles";

interface Props {
  sources: ReviewSource[];
  /** Set when the reviewer clicks a citation chip in the editor. */
  focusedHandle?: string | null;
}

export function SourcesPanel({ sources, focusedHandle }: Props) {
  const byClaim = groupByClaim(sources);
  // Articles drafted before APPRAISE existed have no labels at all; saying
  // "direction not checked" on every one of their rows would be noise. Once
  // any source carries a label, a missing one is worth saying.
  const appraised = sources.some((source) => source.stance !== null);

  return (
    <div className={PANEL_BLOCK}>
      <span className={SECTION_LABEL}>Retrieved sources</span>

      {Object.entries(byClaim).map(([claim, claimSources]) => (
        <section key={claim} className={CLAIM_GROUP}>
          <h3 className={CLAIM_HEADING}>{claim}</h3>
          <ul className={PANEL_SOURCE_LIST}>
            {claimSources.map((source) => (
              <SourceRow
                key={source.sourceId + claim}
                source={source}
                focused={focusedHandle === source.citationHandle}
                appraised={appraised}
              />
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

function SourceRow({
  source,
  focused,
  appraised,
}: {
  source: ReviewSource;
  focused: boolean;
  appraised: boolean;
}) {
  const ref = useRef<HTMLLIElement>(null);

  useEffect(() => {
    if (focused) ref.current?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [focused]);

  return (
    <li
      ref={ref}
      className={panelSourceRow(focused, source.wasCited)}
    >
      <div className={PANEL_SOURCE_HEAD}>
        <span className={PANEL_SOURCE_HANDLE}>{source.citationHandle}</span>
        <a
          href={source.url}
          target="_blank"
          rel="noreferrer noopener"
          className={PANEL_SOURCE_TITLE_LINK}
        >
          {source.title}
        </a>
      </div>

      <div className={panelSourceStudyType(source.isWeakEvidence)}>
        {STUDY_TYPE_LABELS[source.studyType]} ·{" "}
        {source.wasCited ? "cited" : "retrieved, not cited"}
      </div>
      {source.retracted || source.concern ? (
        <div className={RETRACTED_FLAG}>
          {source.retracted ? "Retracted" : "Expression of concern"}
          {source.retractionNote ? ` · ${source.retractionNote}` : ""}
        </div>
      ) : null}
      {appraised ? (
        <div className={PANEL_SOURCE_META}>
          Appraised:{" "}
          {source.stance !== null ? STANCE_LABELS[source.stance] : "direction not checked"}
        </div>
      ) : null}
      <div className={PANEL_SOURCE_META}>
        {[
          source.journal,
          source.year,
          source.citationCount != null ? `${source.citationCount} citations` : null,
        ]
          .filter(Boolean)
          .join(" · ")}
      </div>

      {source.excerpts.length > 0 && (
        <details className={PANEL_EXCERPTS}>
          <summary className={PANEL_EXCERPTS_SUMMARY}>
            {source.excerpts.length === 1
              ? "1 full-text excerpt shown to the model"
              : `${source.excerpts.length} full-text excerpts shown to the model`}
          </summary>
          {source.excerpts.map((excerpt, index) => (
            <blockquote key={index} className={PANEL_EXCERPT}>
              <span className={PANEL_EXCERPT_SECTION}>
                {excerpt.section ?? "Untitled section"}
              </span>{" "}
              {excerpt.text}
            </blockquote>
          ))}
        </details>
      )}
    </li>
  );
}

/**
 * Validation and evidence grade - the two facts that decide whether this draft
 * is publishable at all, above the sources they are computed from.
 *
 * A failing report is stated as a list of what broke, not as a colour: the
 * draft is already unapprovable server-side (invariant #2), so this panel's job
 * is to say *why* precisely enough to fix the prompt.
 */
export function ValidationSummary({
  report,
  grade,
  verdict,
  restsOnWeakEvidence,
}: {
  report: ValidationReport;
  grade: StudyType;
  verdict: Verdict;
  restsOnWeakEvidence: boolean;
}) {
  // The verdict cap only ever fails a draft for being too *confident*. A draft
  // that is more cautious than its sources require passes silently and looks
  // exactly like a correct cautious call, which is how three articles in a row
  // came out "weak" on meta-analyses with a clean report each time. Stating the
  // ceiling next to the verdict is the only place that gap is visible.
  const understated =
    report.verdictCeiling !== null &&
    VERDICT_ORDER.indexOf(verdict) < VERDICT_ORDER.indexOf(report.verdictCeiling);
  return (
    <div className={PANEL_BLOCK}>
      <span className={SECTION_LABEL}>Validation</span>

      <p className={VALIDATION_HEADLINE}>
        {report.citationsResolved}/{report.citationsTotal} citations resolve
      </p>

      {!report.passed ? (
        <ul className={VALIDATION_FAILURES}>
          {report.failures.map((failure, index) => (
            <li key={index} className={VALIDATION_FAILURE}>
              {failure.message}
            </li>
          ))}
        </ul>
      ) : null}

      {report.warnings?.length ? (
        <ul className={VALIDATION_FAILURES}>
          {report.warnings.map((warning, index) => (
            <li key={index} className={WEAK_EVIDENCE_WARNING}>
              {warning.message}
            </li>
          ))}
        </ul>
      ) : null}

      <GradeBar grade={grade} showLabel className={PANEL_GRADE_BAR} />
      <p className={PANEL_GRADE_NOTE}>{GRADE_NOTES[grade]}</p>

      {restsOnWeakEvidence ? (
        <p className={WEAK_EVIDENCE_WARNING}>
          This verdict rests on weak study types. Check the sources before approving.
        </p>
      ) : null}

      {understated && report.verdictCeiling ? (
        <p className={PANEL_GRADE_NOTE}>
          The cited evidence would have supported &ldquo;
          {VERDICT_LABELS[report.verdictCeiling]}&rdquo;. This draft is more
          cautious than its sources require, which may be right; check that it
          uses what it cites.
        </p>
      ) : null}
    </div>
  );
}

/**
 * Group by claim; cited sources first, then by handle number.
 *
 * Cited-first because those are what the reviewer is verifying. The uncited
 * ones still appear below, which is the point - but they should not push the
 * cited evidence out of view.
 */
function groupByClaim(sources: ReviewSource[]): Record<string, ReviewSource[]> {
  const grouped = sources.reduce<Record<string, ReviewSource[]>>((acc, source) => {
    (acc[source.claim] ??= []).push(source);
    return acc;
  }, {});

  for (const claim of Object.keys(grouped)) {
    grouped[claim]!.sort((a, b) => {
      if (a.wasCited !== b.wasCited) return a.wasCited ? -1 : 1;
      return handleNumber(a.citationHandle) - handleNumber(b.citationHandle);
    });
  }
  return grouped;
}

/** Sorts S2 before S10, which lexicographic ordering would not. */
function handleNumber(handle: string): number {
  const digits = Number.parseInt(handle.slice(1), 10);
  return Number.isNaN(digits) ? 0 : digits;
}
