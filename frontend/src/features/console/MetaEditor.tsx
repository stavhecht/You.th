/**
 * The reviewer's headline, summary and verdict.
 *
 * These three were read-only, so a draft with good prose and a wrong verdict
 * could only be rejected and regenerated. Each field saves on its own when the
 * reviewer leaves it; the server keeps the model's original values.
 *
 * The verdict can be lowered freely and raised only as far as the evidence
 * ceiling. Chips above the ceiling are disabled here and refused by the server.
 */

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { consoleKeys, setMeta } from "@/lib/api/console";
import { VERDICT_LABELS } from "@/features/evidence/labels";
import { describeError } from "./errors";
import { SECTION_LABEL } from "./controls";
import {
  ACTION_ERROR_ALERT,
  DECISION_NOTE,
  META_HEADLINE_FIELD,
  META_SUMMARY_FIELD,
  SUBJECT_ROW,
  subjectChip,
} from "./styles";
import type { ArticleDetail, Verdict } from "@/types/api";

/** Weakest to strongest, the order the ceiling is read against. */
const VERDICTS: Verdict[] = ["no_evidence", "weak", "mixed", "supported"];

export function MetaEditor({ article }: { article: ArticleDetail }) {
  const queryClient = useQueryClient();
  const [headline, setHeadline] = useState(article.headline);
  const [summary, setSummary] = useState(article.summary);

  const save = useMutation({
    mutationFn: (meta: Parameters<typeof setMeta>[1]) => setMeta(article.id, meta),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: consoleKeys.article(article.id) });
      void queryClient.invalidateQueries({ queryKey: consoleKeys.queues });
      void queryClient.invalidateQueries({ queryKey: consoleKeys.card(article.id) });
    },
  });

  const ceiling = article.validationReport.verdictCeiling ?? article.verdict;
  const limit = VERDICTS.indexOf(ceiling);

  return (
    <div>
      <input
        value={headline}
        onChange={(event) => setHeadline(event.target.value)}
        onBlur={() => {
          const next = headline.trim();
          if (next && next !== article.headline) save.mutate({ headline: next });
        }}
        aria-label="Headline"
        maxLength={200}
        className={META_HEADLINE_FIELD}
      />
      <textarea
        value={summary}
        onChange={(event) => setSummary(event.target.value)}
        onBlur={() => {
          const next = summary.trim();
          if (next && next !== article.summary) save.mutate({ summary: next });
        }}
        aria-label="Summary"
        maxLength={600}
        rows={2}
        className={META_SUMMARY_FIELD}
      />

      <div className="mt-3">
        <span className={SECTION_LABEL}>Verdict</span>
        <div className={SUBJECT_ROW}>
          {VERDICTS.map((option, index) => (
            <button
              key={option}
              onClick={() => {
                if (option !== article.verdict) save.mutate({ verdict: option });
              }}
              disabled={save.isPending || index > limit}
              aria-pressed={option === article.verdict}
              title={
                index > limit
                  ? `The cited evidence caps the verdict at ${VERDICT_LABELS[ceiling]}`
                  : undefined
              }
              className={subjectChip(option === article.verdict)}
            >
              {VERDICT_LABELS[option]}
            </button>
          ))}
        </div>
      </div>

      {save.isError ? (
        <p role="alert" className={ACTION_ERROR_ALERT}>
          {describeError(save.error)}
        </p>
      ) : article.originalMeta ? (
        <p className={DECISION_NOTE}>
          Edited. The model wrote: “{article.originalMeta.headline}” ·{" "}
          {VERDICT_LABELS[article.originalMeta.verdict]}
        </p>
      ) : null}
    </div>
  );
}
