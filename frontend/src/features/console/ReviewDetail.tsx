/**
 * The review screen: editor and evidence side by side.
 *
 * This is where invariant #1 is actually exercised — the one place in the
 * product where a human decides something goes live. Three things it must
 * never do:
 *
 *   - Offer Approve on anything that is not `pending_review`. The server
 *     enforces this, but showing a button that always 409s trains reviewers to
 *     ignore errors.
 *   - Approve with unsaved edits in flight. Autosave is flushed first, or the
 *     published article is a stale copy of what the reviewer read.
 *   - Collapse a 409 into a generic failure. The detail names which citation
 *     broke, and that is the whole difference between an actionable error and
 *     a scavenger hunt.
 *
 * The layout is the page scrolling with the evidence column pinned, rather than
 * two independently scrolling panes. Side by side is the requirement — the
 * reviewer cross-checks one against the other continuously, and a tabbed layout
 * turns every check into a context switch — but a pinned column gets that
 * without trapping the wheel in whichever pane the cursor happens to be over.
 *
 * The decision controls sit at the bottom of that column, under the evidence
 * they follow from. Putting Approve in the top bar, which is the reflex, places
 * it where a reviewer's hand rests *before* they have read anything.
 */

import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";

import { ApiError } from "@/lib/api/client";
import {
  approveArticle,
  consoleKeys,
  fetchArticleDetail,
  rejectArticle,
} from "@/lib/api/console";
import { VerdictMark } from "@/features/evidence/VerdictMark";
import { VERDICT_LABELS } from "@/features/evidence/labels";
import { readingTimeLabel } from "@/lib/readingTime";
import { ArticleEditor } from "./ArticleEditor";
import { FeedPreview } from "./FeedPreview";
import { SubjectPicker } from "./SubjectPicker";
import { SourcesPanel, ValidationSummary } from "./SourcesPanel";
import { SECTION_LABEL } from "./controls";
import {
  ACTION_ERROR_ALERT,
  APPROVE_BUTTON,
  BACK_TO_QUEUE,
  DECISION_BLOCK,
  DECISION_LINK,
  DECISION_NOTE,
  DRAFT_HEADLINE,
  DRAFT_META,
  DRAFT_STATUS_ROW,
  DRAFT_VERDICT_WORDING,
  EDITOR_SLOT,
  REASON_MISSING_ALERT,
  REJECT_BUTTON,
  REJECT_REASON_FIELD,
  REVIEW_BODY_COLUMN,
  REVIEW_COLUMNS,
  REVIEW_PAGE,
  REVIEW_SIDEBAR,
  REVIEW_TOP_BAR,
  SIDEBAR_SOURCES,
  STATUS_DIVIDER,
  TOP_BAR_ACTION,
} from "./styles";
import { ROUTE_ERROR_MESSAGE, ROUTE_MESSAGE } from "@/features/shell/styles";
import { useAutosave } from "./useAutosave";

export function ReviewDetail() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [actionError, setActionError] = useState<string | null>(null);
  const [focusedHandle, setFocusedHandle] = useState<string | null>(null);
  const [rejectReason, setRejectReason] = useState("");
  const [reasonMissing, setReasonMissing] = useState(false);
  const [previewOpen, setPreviewOpen] = useState(false);
  const previewTrigger = useRef<HTMLElement | null>(null);

  /**
   * Preview exactly what the reviewer is looking at.
   *
   * The flush is the same one `approve` does, for the same reason: the server
   * derives the tile from `edited_content`, so an unflushed edit would be
   * previewed as though it had not been made. Its result is deliberately not
   * checked, unlike in `goBack` — leaving loses work, previewing loses nothing,
   * and a failed save already blocks Approve and says so in the editor.
   */
  async function openPreview() {
    previewTrigger.current = document.activeElement as HTMLElement | null;
    await autosave.flush();
    void queryClient.invalidateQueries({ queryKey: consoleKeys.card(id) });
    setPreviewOpen(true);
  }

  function closePreview() {
    setPreviewOpen(false);
    // Back to the button that opened it, or the reviewer's place on the page
    // is wherever the browser decides after the dialog leaves the tree.
    previewTrigger.current?.focus();
  }

  const { data: article, status } = useQuery({
    queryKey: consoleKeys.article(id),
    queryFn: () => fetchArticleDetail(id),
    enabled: Boolean(id),
  });
  // The editor loads `editedContent ?? originalContent`; only edited content is
  // what a save must start from (null for an unedited draft).
  const autosave = useAutosave(id, article ? (article.editedContent ?? null) : undefined);

  const approve = useMutation({
    mutationFn: async () => {
      // Publish exactly what the reviewer is looking at. An edit that did not
      // save (a conflict found by this very flush) must stop the publish, or the
      // server's version, which this reviewer never saw, goes live.
      if (!(await autosave.flush())) {
        throw new Error("Your edits are not saved. Resolve that before publishing.");
      }
      await approveArticle(id);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["console"] });
      void navigate("/review");
    },
    onError: (error) => setActionError(describeError(error)),
  });

  const reject = useMutation({
    mutationFn: (reason: string) => rejectArticle(id, reason),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["console"] });
      void navigate("/review");
    },
    onError: (error) => setActionError(describeError(error)),
  });

  if (status === "pending") {
    return (
      <p className={ROUTE_MESSAGE}>Loading draft…</p>
    );
  }
  if (status === "error" || !article) {
    return (
      <p className={ROUTE_ERROR_MESSAGE}>Could not load this draft.</p>
    );
  }

  const canApprove = article.status === "pending_review";
  // Rejection is the only way a draft leaves the queue — there is no delete —
  // so it has to be reachable from validation_failed too, or those drafts have
  // no available action at all. They remain unapprovable.
  const canDiscard = canApprove || article.status === "validation_failed";
  const blockedByUnsaved = autosave.state === "error" || autosave.state === "conflict";
  /**
   * A draft the search came up empty on is not publishable from here.
   *
   * `no_evidence` is a real verdict and the article is a well-formed one — it
   * takes the ordinary path and passes validation on its merits — so nothing
   * downstream refuses it. That is what makes the button the place to stop it:
   * the reviewer is the only one who can tell "we searched and the literature
   * is silent" from "this query never had a chance", and the second is by far
   * the more common way a run ends with an empty source list. Reject is still
   * available, and the reason it takes is what the query gets fixed from.
   */
  const foundNothing =
    article.verdict === "no_evidence" || article.sources.length === 0;
  const restsOnWeakEvidence = article.sources.some((s) => s.wasCited && s.isWeakEvidence);

  /**
   * Leaving flushes first. Unmount flushes too, but that fires after the route
   * has already changed — if the save then fails there is no screen left to
   * report it on, and the reviewer walks away believing an edit was kept.
   */
  async function goBack() {
    const saved = await autosave.flush();
    if (!saved) {
      const leave = window.confirm(
        "Some edits could not be saved. Leave anyway and lose them?",
      );
      if (!leave) return;
    }
    void navigate("/review");
  }

  function onReject() {
    setActionError(null);
    if (!rejectReason.trim()) {
      // Not a server rule — the reason is the single most useful artefact a
      // discarded draft leaves behind, because it is what the synthesis prompt
      // gets fixed from. Blocking here is cheaper than losing it.
      setReasonMissing(true);
      return;
    }
    setReasonMissing(false);
    reject.mutate(rejectReason.trim());
  }

  return (
    <main className={REVIEW_PAGE}>
      <div className={REVIEW_TOP_BAR}>
        <button onClick={() => void goBack()} className={BACK_TO_QUEUE}>
          ← Queue
        </button>
        {/* Identity and sign-out live in `ReviewHeader` now that the desk
            has its own chrome — two sign-out buttons on one screen is one
            more than a reviewer needs. */}
        <button
          onClick={() => void openPreview()}
          title="See the feed tile this draft would publish as"
          className={TOP_BAR_ACTION}
        >
          Show draft
        </button>
      </div>

      {previewOpen ? (
        <FeedPreview articleId={id} onClose={closePreview} />
      ) : null}

      <div className={REVIEW_COLUMNS}>
        <section className={REVIEW_BODY_COLUMN}>
          <div className={DRAFT_STATUS_ROW}>
            <span className={SECTION_LABEL}>
              {/* Matches the queue tab's wording — a reviewer arrives here by
                  clicking that tab, and two names for one state reads as two
                  states. See the note on `TABS` in ReviewQueue.tsx. */}
              {article.status === "validation_failed"
                ? "Draft · evidence too weak"
                : article.status === "pending_review"
                  ? "Draft · pending review"
                  : `Draft · ${article.status.replace(/_/g, " ")}`}
            </span>
            <span aria-hidden className={STATUS_DIVIDER} />
            <VerdictMark verdict={article.verdict} size="sm" />
            <span className={DRAFT_VERDICT_WORDING}>
              {VERDICT_LABELS[article.verdict]}
            </span>
            {article.verdictQualifier ? (
              <span className={DRAFT_META}>{article.verdictQualifier}</span>
            ) : null}
            {/* The reader is shown this number on the published page; the
                reviewer deciding whether the draft is finished should see the
                same one, from the same function. */}
            <span className={DRAFT_META}>
              {readingTimeLabel(article.editedContent ?? article.originalContent)}
            </span>
            <span className={DRAFT_META}>{article.topic}</span>
          </div>

          {/*
            Read-only. The headline is not part of the autosave contract — that
            endpoint takes a TipTap document and nothing else — so an editable
            field here would silently discard what was typed into it. Making it
            editable is an API change, not a styling one.
          */}
          <h1 className={DRAFT_HEADLINE}>{article.headline}</h1>

          <div className={EDITOR_SLOT}>
            <ArticleEditor
              content={article.editedContent ?? article.originalContent}
              autosave={autosave}
              articleId={article.id}
              onCitationClick={setFocusedHandle}
            />
          </div>
        </section>

        <aside className={REVIEW_SIDEBAR}>
          <ValidationSummary
            report={article.validationReport}
            grade={article.evidenceGrade}
            verdict={article.verdict}
            restsOnWeakEvidence={restsOnWeakEvidence}
          />

          <div className={SIDEBAR_SOURCES}>
            <SourcesPanel sources={article.sources} focusedHandle={focusedHandle} />
          </div>

          {/* Classification sits above the decision, not inside it: it is
              metadata a reviewer sets while reading, and it stays editable
              after publication. Grouping it with Approve would imply it is
              part of the irreversible act. */}
          <SubjectPicker articleId={article.id} subject={article.subject} />

          <div className={DECISION_BLOCK}>
            <span className={SECTION_LABEL}>Decision</span>

            <button
              onClick={() => {
                setActionError(null);
                approve.mutate();
              }}
              disabled={
                !canApprove || approve.isPending || blockedByUnsaved || foundNothing
              }
              title={
                !canApprove
                  ? "Only drafts pending review can be published"
                  : foundNothing
                    ? "No evidence was found for this draft — it cannot be published"
                    : blockedByUnsaved
                      ? "Unsaved edits — resolve the save error first"
                      : "Publish this article to the public feed"
              }
              className={APPROVE_BUTTON}
            >
              {approve.isPending ? "Publishing…" : "Publish to the public feed"}
            </button>

            {/* Named, so the button is not an abstract state change. This is
                the only path to `published` in the system, and it puts the
                article in front of every reader — worth saying in words. */}
            <p className={DECISION_NOTE}>
              {foundNothing && article.status !== "published" ? (
                <>
                  Retrieval returned no usable sources, so there is nothing here
                  for a reader to check. Reject it with a reason — that reason is
                  what the query gets fixed from.
                </>
              ) : article.status === "published" ? (
                <>
                  Live at{" "}
                  <a
                    href={`/a/${article.slug}`}
                    target="_blank"
                    rel="noreferrer"
                    className={DECISION_LINK}
                  >
                    /a/{article.slug} ↗
                  </a>
                </>
              ) : (
                <>
                  Publishing puts this on the public feed at <code>/a/{article.slug}</code>,
                  recorded against your name.
                </>
              )}
            </p>

            <input
              value={rejectReason}
              onChange={(event) => {
                setRejectReason(event.target.value);
                setReasonMissing(false);
              }}
              disabled={!canDiscard}
              aria-label={canApprove ? "Reason for rejecting" : "Reason for discarding"}
              placeholder={
                canApprove
                  ? "Reason for rejecting (required)"
                  : "Reason for discarding (required)"
              }
              className={REJECT_REASON_FIELD}
            />

            <button
              onClick={onReject}
              disabled={!canDiscard || reject.isPending}
              title={
                canDiscard
                  ? "Removes this draft from the queue. The reason is kept."
                  : "Already resolved"
              }
              className={REJECT_BUTTON}
            >
              {canApprove ? "Reject" : "Discard"}
            </button>

            {reasonMissing ? (
              <p role="alert" className={REASON_MISSING_ALERT}>
                A reason is required before a draft can be rejected.
              </p>
            ) : null}

            {actionError ? (
              <p role="alert" className={ACTION_ERROR_ALERT}>
                {actionError}
              </p>
            ) : null}

            <p className={DECISION_NOTE}>
              Approval writes your name, the timestamp and the edited copy. The
              model's original draft is kept unchanged.
            </p>
          </div>
        </aside>
      </div>
    </main>
  );
}

/**
 * Surface the server's detail verbatim where there is one.
 *
 * The 409 from approve says *which* handles no longer resolve; replacing that
 * with "Publish failed" would send the reviewer hunting through the body.
 */
function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    const detail = (error.detail as { detail?: string } | undefined)?.detail;
    if (detail) return detail;
    if (error.status === 409) return "This draft can no longer be published.";
  }
  return error instanceof Error ? error.message : "Something went wrong.";
}
