/**
 * What a reviewer can do to an article that is already live.
 *
 * Two actions, both with a required sentence: publish the correction typed in
 * the editor (the note is shown to readers with the date), or withdraw the
 * article from the site (the reason stays on the desk). Typing in the editor
 * changes nothing readers see until the correction is published.
 */

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { consoleKeys, publishCorrection, withdrawArticle } from "@/lib/api/console";
import { describeError } from "./errors";
import { SECTION_LABEL } from "./controls";
import {
  ACTION_ERROR_ALERT,
  APPROVE_BUTTON,
  DECISION_BLOCK,
  DECISION_LINK,
  DECISION_NOTE,
  REJECT_BUTTON,
  REJECT_REASON_FIELD,
} from "./styles";
import type { Autosave } from "./useAutosave";
import type { ArticleDetail } from "@/types/api";

export function PublishedDecision({
  article,
  autosave,
}: {
  article: ArticleDetail;
  autosave: Autosave;
}) {
  const queryClient = useQueryClient();
  const [note, setNote] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);

  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["console"] });

  const correct = useMutation({
    mutationFn: async () => {
      if (!(await autosave.flush())) {
        throw new Error("Your correction is not saved. Resolve that before publishing it.");
      }
      await publishCorrection(article.id, note.trim());
    },
    onSuccess: () => {
      setNote("");
      refresh();
      void queryClient.invalidateQueries({ queryKey: consoleKeys.article(article.id) });
    },
    onError: (caught) => setError(describeError(caught)),
  });

  const withdraw = useMutation({
    mutationFn: () => withdrawArticle(article.id, reason.trim()),
    onSuccess: refresh,
    onError: (caught) => setError(describeError(caught)),
  });

  return (
    <div className={DECISION_BLOCK}>
      <span className={SECTION_LABEL}>Published</span>
      <p className={DECISION_NOTE}>
        Live at{" "}
        <a
          href={`/a/${article.slug}`}
          target="_blank"
          rel="noreferrer"
          className={DECISION_LINK}
        >
          /a/{article.slug} ↗
        </a>
        . Edits you type here stay a draft until you publish them as a correction.
      </p>

      <input
        value={note}
        onChange={(event) => setNote(event.target.value)}
        aria-label="What the correction changes"
        placeholder="What changed (shown to readers, required)"
        maxLength={500}
        className={REJECT_REASON_FIELD}
      />
      <button
        onClick={() => {
          setError(null);
          correct.mutate();
        }}
        disabled={!note.trim() || correct.isPending}
        className={APPROVE_BUTTON}
      >
        {correct.isPending ? "Publishing…" : "Publish correction"}
      </button>

      {article.corrections.length > 0 ? (
        <ul className={DECISION_NOTE}>
          {article.corrections.map((entry) => (
            <li key={entry.at}>
              Corrected {new Date(entry.at).toLocaleDateString()}: {entry.note}
            </li>
          ))}
        </ul>
      ) : null}

      <input
        value={reason}
        onChange={(event) => setReason(event.target.value)}
        aria-label="Reason for withdrawing"
        placeholder="Reason for withdrawing (required)"
        maxLength={2000}
        className={REJECT_REASON_FIELD}
      />
      <button
        onClick={() => {
          setError(null);
          withdraw.mutate();
        }}
        disabled={!reason.trim() || withdraw.isPending}
        title="Takes the article off the feed, search and its own page. Cannot be undone."
        className={REJECT_BUTTON}
      >
        {withdraw.isPending ? "Withdrawing…" : "Withdraw from the site"}
      </button>

      {error ? (
        <p role="alert" className={ACTION_ERROR_ALERT}>
          {error}
        </p>
      ) : null}
    </div>
  );
}
