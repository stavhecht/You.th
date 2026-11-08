/**
 * Trend proposals from the fortnightly scan.
 *
 * The evidence-backed counterpart to the free-text New-run box below it: same
 * action, but the topic came from counting what the literature is actually
 * doing rather than from what a reviewer happened to think of.
 *
 * Three decisions worth keeping.
 *
 * **Collapsed by default.** Eight always-open rows push the review queue below
 * the fold, and the queue is why a reviewer opened this screen. The badge in
 * the header is enough to say "there is something here" without spending the
 * space to prove it - and it is drawn only when the count is non-zero, because
 * a circled 0 is a mark that means "ignore me".
 *
 * **Run scan is here rather than only in cron.** A reviewer who has just
 * cleared the list should not wait until Monday to see whether anything moved.
 * It is safe to expose because a scan *proposes* - no model calls, no queued
 * run, no spend - so the money still sits behind Generate draft, one press
 * further in. It is styled as the quietest control in the panel for the same
 * reason: it costs nothing of ours and several minutes of NCBI's.
 *
 * **Every row shows its arithmetic.** "17 papers, usually 3" is the claim; the
 * score is not shown at all. The ranking is built from constants that were
 * guessed before any real data existed, so a reviewer needs to be able to
 * disagree with it - and a bare score invites either belief or dismissal, never
 * judgement. The study mix is there for the same reason: six case reports and
 * six randomised trials are the same surge and a very different proposition.
 *
 * Promoting spends a full generation run. Nothing it produces publishes without
 * an approval, so the button is `PRIMARY` rather than guarded - but it says so.
 */

import { useEffect, useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, LoaderCircle, RefreshCw } from "lucide-react";

import {
  consoleKeys,
  dismissCandidate,
  fetchCandidates,
  fetchTrendScan,
  promoteCandidate,
  startTrendScan,
} from "@/lib/api/console";
import { ApiError } from "@/lib/api/client";
import type { DiscoveryCandidate, TrendScan } from "@/types/api";
import { PRIMARY, SECONDARY } from "./controls";
import {
  CANDIDATE_ACTIONS,
  CANDIDATE_DISMISS_FIELD,
  CANDIDATE_DISMISS_ROW,
  CANDIDATE_EVIDENCE,
  CANDIDATE_MAIN,
  CANDIDATE_MIX,
  CANDIDATE_ROW,
  CANDIDATE_TOPIC,
  CANDIDATES_BADGE,
  CANDIDATES_COUNT,
  CANDIDATES_EMPTY,
  CANDIDATES_ERROR,
  CANDIDATES_HEADER,
  CANDIDATES_LIST,
  CANDIDATES_PANEL,
  CANDIDATES_SCAN,
  CANDIDATES_SCAN_NOTE,
  CANDIDATES_SUPPRESSED,
  CANDIDATES_SUPPRESSED_ROW,
  CANDIDATES_SUPPRESSED_TOPIC,
  CANDIDATES_SUPPRESSED_WHY,
  CANDIDATES_TITLE,
  CANDIDATES_TOGGLE,
  candidatesChevron,
} from "./styles";

/** Grades the pipeline itself treats as supported-tier evidence. */
const STRONG = new Set(["rct", "systematic_review", "meta_analysis"]);

const GRADE_WORDS: Record<string, string> = {
  meta_analysis: "meta-analysis",
  systematic_review: "systematic review",
  rct: "randomised trial",
  observational: "observational",
  narrative_review: "review",
  case_report: "case report",
  animal: "animal",
  in_vitro: "in vitro",
  unknown: "unclassified",
};

/**
 * The surge, in words.
 *
 * A baseline under one paper per window is reported as "new to this corpus"
 * rather than as a decimal: "usually 0.3 papers" is arithmetic pretending to be
 * an observation, and the honest claim is that we have not seen this before.
 */
function evidenceLine(candidate: DiscoveryCandidate): string {
  const papers = `${candidate.paperCount} paper${candidate.paperCount === 1 ? "" : "s"}`;
  const surge =
    candidate.baselineCount < 1
      ? `${papers}, new to this corpus`
      : `${papers}, usually ${candidate.baselineCount.toFixed(1)} (${candidate.lift.toFixed(1)}x)`;
  // Only when the angle is narrower than the substance. Saying "8 papers · 8 on
  // this angle" is noise, and it would make the one case that matters - a broad
  // surge with a thin angle - look like every other row.
  if (candidate.anglePaperCount && candidate.anglePaperCount < candidate.paperCount) {
    return `${surge} · ${candidate.anglePaperCount} on this angle`;
  }
  return surge;
}

/** Strongest grades first, so the most load-bearing evidence reads first. */
function mixLine(studyMix: Record<string, number>): string {
  const entries = Object.entries(studyMix);
  if (entries.length === 0) return "";
  entries.sort(([a], [b]) => Number(STRONG.has(b)) - Number(STRONG.has(a)));
  return entries
    .map(([grade, count]) => `${count} ${GRADE_WORDS[grade] ?? grade}`)
    .join(", ");
}

function CandidateRow({ candidate }: { candidate: DiscoveryCandidate }) {
  const queryClient = useQueryClient();
  const [dismissing, setDismissing] = useState(false);
  const [reason, setReason] = useState("");

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: consoleKeys.candidates });
    // The promoted run appears in the in-flight panel immediately, which is the
    // whole reason promote returns the run rather than 204.
    void queryClient.invalidateQueries({ queryKey: consoleKeys.runs });
  };

  const promote = useMutation({
    mutationFn: () => promoteCandidate(candidate.id),
    onSuccess: invalidate,
  });
  const dismiss = useMutation({
    mutationFn: () => dismissCandidate(candidate.id, reason.trim()),
    onSuccess: invalidate,
  });

  const busy = promote.isPending || dismiss.isPending;

  const onDismiss = (event: FormEvent) => {
    event.preventDefault();
    if (reason.trim()) dismiss.mutate();
  };

  return (
    <div className={CANDIDATE_ROW}>
      <div className={CANDIDATE_MAIN}>
        <p className={CANDIDATE_TOPIC}>{candidate.question}</p>
        <p className={CANDIDATE_MIX}>{candidate.topic}</p>
        <p className={CANDIDATE_EVIDENCE}>{evidenceLine(candidate)}</p>
        {mixLine(candidate.studyMix) ? (
          <p className={CANDIDATE_MIX}>{mixLine(candidate.studyMix)}</p>
        ) : null}
        {(promote.isError || dismiss.isError) && (
          <p className={CANDIDATES_ERROR}>
            {(promote.error ?? dismiss.error) instanceof Error
              ? ((promote.error ?? dismiss.error) as Error).message
              : "Could not save that."}
          </p>
        )}
      </div>

      <div className={CANDIDATE_ACTIONS}>
        <button
          type="button"
          className={PRIMARY}
          disabled={busy}
          onClick={() => promote.mutate()}
          title="Queues a full generation run. Nothing it produces publishes without your approval."
        >
          {promote.isPending ? <LoaderCircle className="size-3.5 animate-spin" /> : "Generate draft"}
        </button>
        <button
          type="button"
          className={SECONDARY}
          disabled={busy}
          onClick={() => setDismissing((open) => !open)}
        >
          Dismiss
        </button>
      </div>

      {dismissing ? (
        <form className={CANDIDATE_DISMISS_ROW} onSubmit={onDismiss}>
          <input
            className={CANDIDATE_DISMISS_FIELD}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="Why not? Shown in the next scan's report."
            aria-label={`Reason for dismissing ${candidate.question}`}
          />
          <button type="submit" className={SECONDARY} disabled={!reason.trim() || busy}>
            Save
          </button>
        </form>
      ) : null}
    </div>
  );
}

/** While a scan is in flight. It is minutes long, so this is not a spinner. */
const SCAN_POLL_MS = 10_000;

/**
 * The scan's state, and a refresh of the list when one lands.
 *
 * Polling starts only when a scan is actually running, so an idle console makes
 * one request for this on load and then nothing. That single request is what
 * survives a reload mid-scan: the runner is process state, not component state,
 * so a reviewer who navigates away and back still sees it working.
 */
function useTrendScan(): TrendScan | undefined {
  const queryClient = useQueryClient();

  const { data } = useQuery({
    queryKey: consoleKeys.trendScan,
    queryFn: () => fetchTrendScan(),
    // Live state, like the in-flight runs - the global staleTime would serve a
    // snapshot of a scan that has since finished.
    staleTime: 0,
    refetchInterval: (query) =>
      query.state.data?.status === "running" ? SCAN_POLL_MS : false,
  });

  // A scan finishing is the moment the list beneath it becomes wrong. Nothing
  // else invalidates it: candidates have a staleTime and no polling of their
  // own, so without this the panel would keep showing the pre-scan ranking
  // under a message announcing the new one.
  const running = useRef(false);
  useEffect(() => {
    const now = data?.status === "running";
    if (running.current && !now) {
      void queryClient.invalidateQueries({ queryKey: consoleKeys.candidates });
    }
    running.current = now;
  }, [data, queryClient]);

  return data;
}

/**
 * What the scan is doing, in one line.
 *
 * Reports records as well as proposals because zero proposals is an ordinary
 * fortnight - most weeks nothing accelerates - while zero records means the
 * harvest itself came back empty, and a proposal count alone cannot tell a
 * reviewer which of those just happened.
 */
function scanLine(scan: TrendScan): string | null {
  if (scan.status === "running") {
    return "Scanning PubMed. A few minutes; you can leave this page.";
  }
  if (scan.status === "failed") {
    return `Scan failed: ${scan.error ?? "no reason given"}.`;
  }
  if (scan.status === "succeeded") {
    const head = `Scanned ${scan.recordsSeen} record${scan.recordsSeen === 1 ? "" : "s"}, refreshed ${scan.candidatesProposed} proposal${scan.candidatesProposed === 1 ? "" : "s"}.`;
    // Verbatim, and after the counts: a shallow baseline or a truncated seed is
    // the reason the counts read the way they do.
    return scan.notes.length ? `${head} ${scan.notes.join(" ")}` : head;
  }
  return null;
}

/** The server's own words where it has any - a 409 says a scan is already running. */
function describeError(failure: unknown): string {
  if (failure instanceof ApiError) {
    const detail = (failure.detail as { detail?: string } | undefined)?.detail;
    if (detail) return detail;
  }
  return "Could not start the scan.";
}

export function TrendCandidates() {
  // Not persisted. A reviewer who opened it once is not asking for it open
  // forever, and the queue below is the reason they are on this screen.
  const [open, setOpen] = useState(false);

  const { data, status } = useQuery({
    queryKey: consoleKeys.candidates,
    queryFn: () => fetchCandidates(),
  });

  const queryClient = useQueryClient();
  const scan = useTrendScan();
  const startScan = useMutation({
    mutationFn: () => startTrendScan(),
    onSuccess: (next) => queryClient.setQueryData(consoleKeys.trendScan, next),
  });

  // A press that collided with a running scan leaves an error React Query holds
  // until the next press. Clearing it when a scan lands means the reviewer is
  // told the result of the scan they collided with, rather than being left
  // looking at the collision.
  const { reset } = startScan;
  useEffect(() => reset(), [reset, scan?.finishedAt]);

  const count = data?.length ?? 0;
  const scanning = scan?.status === "running" || startScan.isPending;
  // A failed press outranks the last scan's result: it is the newer fact, and
  // it is almost always the 409 - a scan already running in this process,
  // started here or from another tab. `Request failed: 409` is not that
  // sentence, so the server's own words win where it has any.
  const pressFailed = startScan.isError ? describeError(startScan.error) : null;
  const note = pressFailed ?? (scan ? scanLine(scan) : null);

  return (
    <section className={CANDIDATES_PANEL}>
      <div className={CANDIDATES_HEADER}>
        <button
          type="button"
          className={CANDIDATES_TOGGLE}
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          <ChevronRight className={candidatesChevron(open)} aria-hidden />
          <span className={CANDIDATES_TITLE}>Trend candidates</span>
        </button>

        {status === "pending" ? (
          <span className={CANDIDATES_COUNT}>…</span>
        ) : count > 0 ? (
          <span className={CANDIDATES_BADGE} aria-label={`${count} proposed`}>
            {count}
          </span>
        ) : null}

        <button
          type="button"
          className={CANDIDATES_SCAN}
          disabled={scanning}
          onClick={() => startScan.mutate()}
          title={
            scan?.lastScanAt
              ? `Counts new MeSH tags on PubMed and re-ranks this list. No drafts are generated. Last scan ${new Date(scan.lastScanAt).toLocaleString()}.`
              : "Counts new MeSH tags on PubMed and re-ranks this list. No drafts are generated."
          }
        >
          {scanning ? (
            <LoaderCircle className="size-3.5 animate-spin" aria-hidden />
          ) : (
            <RefreshCw className="size-3.5" aria-hidden />
          )}
          {scanning ? "Scanning" : "Run scan"}
        </button>
      </div>

      {note ? (
        <p className={CANDIDATES_SCAN_NOTE} role="status">
          {note}
        </p>
      ) : null}

      {/* The topics behind the suppression count. This used to end "Run the CLI
          for the per-topic list", which is a dead end on a deployed stack:
          `scripts/` is deliberately outside the image. The list was computed and
          discarded server-side, so the one screen that needed it was the one
          screen that could not have it. */}
      {scan?.suppressed.length ? (
        <ul className={CANDIDATES_SUPPRESSED}>
          {scan.suppressed.map((item) => (
            <li key={item.topic} className={CANDIDATES_SUPPRESSED_ROW}>
              <span className={CANDIDATES_SUPPRESSED_TOPIC}>{item.topic}</span>
              <span className={CANDIDATES_SUPPRESSED_WHY}>{item.reason}</span>
            </li>
          ))}
        </ul>
      ) : null}

      {open ? (
        <div className={CANDIDATES_LIST}>
          {status === "error" ? (
            <p className={CANDIDATES_ERROR}>Could not load proposals.</p>
          ) : count === 0 ? (
            // Says which of the two silences this is. "Nothing here" would read
            // as a broken panel, and an empty desk is the *ordinary* outcome,
            // a floor, a quorum and a suppression rule exist to make it so.
            //
            // It no longer claims a cadence. It said "weekly" while nothing was
            // scheduled at all, and now that something is, the interval is a
            // setting this component cannot see; `lastScanAt` is a fact it has.
            <p className={CANDIDATES_EMPTY}>
              {scan?.lastScanAt
                ? `Nothing proposed. The scan runs on its own; the last one finished ${new Date(scan.lastScanAt).toLocaleString()}.`
                : "Nothing proposed. The scan runs on its own."}
            </p>
          ) : (
            data?.map((candidate) => (
              <CandidateRow key={candidate.id} candidate={candidate} />
            ))
          )}
        </div>
      ) : null}
    </section>
  );
}
