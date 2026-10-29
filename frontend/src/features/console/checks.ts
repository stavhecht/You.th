/**
 * Pure logic behind the review desk's "Need to be checked" tab, kept apart from
 * the component so it can be unit-tested without a browser.
 *
 * Built from what the draft already carries (validation report, source
 * stances). Retrieval precision is an evaluation metric that needs a
 * hand-labelled gold list; a live draft has none, so it is not invented here.
 */

import { VERDICT_LABELS, VERDICT_ORDER } from "@/features/evidence/labels";
import type { ReviewSource, TipTapDoc, ValidationReport, Verdict } from "@/types/api";

export interface Check {
  name: string;
  ok: boolean;
  detail: string;
}

export interface CitedSentence {
  text: string;
  handles: string[];
}

/**
 * `article_sources` has one row per paper *per claim*, so a handle can appear
 * twice. Count handles, not rows, and call a handle off-topic only when every
 * appraised row cited under it is.
 */
export function buildChecks(
  report: ValidationReport,
  sources: ReviewSource[],
  verdict: Verdict,
): Check[] {
  const byHandle = new Map<string, ReviewSource[]>();
  for (const s of sources)
    if (s.wasCited) byHandle.set(s.citationHandle, [...(byHandle.get(s.citationHandle) ?? []), s]);
  const appraisedHandles = [...byHandle].filter(([, rows]) => rows.some((r) => r.stance !== null));
  const offTopic = appraisedHandles
    .filter(([, rows]) => rows.filter((r) => r.stance !== null).every((r) => r.stance === "off_topic"))
    .map(([handle]) => handle);
  const appraised = appraisedHandles.length;

  const ceiling = report.verdictCeiling;
  const rank = (v: Verdict) => VERDICT_ORDER.indexOf(v);
  const over = ceiling !== null && rank(verdict) > rank(ceiling);
  const under = ceiling !== null && rank(verdict) < rank(ceiling);

  const checks: Check[] = [
    {
      name: "citations resolve",
      ok: report.citationsResolved === report.citationsTotal,
      detail: `${report.citationsResolved}/${report.citationsTotal} citations point at a real source`,
    },
    {
      name: "citation relevance",
      ok: appraised > 0 && offTopic.length === 0,
      detail: appraised
        ? `${appraised - offTopic.length}/${appraised} appraised cited sources test the claim; off-topic: [${offTopic.join(", ")}]`
        : "cited sources were not appraised, check each one by hand",
    },
    {
      name: "uncertainty",
      ok: ceiling !== null && !over && !under,
      detail:
        ceiling === null
          ? "ceiling not recorded for this draft"
          : `verdict ${VERDICT_LABELS[verdict]}, evidence allows up to ${VERDICT_LABELS[ceiling]}${over ? " (too confident)" : under ? " (more cautious than the sources require; check it is deliberate)" : ""}`,
    },
  ];
  for (const f of report.failures)
    checks.push({ name: f.code.replace(/_/g, " "), ok: false, detail: f.message });
  for (const w of report.warnings ?? [])
    checks.push({ name: w.code.replace(/_/g, " "), ok: false, detail: w.message });
  return checks;
}

/** The sentence before each citation chip: text since the last chip, last sentence only. */
export function citedSentences(doc: TipTapDoc): CitedSentence[] {
  const out: CitedSentence[] = [];
  for (const block of doc.content ?? []) {
    let run = "";
    for (const node of block.content ?? []) {
      if (node.type === "text") run += node.text ?? "";
      else if (node.type === "citation") {
        const text = run.trim().split(/(?<=[.!?])\s+/).pop() ?? "";
        const handles = (node.attrs?.sourceIds as string[] | undefined) ?? [];
        if (text && handles.length) out.push({ text, handles });
        run = "";
      }
    }
  }
  return out;
}

// Words that carry no topic. Without them every sentence is flagged for
// "found" or "after" whenever the abstract words it differently.
const COMMON = new Set(
  "about above after again among because before being between both could during found further having might results result shown showed shows since study studies their there these they those through under until using used where which while with within without would".split(
    " ",
  ),
);

const NUMBER = /\d+(?:\.\d+)?/g;

// Numbers (whole tokens: "10" is not in "2010") and topic words of 5+ letters,
// matched by 5-letter stem. A hint for the eye, not a judge: it cannot see a
// reworded claim or a wrong direction.
export function missingTerms(sentence: CitedSentence, sources: ReviewSource[]): string[] {
  const haystack = sources
    .filter((s) => sentence.handles.includes(s.citationHandle))
    .map((s) => [s.title, s.year ?? "", s.abstract, ...s.excerpts.map((e) => e.text)].join(" "))
    .join(" ")
    .toLowerCase();
  const numbers = new Set(haystack.match(NUMBER) ?? []);
  const lower = sentence.text.toLowerCase();
  const words = [...new Set(lower.match(/[a-z]{5,}/g) ?? [])].filter((w) => !COMMON.has(w));
  return [
    ...new Set(lower.match(NUMBER) ?? []).values(),
    ...words,
  ].filter((t) => (/\d/.test(t) ? !numbers.has(t) : !haystack.includes(t.slice(0, 5))));
}

/** Stable across edits, unlike a list position. */
export const sentenceKey = (s: CitedSentence) => `${s.handles.join(",")}|${s.text}`;
