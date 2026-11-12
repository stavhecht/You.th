/**
 * The review queue.
 *
 * Every tab is newest-first: the reviewer is usually here for the draft that
 * just finished. Old drafts stranding behind fresher ones is a real cost, but
 * each row states its own age ("queued 3 days ago"), so it stays visible.
 *
 * The "Failed validation" tab is not an error log. Those drafts can never be
 * approved, but a run of them is the signal that the synthesis prompt has
 * regressed - hiding them would make the grounding check look like silence
 * rather than like enforcement.
 *
 * Each row carries both signals at once: the verdict mark on the left, the
 * evidence grade bar on the right. A reviewer triaging twenty drafts is really
 * asking "which of these is a confident verdict standing on thin evidence?",
 * and that is a question about the *pair*, answerable here without opening one.
 */

import { useEffect, useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { LoaderCircle, Trash2 } from "lucide-react";

import {
  consoleKeys,
  createRun,
  fetchQueue,
  fetchRuns,
  rejectArticle,
} from "@/lib/api/console";
import { GradeBar } from "@/features/evidence/GradeBar";
import { VerdictMark } from "@/features/evidence/VerdictMark";
import { GRADE_LABELS, VERDICT_LABELS } from "@/features/evidence/labels";
import { subjectBorderLeft } from "@/features/evidence/subject";
import type { ArticleStatus, PipelineRun, QueueItem } from "@/types/api";
import { ContactInbox, WorkerStatusLine } from "./DeskStatus";
import { ResearchPanel } from "./ResearchPanel";
import { SocialPanel } from "./SocialPanel";
import { TrendCandidates } from "./TrendCandidates";
import { PRIMARY } from "./controls";
import {
  CONSOLE_PAGE,
  DISCARD_BUTTON,
  NEW_RUN_BLURB_FIELD,
  NEW_RUN_CONFIRMATION,
  NEW_RUN_ERROR,
  NEW_RUN_FORM,
  NEW_RUN_ROW,
  NEW_RUN_TOPIC_FIELD,
  PENDING_RUN_KICKER,
  PENDING_RUN_NOTE,
  PENDING_RUN_ROW,
  PENDING_RUN_SPINNER,
  PENDING_RUN_TOPIC,
  QUEUE_FOOTNOTE,
  QUEUE_HEADER,
  QUEUE_LIST,
  QUEUE_MESSAGE,
  QUEUE_ROW_ASIDE,
  QUEUE_ROW_ERROR,
  QUEUE_ROW_GRADE,
  QUEUE_ROW_GRADE_LABEL,
  QUEUE_ROW_HEADLINE,
  QUEUE_ROW_LINK,
  QUEUE_ROW_META,
  QUEUE_ROW_SIGNALS,
  QUEUE_STANDFIRST,
  QUEUE_TITLE,
  QUEUE_VALIDATION_BADGE,
  QUEUE_VERDICT_WORDING,
  TAB_BAR,
  TAB_COUNT,
  RETRACTED_FLAG,
  WEAK_EVIDENCE_FLAG,
  queueRow,
  queueTab,
} from "./styles";

/**
 * Tab copy is reviewer-facing wording, not the status name. `validation_failed`
 * stays the status everywhere else - it is the API contract, the enum in the
 * database and what the invariant #2 tests grep for; only the label a reviewer
 * reads is softened here. "Failed validation" described the machinery rather
 * than the draft, and read as something broken in the system rather than a
 * judgment about the article.
 *
 * The label narrows the tab's meaning: `verdict_exceeds_grade` is only one of
 * five codes that land here, and the other four (a hallucinated handle, an
 * unresolvable source, an uncited beat or section) are about missing citations
 * rather than weak ones. The per-draft message in `ValidationSummary` still
 * names which it was, so the specific reason is one click away.
 */
const TABS: { status: ArticleStatus; label: string }[] = [
  { status: "pending_review", label: "Pending review" },
  { status: "validation_failed", label: "Evidence too weak" },
  { status: "published", label: "Published" },
  { status: "rejected", label: "Rejected" },
  { status: "withdrawn", label: "Withdrawn" },
];

export function ReviewQueue() {
  const [status, setStatus] = useState<ArticleStatus>("pending_review");

  const { data, status: queryStatus } = useQuery({
    queryKey: consoleKeys.queue(status),
    queryFn: () => fetchQueue({ status }),
  });

  // Runs still generating. Only pending review shows them - they are drafts
  // that have not happened yet, so they belong to no other tab. The hook runs
  // on every tab regardless, so a run finishing while the reviewer is reading
  // Published still refreshes the queue underneath them.
  const inFlight = useInFlightRuns();
  const generating = status === "pending_review" ? inFlight : [];

  // Without this the note under a stalled run never updates: see the comment on
  // `useTick`.
  useTick(generating.length > 0, 30_000);

  return (
    <main className={CONSOLE_PAGE}>
      <div className={QUEUE_HEADER}>
        <div>
          <h1 className={QUEUE_TITLE}>Review queue</h1>
          <p className={QUEUE_STANDFIRST}>
            Newest first. Nothing publishes without an approval recorded against a
            named reviewer.
          </p>
        </div>
        {/* Who is signed in, and signing out, moved to `ReviewHeader` when
            the desk gained its own chrome - repeating them here would be two
            sign-out buttons on one screen. */}
      </div>

      {/* Above the free-text box on purpose: both start a run, and the ranked
          lists are the evidence-backed version of typing a topic from memory.
          Internet trends first (what readers are asking), then the literature
          scan (what researchers are publishing). */}
      <WorkerStatusLine />
      <ResearchPanel />
      <TrendCandidates />
      <SocialPanel />
      <ContactInbox />
      <NewRunForm />

      <nav className={TAB_BAR}>
        {TABS.map((tab) => (
          <button
            key={tab.status}
            onClick={() => setStatus(tab.status)}
            aria-current={status === tab.status ? "page" : undefined}
            className={queueTab(status === tab.status)}
          >
            {tab.label}
            {/* Only the open tab's count is known - the queue is fetched one
                status at a time. Showing all four would mean four requests on
                every console load, or a count endpoint that does not exist. */}
            {status === tab.status && data ? (
              <span className={TAB_COUNT}>{data.items.length}</span>
            ) : null}
          </button>
        ))}
      </nav>

      {queryStatus === "pending" ? (
        <p className={QUEUE_MESSAGE}>Loading…</p>
      ) : (data?.items.length ?? 0) === 0 && generating.length === 0 ? (
        <p className={QUEUE_MESSAGE}>
          {status === "pending_review" ? "Nothing waiting for review." : "Nothing here."}
        </p>
      ) : (
        <ul className={QUEUE_LIST}>
          {/* Above the queue, continuing its newest-first order: these have not
              finished, so they are newer than every row below, and the reviewer
              who just submitted a topic is looking for exactly this row. They
              are also the only rows whose state changes while being looked at. */}
          {generating.map((run) => (
            <PendingRunRow key={run.id} run={run} />
          ))}
          {data?.items.map((item) => (
            <QueueRow key={item.id} item={item} tab={status} />
          ))}
        </ul>
      )}

      <p className={QUEUE_FOOTNOTE}>
        Failed-validation drafts stay visible: a run of them is how a synthesis-prompt
        regression shows itself.
      </p>
    </main>
  );
}

/* ── runs in flight ─────────────────────────────────────────────────────── */

/** How often to ask whether a generating run has landed. */
const RUN_POLL_MS = 5_000;

/**
 * When "a couple of minutes" stops being true.
 *
 * Generous on purpose - synthesis alone is a model call, and four scholarly
 * APIs sit in front of it behind a throttle. This is the point at which a
 * reassuring message would start misinforming, not the point at which the run
 * is late.
 */
const SLOW_RUN_AFTER_MS = 10 * 60_000;

function isInFlight(run: PipelineRun): boolean {
  return run.status === "queued" || run.status === "running";
}

/**
 * The runs still generating, plus a refresh of the queue when one lands.
 *
 * Polling stops the moment nothing is in flight, so an idle console makes one
 * request per load rather than one every five seconds. `refetchIntervalIn
 * Background` is left at its default, so a backgrounded tab does not poll at
 * all.
 */
function useInFlightRuns(): PipelineRun[] {
  const queryClient = useQueryClient();

  const { data } = useQuery({
    queryKey: consoleKeys.runs,
    queryFn: () => fetchRuns(),
    // Live state, unlike the rest of the console - the global 60s staleTime
    // would otherwise serve a cached snapshot of a run that has since finished.
    staleTime: 0,
    refetchInterval: (query) =>
      query.state.data?.items.some(isInFlight) ? RUN_POLL_MS : false,
  });

  // A run leaving the in-flight set is the moment its draft becomes reviewable.
  // The queue has a 60s staleTime and no polling of its own, so without this
  // the spinner would vanish and the row it stood in for would not appear until
  // the reviewer reloaded the page - the exact gap this feature exists to close.
  const watching = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!data) return;
    const current = new Set(data.items.filter(isInFlight).map((run) => run.id));
    const landed = [...watching.current].some((id) => !current.has(id));
    watching.current = current;
    if (landed) void queryClient.invalidateQueries({ queryKey: consoleKeys.queues });
  }, [data, queryClient]);

  return data?.items.filter(isInFlight) ?? [];
}

/**
 * A re-render on a timer, for as long as something is generating.
 *
 * The poll alone is not enough. A queued run that no worker ever claims returns
 * byte-identical JSON every time; react-query's structural sharing keeps the
 * same object and nothing re-renders, so an elapsed-time message computed in
 * render would freeze at whatever it said on first paint - and that stalled run
 * is precisely the case the message exists to report.
 */
function useTick(active: boolean, everyMs: number): void {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setTick((n) => n + 1), everyMs);
    return () => clearInterval(id);
  }, [active, everyMs]);
}

/**
 * A draft being generated: the row that stands where the finished one will be.
 *
 * It carries the topic rather than a placeholder headline, because the topic is
 * what the reviewer typed and is the only thing that identifies which of
 * several runs this is. There is no verdict, grade or discard action - none of
 * them exist yet, and stubbing them greyed-out would imply the row is a draft
 * in a worse state rather than one that has not been written.
 *
 * `aria-live` without `role="status"`, which would replace the row's own
 * `listitem` role and drop it out of the list for anyone navigating by one.
 * `polite` because the thing it announces is the note changing under a run that
 * has stopped behaving - worth hearing at a pause, not worth an interruption.
 */
function PendingRunRow({ run }: { run: PipelineRun }) {
  return (
    <li className={PENDING_RUN_ROW} aria-live="polite">
      <div className={QUEUE_ROW_SIGNALS}>
        <LoaderCircle size={13} className={PENDING_RUN_SPINNER} aria-hidden />
        <span className={PENDING_RUN_KICKER}>Generating</span>
      </div>

      <p className={PENDING_RUN_TOPIC}>{run.topic}</p>
      <p className={PENDING_RUN_NOTE}>{runNote(run)}</p>
    </li>
  );
}

/**
 * What to say under the spinner.
 *
 * "A couple of minutes" is true of a healthy run and a lie about a stalled one,
 * and the distinction is not hypothetical here: compose starts the API without
 * the worker (CLAUDE.md), so "queued and nothing is consuming the queue" is a
 * state reached routinely - and a spinner promising minutes would be the only
 * thing on screen getting it wrong. Past the threshold the message says which
 * of the two it is, because "queued" and "running" have different causes and
 * different fixes.
 */
function runNote(run: PipelineRun): string {
  const elapsed = Date.now() - new Date(run.createdAt).getTime();

  if (elapsed > SLOW_RUN_AFTER_MS) {
    return run.status === "queued"
      ? "Still queued: no worker has picked this up yet."
      : "Still working. This one is taking longer than usual.";
  }

  // >1 means a retryable failure requeued it. Worth saying: the reviewer is
  // otherwise watching a spinner whose elapsed time no longer means anything.
  if (run.attempts > 1) {
    return `Retrying, attempt ${run.attempts} - usually takes a couple of minutes.`;
  }

  return "Article in the making - usually takes a couple of minutes.";
}

/**
 * The reject reason a queue-row discard sends.
 *
 * `RejectRequest.reason` is `min_length=1`, and the queue no longer asks - so
 * this says where the rejection came from rather than inventing a reason nobody
 * gave. Anything that reads reasons for prompt-fixing signal can skip it on
 * sight, which a blank string would not allow.
 */
const QUEUE_DISCARD_REASON = "Discarded from the queue without a reason.";

/**
 * One queue row, with its discard action.
 *
 * The action sits *beside* the link rather than inside it - a button nested in
 * an anchor is invalid markup and swallows the click on some browsers.
 *
 * Discard is a rejection, not a delete. There is no DELETE on articles by
 * design: the reason a draft was thrown away is the best available signal for
 * fixing the synthesis prompt. Rejected rows stay readable under the Rejected
 * tab. It is offered on validation_failed too, because otherwise those drafts
 * have no action at all and pile up in a tab nobody can clear.
 *
 * It does not ask for a reason. The queue is where a reviewer clears rows they
 * have already judged from the row itself, and a modal prompt per row turned
 * that into a typing exercise - the reasons it collected were placeholders, not
 * signal. The reason a rejection *is* worth writing down gets typed on the
 * review screen, next to the draft it is about.
 */
function QueueRow({ item, tab }: { item: QueueItem; tab: ArticleStatus }) {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const discardable = tab === "pending_review" || tab === "validation_failed";

  const discard = useMutation({
    mutationFn: () => rejectArticle(item.id, QUEUE_DISCARD_REASON),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["console"] }),
    onError: () => setError("Could not discard this draft."),
  });

  return (
    <li className={queueRow(subjectBorderLeft(item.subject ?? null))}>
      <Link to={`/review/article/${item.id}`} className={QUEUE_ROW_LINK}>
        <div className={QUEUE_ROW_SIGNALS}>
          <VerdictMark verdict={item.verdict} size="sm" />
          <span className={QUEUE_VERDICT_WORDING}>{VERDICT_LABELS[item.verdict]}</span>
          <span className={QUEUE_VALIDATION_BADGE}>{item.validationBadge}</span>
          {item.hasWeakEvidence ? (
            <span className={WEAK_EVIDENCE_FLAG}>Rests on weak study types</span>
          ) : null}
          {item.retractionFlagged ? (
            <span className={RETRACTED_FLAG}>A cited source was retracted</span>
          ) : null}
        </div>

        <p className={QUEUE_ROW_HEADLINE}>{item.headline}</p>
        <p className={QUEUE_ROW_META}>
          {item.topic} · queued {relativeAge(item.createdAt)}
        </p>
        {error ? <p className={QUEUE_ROW_ERROR}>{error}</p> : null}
      </Link>

      <div className={QUEUE_ROW_ASIDE}>
        <div className={QUEUE_ROW_GRADE}>
          <GradeBar grade={item.evidenceGrade} />
          <span className={QUEUE_ROW_GRADE_LABEL}>
            {GRADE_LABELS[item.evidenceGrade]}
          </span>
        </div>

        {discardable ? (
          <button
            onClick={() => {
              setError(null);
              discard.mutate();
            }}
            disabled={discard.isPending}
            title="Moves this draft to Rejected. The reason is kept."
            aria-label={`Discard ${item.headline}`}
            className={DISCARD_BUTTON}
          >
            <Trash2 size={14} aria-hidden />
          </button>
        ) : null}
      </div>
    </li>
  );
}

/**
 * Queue a topic for generation.
 *
 * Returns 202 - generation takes minutes - so this confirms submission rather
 * than waiting for a draft. The confirmation says plainly that nothing it
 * produces can publish itself.
 */
function NewRunForm() {
  const [topic, setTopic] = useState("");
  const [blurb, setBlurb] = useState("");
  const queryClient = useQueryClient();

  const submit = useMutation({
    mutationFn: () => createRun(topic.trim(), blurb.trim() || undefined),
    onSuccess: () => {
      setTopic("");
      setBlurb("");
      void queryClient.invalidateQueries({ queryKey: consoleKeys.runs });
    },
  });

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (topic.trim()) submit.mutate();
  }

  return (
    <form onSubmit={onSubmit} className={NEW_RUN_FORM}>
      <div className={NEW_RUN_ROW}>
        <input
          value={topic}
          onChange={(event) => setTopic(event.target.value)}
          placeholder="Topic, e.g. ashwagandha for stress"
          aria-label="Topic to generate a draft for"
          className={NEW_RUN_TOPIC_FIELD}
        />
        <button
          type="submit"
          disabled={!topic.trim() || submit.isPending}
          className={PRIMARY}
        >
          {submit.isPending ? "Queueing…" : "Generate draft"}
        </button>
      </div>

      <textarea
        value={blurb}
        onChange={(event) => setBlurb(event.target.value)}
        placeholder="Optional marketing copy. Claims are extracted only from what it actually says"
        aria-label="Marketing copy to extract claims from"
        rows={2}
        className={NEW_RUN_BLURB_FIELD}
      />

      {submit.isSuccess ? (
        // The queue below now shows the run generating, so this no longer has
        // to promise that something will happen - only the part the row cannot
        // say, which is what happens when it finishes.
        <p className={NEW_RUN_CONFIRMATION}>
          Queued. Nothing it produces publishes without your approval.
        </p>
      ) : null}
      {submit.isError ? (
        <p className={NEW_RUN_ERROR}>Could not queue that topic.</p>
      ) : null}
    </form>
  );
}

/**
 * "3 hours ago" / "yesterday" / "2 days ago".
 *
 * Relative rather than absolute because the queue's job is triage: how long a
 * draft has been waiting is the actionable fact, and a timestamp makes the
 * reviewer do that subtraction themselves on every row.
 */
function relativeAge(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "recently";

  const minutes = Math.max(0, Math.round((Date.now() - then) / 60_000));
  if (minutes < 60) return minutes <= 1 ? "just now" : `${minutes} minutes ago`;

  const hours = Math.round(minutes / 60);
  if (hours < 24) return hours === 1 ? "an hour ago" : `${hours} hours ago`;

  const days = Math.round(hours / 24);
  return days === 1 ? "yesterday" : `${days} days ago`;
}
