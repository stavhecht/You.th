/**
 * Social listening and product discovery - the desk half of `app/social` and
 * `app/products`.
 *
 * The scheduler collects YouTube daily and analyses the week; the buttons here
 * queue the same runs now. Nothing here is generated until a reviewer presses
 * Generate draft, and nothing generated publishes without an approval.
 *
 * The rules `ResearchPanel` follows hold here too, and matter more:
 *
 * **Popularity is not evidence.** Momentum (creators, uploads, views) and the
 * literature count are separate lines, and the literature line says it is a
 * count of papers, not a verdict. A video's views say nothing about whether
 * the thing works.
 *
 * **Unmeasured is not zero.** A week the collection could not cover reads "not
 * measured", a platform that is not collected says why (Instagram needs Meta's
 * app review; TikTok has no commercial API), and a failed source is named.
 *
 * **Each related item keeps its own system's numbers.** A trend-search topic
 * shows its trend score, a MeSH substance its paper count; nothing is merged.
 */

import { useEffect, useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, LoaderCircle } from "lucide-react";

import {
  consoleKeys,
  dismissSocialCandidate,
  fetchCollectionRuns,
  fetchProducts,
  fetchRelated,
  fetchSocialResult,
  promoteSocialCandidate,
  startCollectionRun,
} from "@/lib/api/console";
import { ApiError } from "@/lib/api/client";
import type {
  CollectionKind,
  CollectionRun,
  Product,
  RelatedItem,
  SocialTopicResult,
  SourceStatus,
} from "@/types/api";
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
  CANDIDATES_EMPTY,
  CANDIDATES_ERROR,
  CANDIDATES_HEADER,
  CANDIDATES_LIST,
  CANDIDATES_PANEL,
  CANDIDATES_SCAN,
  CANDIDATES_SCAN_NOTE,
  CANDIDATES_TITLE,
  CANDIDATES_TOGGLE,
  RESEARCH_DONE,
  RESEARCH_SCORE,
  RESEARCH_SOURCE_LINK,
  RESEARCH_SOURCES,
  RESEARCH_SUBHEAD,
  candidatesChevron,
} from "./styles";

const POLL_MS = 15_000;

const RUN_BUTTONS: { kind: CollectionKind; label: string; title: string }[] = [
  {
    kind: "social_daily",
    label: "Collect now",
    title: "Refreshes tracked YouTube videos and searches for new ones. No model calls.",
  },
  {
    kind: "social_weekly",
    label: "Analyse week",
    title: "Reads new posts with the local model, measures each topic and proposes the movers.",
  },
  {
    kind: "product_discovery",
    label: "Find products",
    title: "Reads feeds and news for product names, then looks for official pages and FDA records.",
  },
];

const KIND_WORDS: Record<string, string> = {
  research_topic: "Trend search",
  social_topic: "Social",
  mesh_substance: "MeSH",
  product: "Product",
};

function plural(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

function describeError(failure: unknown, fallback: string): string {
  if (failure instanceof ApiError) {
    const detail = (failure.detail as { detail?: string } | undefined)?.detail;
    if (detail) return detail;
  }
  return fallback;
}

/** Only http(s) links reach an href: these came off the open web. */
function safe(url: string | null | undefined): url is string {
  return Boolean(url && /^https?:\/\//i.test(url));
}

/** Platforms and sources that did not answer, with why. */
function statusLine(statuses: Record<string, SourceStatus> | null | undefined): string | null {
  if (!statuses) return null;
  const parts = Object.entries(statuses).map(([name, s]) =>
    s.status === "SUCCESS" || s.status === "EMPTY"
      ? `${name} ${s.status.toLowerCase()} (${plural(s.items, "item")})`
      : `${name} ${s.status.toLowerCase().replace("_", " ")}${s.detail ? `: ${s.detail}` : ""}`,
  );
  return parts.length ? parts.join(" · ") : null;
}

function momentumLine(topic: SocialTopicResult): string {
  return `Momentum ${topic.momentum.status.replace("_", " ")}: ${topic.momentum.reason}`;
}

function weeksLine(topic: SocialTopicResult): string {
  const names = ["this week", "last week", "the week before"];
  return topic.metrics.windows
    .map((w, i) =>
      w.covered
        ? `${names[i]} ${plural(w.uploads ?? 0, "upload")} by ${plural(w.creators ?? 0, "creator")}${w.truncated ? " (at least)" : ""}`
        : `${names[i]} not measured (${w.covered_days} of 7 days collected)`,
    )
    .join(" · ");
}

function popularityLine(topic: SocialTopicResult): string {
  const m = topic.metrics;
  const parts = [];
  if (m.median_views_per_day != null)
    parts.push(`median ${Math.round(m.median_views_per_day).toLocaleString()} views a day`);
  parts.push(
    m.view_velocity == null
      ? "view velocity not measured yet"
      : `${Math.round(m.view_velocity).toLocaleString()} views a day gained across ${plural(m.velocity_posts, "post")}`,
  );
  if (m.engagement_rate != null)
    parts.push(
      `engagement ${(m.engagement_rate * 100).toFixed(1)}% (${plural(m.engagement_posts, "post")})`,
    );
  return `Popularity: ${parts.join(" · ")}`;
}

function evidenceLine(topic: SocialTopicResult): string {
  const e = topic.evidence;
  if (!e.counts) return `Literature not checked${e.reason ? `: ${e.reason}` : ""}.`;
  const c = e.counts;
  if (c.total === 0) {
    return "No papers found by this article's search. A draft would likely read “no evidence”.";
  }
  return (
    `Literature ${e.status}: ${plural(c.total, "paper")}, ${c.reviews_or_meta} reviews/meta-analyses, ` +
    `${plural(c.rcts, "trial")} (${e.source === "europe_pmc" ? "Europe PMC, PubMed was down" : "PubMed"}). ` +
    "A count of papers, not a verdict."
  );
}

function relatedLine(item: RelatedItem): string {
  const own = item.own;
  const numbers =
    item.kind === "research_topic"
      ? own.trend_score == null
        ? "no trend score yet"
        : `trend score ${Math.round(Number(own.trend_score))}${own.trend_status ? `, ${own.trend_status}` : ""}`
      : item.kind === "mesh_substance"
        ? own.candidate_topic
          ? `${own.candidate_topic}: ${own.paper_count} papers (usually ${Math.round(Number(own.baseline_count))})`
          : "no MeSH proposal"
        : item.kind === "product"
          ? `${own.status}, launch ${own.launch_status}`
          : `momentum ${own.momentum ?? "not measured"}`;
  return `${KIND_WORDS[item.kind] ?? item.kind}: ${item.label} (${numbers}; ${item.matched_by})`;
}

function TopicRow({ topic }: { topic: SocialTopicResult }) {
  const queryClient = useQueryClient();
  const [dismissing, setDismissing] = useState(false);
  const [reason, setReason] = useState("");
  const [showRelated, setShowRelated] = useState(false);
  const related = useQuery({
    queryKey: consoleKeys.related("social_topic", topic.topic_id),
    queryFn: () => fetchRelated("social_topic", topic.topic_id),
    enabled: showRelated,
  });
  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: consoleKeys.socialResult });
    void queryClient.invalidateQueries({ queryKey: consoleKeys.runs });
  };
  const promote = useMutation({
    mutationFn: () => promoteSocialCandidate(topic.candidate_id),
    onSuccess: invalidate,
  });
  const dismiss = useMutation({
    mutationFn: () => dismissSocialCandidate(topic.candidate_id, reason.trim()),
    onSuccess: invalidate,
  });
  const busy = promote.isPending || dismiss.isPending;
  const decided = topic.status !== "proposed";
  const posts = topic.supporting_posts.filter((p) => safe(p.url));

  const onDismiss = (event: FormEvent) => {
    event.preventDefault();
    if (reason.trim()) dismiss.mutate();
  };

  return (
    <div className={CANDIDATE_ROW}>
      <div className={CANDIDATE_MAIN}>
        <p className={CANDIDATE_TOPIC}>{topic.question ?? topic.label}</p>
        {topic.question ? <p className={RESEARCH_SCORE}>{topic.label}</p> : null}
        <p className={CANDIDATE_MIX}>{momentumLine(topic)}</p>
        <p className={RESEARCH_SCORE}>{weeksLine(topic)}</p>
        <p className={RESEARCH_SCORE}>{popularityLine(topic)}</p>
        <p
          className={
            topic.evidence.counts?.total === 0
              ? `${CANDIDATE_EVIDENCE} font-bold text-ink`
              : CANDIDATE_EVIDENCE
          }
        >
          {evidenceLine(topic)}
        </p>
        {posts.length ? (
          <ul className={RESEARCH_SOURCES} aria-label="Posts behind this topic">
            {posts.map((post) => (
              <li key={post.url}>
                <a
                  className={RESEARCH_SOURCE_LINK}
                  href={post.url}
                  target="_blank"
                  rel="noopener noreferrer nofollow"
                >
                  {post.title}
                </a>
                {post.creator ? ` · ${post.creator}` : ""}
                {post.views != null ? ` · ${post.views.toLocaleString()} views` : ""}
              </li>
            ))}
          </ul>
        ) : null}
        {showRelated && related.data?.length ? (
          <ul className={RESEARCH_SOURCES} aria-label="Related in other discovery systems">
            {related.data.slice(0, 6).map((item) => (
              <li key={`${item.kind}-${item.id}`}>{relatedLine(item)}</li>
            ))}
          </ul>
        ) : null}
        {showRelated && related.data?.length === 0 ? <p className={RESEARCH_SCORE}>Nothing related yet.</p> : null}
        {showRelated && related.isError ? <p className={CANDIDATES_ERROR}>Could not load related items.</p> : null}
        <p className={RESEARCH_SCORE}>Limits: {topic.limitations.join("; ")}.</p>
        {(promote.isError || dismiss.isError) && (
          <p className={CANDIDATES_ERROR}>
            {describeError(promote.error ?? dismiss.error, "Could not save that.")}
          </p>
        )}
      </div>

      <div className={CANDIDATE_ACTIONS}>
        {decided ? (
          <span className={RESEARCH_DONE}>
            {topic.status === "promoted" ? "Draft queued" : "Dismissed"}
          </span>
        ) : (
          <>
            <button
              type="button"
              className={PRIMARY}
              disabled={busy}
              onClick={() => promote.mutate()}
              title="Queues a full generation run. Nothing it produces publishes without your approval."
            >
              {promote.isPending ? (
                <LoaderCircle className="size-3.5 animate-spin" />
              ) : (
                "Generate draft"
              )}
            </button>
            <button
              type="button"
              className={SECONDARY}
              disabled={busy}
              onClick={() => setDismissing((open) => !open)}
            >
              Dismiss
            </button>
          </>
        )}
        <button
          type="button"
          className={SECONDARY}
          aria-expanded={showRelated}
          onClick={() => setShowRelated((value) => !value)}
        >
          {related.isFetching ? <LoaderCircle className="size-3.5 animate-spin" /> : "Related"}
        </button>
      </div>

      {dismissing && !decided ? (
        <form className={CANDIDATE_DISMISS_ROW} onSubmit={onDismiss}>
          <input
            className={CANDIDATE_DISMISS_FIELD}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="Why not? It returns after 28 days, or sooner if twice as many creators pick it up."
            aria-label={`Reason for dismissing ${topic.question ?? topic.label}`}
          />
          <button type="submit" className={SECONDARY} disabled={!reason.trim() || busy}>
            Save
          </button>
        </form>
      ) : null}
    </div>
  );
}

function ProductRow({ product }: { product: Product }) {
  // Asked for per product, on demand: relations for every row cost the server
  // hundreds of queries per page.
  const [showRelated, setShowRelated] = useState(false);
  const related = useQuery({
    queryKey: consoleKeys.related("product", product.id),
    queryFn: () => fetchRelated("product", product.id),
    enabled: showRelated,
  });
  const weeks = product.weeks
    .map((w, i) =>
      w.covered
        ? `${["this week", "last week", "the week before"][i]} ${plural(w.mentions ?? 0, "mention")}`
        : `${["this week", "last week", "the week before"][i]} not measured`,
    )
    .join(" · ");
  const quote = product.recent[0];
  return (
    <div className={CANDIDATE_ROW}>
      <div className={CANDIDATE_MAIN}>
        <p className={CANDIDATE_TOPIC}>
          {product.name}{" "}
          <span className={RESEARCH_SCORE}>
            {product.brand} · {product.category.replaceAll("_", " ")}
          </span>
        </p>
        <p className={CANDIDATE_MIX}>
          {product.status} ({plural(product.independentSources, "independent source")}) · launch{" "}
          {product.launchStatus}
          {product.fdaReference
            ? ` · FDA ${product.fdaReference.k_number}${product.fdaReference.decision_date ? ` (${product.fdaReference.decision_date.slice(0, 10)})` : ""}`
            : ""}
        </p>
        <p className={RESEARCH_SCORE}>{weeks}</p>
        {quote ? (
          <p className={RESEARCH_SCORE}>
            “{quote.quote}”{quote.publisher ? `, ${quote.publisher}` : ""} ({quote.mention_kind})
          </p>
        ) : null}
        <ul className={RESEARCH_SOURCES}>
          {safe(product.officialUrl) ? (
            <li>
              <a
                className={RESEARCH_SOURCE_LINK}
                href={product.officialUrl}
                target="_blank"
                rel="noopener noreferrer nofollow"
              >
                Official page
              </a>
            </li>
          ) : null}
          {(showRelated ? related.data : undefined)?.slice(0, 5).map((item) => (
            <li key={`${item.kind}-${item.id}`}>{relatedLine(item)}</li>
          ))}
        </ul>
        {showRelated && related.data?.length === 0 ? <p className={RESEARCH_SCORE}>Nothing related yet.</p> : null}
        {showRelated && related.isError ? <p className={CANDIDATES_ERROR}>Could not load related items.</p> : null}
      </div>
      <div className={CANDIDATE_ACTIONS}>
        <button
          type="button"
          className={SECONDARY}
          aria-expanded={showRelated}
          onClick={() => setShowRelated((open) => !open)}
        >
          {related.isFetching ? <LoaderCircle className="size-3.5 animate-spin" /> : "Related"}
        </button>
      </div>
    </div>
  );
}

export function SocialPanel() {
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();

  const runs = useQuery({
    queryKey: consoleKeys.collectionRuns,
    queryFn: () => fetchCollectionRuns(10),
    refetchInterval: (query) =>
      query.state.data?.some((r) => r.status === "queued" || r.status === "running")
        ? POLL_MS
        : false,
  });
  const result = useQuery({
    queryKey: consoleKeys.socialResult,
    queryFn: fetchSocialResult,
    retry: false,
  });
  const products = useQuery({
    queryKey: consoleKeys.products,
    queryFn: () => fetchProducts(20),
    enabled: open,
  });
  // When the last live run finishes, its proposals and products are new.
  const anyLive = Boolean(
    runs.data?.some((r) => r.status === "queued" || r.status === "running"),
  );
  const wasLive = useRef(false);
  useEffect(() => {
    if (wasLive.current && !anyLive) {
      void queryClient.invalidateQueries({ queryKey: consoleKeys.socialResult });
      void queryClient.invalidateQueries({ queryKey: consoleKeys.products });
    }
    wasLive.current = anyLive;
  }, [anyLive, queryClient]);

  const start = useMutation({
    mutationFn: startCollectionRun,
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: consoleKeys.collectionRuns }),
  });

  const active = (kind: CollectionKind): CollectionRun | undefined =>
    runs.data?.find((r) => r.kind === kind && (r.status === "queued" || r.status === "running"));
  const undecided = result.data?.topics.filter((t) => t.status === "proposed").length ?? 0;
  const noResult = result.error instanceof ApiError && result.error.status === 404;
  const platforms = statusLine(result.data?.collection.latest_daily);
  const live = runs.data?.filter((r) => r.status === "queued" || r.status === "running") ?? [];
  // Runs arrive newest first. The list below is the last weekly run that completed.
  const lastWeekly = runs.data?.find((r) => r.kind === "social_weekly");

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
          <span className={CANDIDATES_TITLE}>Social &amp; products</span>
        </button>
        {undecided > 0 ? (
          <span className={CANDIDATES_BADGE} aria-label={`${undecided} proposed`}>
            {undecided}
          </span>
        ) : null}
        {RUN_BUTTONS.map((button) => (
          <button
            key={button.kind}
            type="button"
            className={CANDIDATES_SCAN}
            disabled={Boolean(active(button.kind)) || start.isPending}
            onClick={() => start.mutate(button.kind)}
            title={button.title}
          >
            {active(button.kind) ? (
              <LoaderCircle className="size-3.5 animate-spin" aria-hidden />
            ) : null}
            {button.label}
          </button>
        ))}
      </div>

      {start.isError ? (
        <p className={CANDIDATES_SCAN_NOTE} role="status">
          {describeError(start.error, "Could not start that run.")}
        </p>
      ) : live.length ? (
        <p className={CANDIDATES_SCAN_NOTE} role="status">
          Running: {live.map((r) => r.kind.replace("_", " ")).join(", ")}. The worker takes them in
          turn; you can leave this page.
        </p>
      ) : null}
      {lastWeekly?.status === "failed" ? (
        <p className={CANDIDATES_SCAN_NOTE} role="status">
          The last weekly analysis failed
          {lastWeekly.error ? `: ${lastWeekly.error.message}` : ""}. Any proposals here are from the
          last one that finished.
        </p>
      ) : null}

      {open ? (
        <>
          {platforms ? (
            <p className={CANDIDATES_SCAN_NOTE}>Last daily collection: {platforms}</p>
          ) : null}
          {result.data?.notes.length ? (
            <p className={CANDIDATES_SCAN_NOTE}>{result.data.notes.join(" ")}</p>
          ) : null}
          <div className={CANDIDATES_LIST}>
            {noResult ? (
              <p className={CANDIDATES_EMPTY}>
                No weekly analysis yet. It needs daily collections to measure against; the
                scheduler runs both, or press Collect now and then Analyse week.
              </p>
            ) : result.isError ? (
              <p className={CANDIDATES_ERROR}>Could not load social listening.</p>
            ) : result.data && result.data.topics.length === 0 ? (
              <p className={CANDIDATES_EMPTY}>
                Nothing proposed this week: no topic had enough posts after the held-back ones.
              </p>
            ) : (
              result.data?.topics.map((topic) => <TopicRow key={topic.candidate_id} topic={topic} />)
            )}
          </div>

          <p className={RESEARCH_SUBHEAD}>Products</p>
          <div className={CANDIDATES_LIST}>
            {products.isError ? (
              <p className={CANDIDATES_ERROR}>Could not load products.</p>
            ) : products.data?.length === 0 ? (
              <p className={CANDIDATES_EMPTY}>No products seen yet.</p>
            ) : (
              products.data?.map((product) => <ProductRow key={product.id} product={product} />)
            )}
          </div>
        </>
      ) : null}
    </section>
  );
}
