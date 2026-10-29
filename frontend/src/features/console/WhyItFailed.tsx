/**
 * "Need to be checked": the checks a reviewer can verify by hand, one line
 * each, worded like the evaluation report's per-case list. Logic is in
 * `checks.ts`.
 */

import { useState } from "react";

import type { ReviewSource, TipTapDoc, ValidationReport, Verdict } from "@/types/api";
import { buildChecks, citedSentences, missingTerms, sentenceKey } from "./checks";
import { SECTION_LABEL } from "./controls";
import {
  PANEL_BLOCK,
  PANEL_GRADE_NOTE,
  VALIDATION_FAILURES,
  WEAK_EVIDENCE_WARNING,
} from "./styles";

const FAILED = "font-body text-micro text-danger-ink";

/** "I fixed this" tick. Session-only: it is the reviewer's own bookkeeping, not article state. */
function Fixed({
  id,
  done,
  onChange,
}: {
  id: string;
  done: boolean;
  onChange: (done: boolean) => void;
}) {
  return (
    <label className="mr-2 inline-flex items-center gap-1 text-ink-3" title="Mark as fixed">
      <input
        type="checkbox"
        className="accent-accent"
        aria-label={`Fixed: ${id}`}
        checked={done}
        onChange={(e) => onChange(e.target.checked)}
      />
    </label>
  );
}

function CitedSentences({ doc, sources }: { doc: TipTapDoc; sources: ReviewSource[] }) {
  const sentences = citedSentences(doc);
  // Keyed by the sentence, so an edit that shifts the list moves no tick.
  const [fixed, setFixed] = useState<ReadonlySet<string>>(new Set());
  return (
    <div className="mt-4">
      <span className={SECTION_LABEL}>Cited sentences vs their sources</span>
      <ul className="mt-2 space-y-3">
        {sentences.map((s) => {
          const missing = missingTerms(s, sources);
          const key = sentenceKey(s);
          return (
            <li key={key} className="font-body text-[12px] leading-normal text-ink-2">
              <p>
                “{s.text}” <b>[{s.handles.join(", ")}]</b>
              </p>
              {missing.length ? (
                <p className={FAILED}>
                  <Fixed
                    id={s.text.slice(0, 40)}
                    done={fixed.has(key)}
                    onChange={(on) =>
                      setFixed((prev) => {
                        const next = new Set(prev);
                        if (on) next.add(key);
                        else next.delete(key);
                        return next;
                      })
                    }
                  />
                  Not in the cited source: {missing.join(", ")}
                </p>
              ) : null}
              {s.handles.map((h) => {
                const src = sources.find((x) => x.citationHandle === h);
                return src ? (
                  <details key={h}>
                    <summary className="cursor-pointer text-ink-3">
                      {h}: {src.title}
                    </summary>
                    <p className={PANEL_GRADE_NOTE}>{src.abstract || "No abstract stored."}</p>
                  </details>
                ) : null;
              })}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export function WhyItFailed({
  report,
  sources,
  verdict,
  doc,
}: {
  report: ValidationReport;
  sources: ReviewSource[];
  verdict: Verdict;
  doc: TipTapDoc;
}) {
  return (
    <div className={PANEL_BLOCK}>
      <span className={SECTION_LABEL}>Need to be checked</span>
      <ul className={VALIDATION_FAILURES}>
        {buildChecks(report, sources, verdict).map((c, i) => (
          <li key={i} className={c.ok ? PANEL_GRADE_NOTE : FAILED}>
            <b>
              {c.ok ? "Pass" : "Check"} · {c.name}
            </b>
            : {c.detail}
          </li>
        ))}
      </ul>
      <p className={WEAK_EVIDENCE_WARNING}>
        Retrieval precision is only scored in the evaluation, which has a gold list.
      </p>
      <CitedSentences doc={doc} sources={sources} />
    </div>
  );
}
