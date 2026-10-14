/**
 * Mirrors the backend response models.
 *
 * Hand-maintained for now. Once the API stabilises, generate these from the
 * OpenAPI schema FastAPI already produces — hand-written mirrors of a contract
 * drift, and they drift silently, because TypeScript happily validates against
 * a stale definition.
 *
 * Sources: backend/app/api/public/schemas.py, backend/app/api/console/schemas.py
 */

export type Verdict = "supported" | "mixed" | "weak" | "no_evidence";

/**
 * Which way one source's finding points for one claim, as the pipeline's
 * APPRAISE stage read its abstract. Relative to the claim as written: a trial
 * finding no harm for "X damages the kidneys" is `no_effect`.
 */
export type Stance = "supports" | "no_effect" | "contradicts" | "unclear" | "off_topic";

export type StudyType =
  | "unknown"
  | "in_vitro"
  | "animal"
  | "case_report"
  | "narrative_review"
  | "observational"
  | "rct"
  | "systematic_review"
  | "meta_analysis";

export type ArticleStatus =
  | "pending_review"
  | "published"
  | "rejected"
  | "validation_failed"
  | "draft_failed";

/**
 * What kind of thing is being assessed. Drives the one chromatic axis in the
 * design (see `features/evidence/subject.ts`).
 *
 * Set by a reviewer, never inferred — it cannot be derived from `product`,
 * which is free text, and a guessed subject would put a confident colour on an
 * unchecked classification. Still optional everywhere: an unclassified article
 * renders in ink, which is the design's resting state rather than a gap.
 */
export type Subject =
  | "fitness"
  | "nutrition"
  | "supplements"
  | "sleep_recovery"
  | "lifestyle"
  | "preventive_health"
  | "general_health"
  | "wellness"
  | "other";

/**
 * TipTap document node.
 *
 * Structurally compatible with TipTap's own `JSONContent`, so documents pass
 * into the editor without a cast — but declared here rather than imported from
 * `@tiptap/core`, so the public feed's renderer does not pull the editor
 * package into its bundle.
 */
export interface TipTapNode {
  type?: string;
  attrs?: Record<string, unknown>;
  content?: TipTapNode[];
  marks?: { type: string; attrs?: Record<string, unknown> }[];
  text?: string;
}

export interface TipTapDoc extends TipTapNode {
  type: "doc";
  content?: TipTapNode[];
}

// --- public ----------------------------------------------------------------

export interface FeedCard {
  slug: string;
  headline: string;
  excerpt: string;
  verdict: Verdict;
  verdictQualifier: string | null;
  /**
   * The article's own first picture, derived at publish time — never a
   * separately-uploaded thumbnail, so a tile cannot show something the article
   * does not. Null is the normal case, and the tile falls back to type.
   */
  image: string | null;
  imageAlt: string | null;
  publishedAt: string;
  subject: Subject | null;
}

/** A feed card plus the folder it sits on. Only ever from `/readers/saved`. */
export interface SavedCard extends FeedCard {
  folderId: string;
}

/**
 * The feed tile a draft *would* publish as. Console-only.
 *
 * Extends {@link FeedCard} rather than restating it, because the point of the
 * preview is that it renders through the same `ArticleCard` the public feed
 * uses. A shape of its own would let the two drift, and the drift is exactly
 * what the preview exists to catch.
 *
 * Derived server-side by the same `derive_card()` the publish path runs — see
 * `fetchCardPreview`.
 */
export interface CardPreview extends FeedCard {
  /**
   * True when the picture is the portrait cover the pipeline generated rather
   * than the article's own first image. The tile a reviewer is looking at is
   * then a different crop from the picture in the editor beside it, which
   * reads as a bug unless it is said out loud.
   */
  imageIsGeneratedCover: boolean;
}

/** One frame of a generated illustration. */
export interface GeneratedFrame {
  src: string;
  alt: string;
}

/**
 * Which of an article's two pictures. They live in different places and are
 * judged separately, so they can be redrawn separately.
 */
export type IllustrationFrame = "lead" | "cover";

/**
 * Both frames after a regenerate — always both, whether or not both were drawn.
 *
 * `lead` goes into the document — the client swaps the editor's image node and
 * lets autosave persist it, so the new `src` passes the server's media check
 * like any other edit. `cover` is already recorded on the article and reaches
 * the feed tile only while `lead` is still the document's first picture.
 *
 * The server answers with both even for a one-frame redraw, because the client's
 * job is to make the document agree with the article row and it cannot do that
 * from a partial answer.
 */
export interface GeneratedImagery {
  lead: GeneratedFrame;
  cover: GeneratedFrame;
  /** Which frames this call actually drew, as the server reports them. */
  redrawn: IllustrationFrame[];
}

export interface FeedPage {
  items: FeedCard[];
  nextCursor: string | null;
  /**
   * Null for the feed. For a search: `text` when articles matched the words,
   * `similar` when nothing did and these are titles spelled like the query —
   * which the page has to say, because a close spelling can be wrong.
   */
  match: "text" | "similar" | null;
}

/**
 * Published counts per subject and per verdict, for the browse drawer.
 *
 * Both maps are **sparse** — a key is absent when nothing is published under
 * it — so read them with a `?? 0`, never by assuming the enum is populated.
 * They also do not sum to `total`: an unclassified article is counted in the
 * total and in no subject, which is why "Everything" is genuinely larger than
 * the five categories added up.
 */
export interface FeedFacets {
  total: number;
  subjects: Partial<Record<Subject, number>>;
  verdicts: Partial<Record<Verdict, number>>;
}

export interface Source {
  title: string;
  journal: string | null;
  year: number | null;
  studyType: StudyType;
  citationHandle: string;
  url: string;
  pmid: string | null;
  doi: string | null;
  /**
   * The literature has withdrawn this paper since the article cited it. Shown
   * on the source itself, not only in the article banner — a reader who
   * scrolls to the citations should not have to infer which one it was.
   */
  retracted: boolean;
}

export interface Citation {
  claim: string;
  handles: string[];
}

export interface Article {
  slug: string;
  headline: string;
  summary: string;
  verdict: Verdict;
  verdictQualifier: string | null;
  product: string;
  targetClaims: string[];
  ingredients: string[];
  content: TipTapDoc;
  sources: Source[];
  citations: Citation[];
  evidenceGrade: StudyType;
  subject: Subject | null;
  publishedAt: string;
  /**
   * A cited source has been retracted since publication. The article stays up
   * and stays readable — one withdrawn source does not necessarily invalidate
   * a conclusion, and a human decides — but the reader is told first.
   */
  retractionNotice: boolean;
  disclaimer: string;
}

// --- console ---------------------------------------------------------------

export interface QueueItem {
  id: string;
  topic: string;
  headline: string;
  verdict: Verdict;
  status: ArticleStatus;
  evidenceGrade: StudyType;
  /** e.g. "4/4 citations resolve". Computed server-side at draft time. */
  validationBadge: string;
  hasWeakEvidence: boolean;
  createdAt: string;
  subject: Subject | null;
}

export interface ReviewSource extends Source {
  sourceId: string;
  claim: string;
  citationCount: number | null;
  /** False for sources retrieved but not cited — shown greyed, not hidden. */
  wasCited: boolean;
  relevanceScore: number | null;
  isWeakEvidence: boolean;
  /**
   * Distinct from {@link isWeakEvidence}: weak means a poor basis for
   * confidence, retracted means not a basis at all.
   */
  retracted: boolean;
  /** Under investigation, not withdrawn. Recorded, not refused. */
  concern: boolean;
  /** Which provider said so, e.g. "pubmed: retracted publication". */
  retractionNote: string | null;
  /**
   * Full-text passages the synthesis model was shown beside the abstract.
   * Empty for most sources; only a few open-access papers get them.
   */
  excerpts: Excerpt[];
  /**
   * Which way APPRAISE found this source pointing for `claim`. `null` means
   * not appraised (an older article, appraisal off, or the call failed), which
   * is not the same as `unclear`.
   */
  stance: Stance | null;
}

/** A passage from a paper's full text. `section` is its heading, e.g. "Results". */
export interface Excerpt {
  section: string | null;
  text: string;
}

export interface ValidationFailure {
  code: string;
  message: string;
  detail: Record<string, unknown>;
}

export interface ValidationReport {
  passed: boolean;
  citationsTotal: number;
  citationsResolved: number;
  bestEvidenceGrade: StudyType;
  /**
   * The strongest verdict the cited evidence would have allowed.
   *
   * The cap is one-sided: it fails a verdict that is too strong and says
   * nothing about one that is too weak, so an under-confident draft is
   * indistinguishable from a correct cautious call unless the ceiling is shown
   * next to it. `null` on drafts written before this was recorded, which must
   * render as "not recorded" rather than as any particular verdict.
   */
  verdictCeiling: Verdict | null;
  failures: ValidationFailure[];
  /**
   * Problems that did not block the draft but need a reviewer's eye — a
   * "no evidence" verdict that still cites studies, kept after one rewrite.
   * Absent on reports written before warnings existed.
   */
  warnings?: ValidationFailure[];
}

export interface ArticleDetail {
  id: string;
  /**
   * Where the article lives on the public feed once published. Sent for every
   * draft — the slug is assigned at persist time — so the console gates the
   * "view on the feed" link on `status`, not on this being set.
   */
  slug: string;
  subject: Subject | null;
  status: ArticleStatus;
  topic: string;
  product: string;
  targetClaims: string[];
  ingredients: string[];
  headline: string;
  summary: string;
  verdict: Verdict;
  verdictQualifier: string | null;
  evidenceGrade: StudyType;
  /** The immutable AI draft. Never sent back to the server. */
  originalContent: TipTapDoc;
  /** Null until the first autosave. The editor loads `edited ?? original`. */
  editedContent: TipTapDoc | null;
  validationReport: ValidationReport;
  sources: ReviewSource[];
  pipelineRunId: string | null;
  /**
   * Set when a cited source has been retracted since this article was written.
   * The article keeps its status — this raises it for a human, it does not
   * withdraw it.
   */
  retractionFlaggedAt: string | null;
  retractionDetail: Record<string, unknown> | null;
  createdAt: string;
}

/**
 * A just-uploaded image. No id and no record: the store is content-addressed,
 * so the path is the identity, and an image is referenced only by the document
 * that embeds it.
 */
export interface MediaUpload {
  /** Origin-relative, e.g. `/api/media/1f/2a….png`. Goes into the image node. */
  src: string;
  /** Sniffed from the bytes — not the Content-Type the upload claimed. */
  contentType: string;
  bytes: number;
}

export interface Reviewer {
  id: string;
  email: string;
  displayName: string;
  role: "admin" | "reviewer";
}

// --- pipeline --------------------------------------------------------------

export type RunStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface StageRun {
  stage: string;
  ordinal: number;
  status: RunStatus;
  error: Record<string, unknown> | null;
  metrics: Record<string, unknown> | null;
  /**
   * Provider-namespaced model this stage called (`anthropic/claude-sonnet-5`),
   * or null for the four stages that call no model. Recorded from the call, so
   * it is what actually ran rather than what the settings say now.
   */
  model: string | null;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
  /**
   * Decimal USD, serialised as a string so it survives the trip without
   * binary-float drift. **Null means unknown, not free** — the model is not in
   * the backend price table. Render it as "unknown"; a `?? 0` here would make
   * a newly-configured model look like a local one.
   */
  estimatedCostUsd: string | null;
  startedAt: string | null;
  finishedAt: string | null;
}

/**
 * A generation run. Newest first from the API.
 *
 * `queued` and `running` are the only states the console renders — they are
 * what stands between "Generate draft" and a row in the review queue, and
 * without them the reviewer stares at an unchanged queue for several minutes.
 */
export interface PipelineRun {
  id: string;
  topic: string;
  status: RunStatus;
  articleId: string | null;
  error: Record<string, unknown> | null;
  /**
   * Lifetime totals across every attempt, so a retried run reports what it
   * really spent. `stages` below is the latest attempt only, so these will
   * exceed the stage figures whenever `attempts > 1`.
   */
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
  /**
   * Decimal USD as a string, summed over the latest attempt's stages. Null if
   * any stage that consumed tokens ran on an unpriced model — all-or-nothing,
   * because a partial sum is a plausible number that is wrong by an order of
   * magnitude. Same rule as above: null is unknown, not zero.
   */
  estimatedCostUsd: string | null;
  /** Attempts started. >1 means a retryable failure requeued this run. */
  attempts: number;
  /** Set while a run is queued waiting out a retry backoff; null otherwise. */
  nextAttemptAt: string | null;
  /** Last sign of life while `running`. Minutes old means the worker died. */
  heartbeatAt: string | null;
  stages: StageRun[];
  createdAt: string;
  finishedAt: string | null;
}

export interface RunPage {
  items: PipelineRun[];
  nextCursor: string | null;
}

// --- reader accounts -------------------------------------------------------
//
// The public side's own auth surface. A reader is not a `Reviewer` with fewer
// permissions — it is a different table, a different token, and a type that
// carries no role at all, so no component can branch on one and be handed the
// other.

export interface Reader {
  id: string;
  email: string;
  displayName: string;
  /** Subjects lifted to the top of this reader's feed. Never a filter. */
  interests: Subject[];
  newsletter: boolean;
}

export interface Folder {
  id: string;
  name: string;
  /** Articles on this shelf. Server-counted, so the tab label cannot drift. */
  count: number;
}

export interface SavedPage {
  folders: Folder[];
  items: SavedCard[];
}

/**
 * `{ slug: folderId }` for everything the signed-in reader has saved.
 *
 * Fetched once alongside the feed rather than per tile, and deliberately not
 * folded into the feed response: the feed is public, identical for everyone
 * and cacheable, and a per-reader field in it would make it none of those.
 */
export type SavedIndex = Record<string, string>;

export type ContactKind = "fact_check" | "topic" | "other";

export interface ContactSubmission {
  kind: ContactKind;
  name?: string | null;
  email: string;
  link?: string | null;
  note: string;
}

/**
 * How a pipeline run's topic was chosen. Both are human decisions — this says
 * which surface produced the string, not whether anyone approved it.
 */
export type RunOrigin = "console" | "discovery";

export type DiscoveryCandidateStatus =
  | "proposed"
  | "promoted"
  | "dismissed"
  | "expired";

/**
 * A topic the fortnightly scan proposes, with the arithmetic behind it.
 *
 * Everything past `topic` exists to be shown. The ranking is built from
 * constants guessed before any real data existed, so a reviewer has to be able
 * to see *why* something is on the list — a bare score is either believed too
 * readily or ignored entirely.
 */
export interface DiscoveryCandidate {
  id: string;
  topic: string;
  substanceUi: string;
  substanceName: string;
  outcomeName: string | null;
  score: number;
  /** Distinct papers behind the substance's surge — what the lift measures. */
  paperCount: number;
  /**
   * Papers behind this specific angle. Equal to `paperCount` when the candidate
   * names no outcome. Both matter: the first is why the substance is on the
   * desk, the second is what this article would actually rest on.
   */
  anglePaperCount: number;
  /** Mean papers per window across the preceding windows. */
  baselineCount: number;
  /** `paperCount` against `baselineCount`, Laplace-smoothed. */
  lift: number;
  /** Study types among those papers, keyed by grade. */
  studyMix: Record<string, number>;
  topPmids: string[];
  status: DiscoveryCandidateStatus;
  pipelineRunId: string | null;
  scannedAt: string;
}

/** Where the console's manual scan is. `idle` is also the state after a restart. */
export type TrendScanStatus = "idle" | "running" | "succeeded" | "failed";

/**
 * The state of a scan the reviewer started, plus when one last succeeded.
 *
 * Not a `discovery_scans` row: that table is the window ledger and a restart
 * forgets that a press was in flight. `lastScanAt` is the one field that does
 * come from it, so the desk can still say when the cron slot last ran.
 */
export interface TrendScan {
  status: TrendScanStatus;
  startedAt: string | null;
  finishedAt: string | null;
  /** Zero records is a broken harvest; zero candidates is an ordinary week. */
  recordsSeen: number;
  observationsWritten: number;
  candidatesProposed: number;
  /** The scan's own remarks — a shallow baseline, a truncated seed. */
  notes: string[];
  /**
   * Trends found again and deliberately not re-proposed, each with why.
   *
   * Rendered rather than counted: "0 proposals" and "0 proposals, and here are
   * the three I am holding back" are different screens, and only the second is
   * trustworthy when it is empty.
   */
  suppressed: SuppressedTopic[];
  error: string | null;
  lastScanAt: string | null;
}

export interface SuppressedTopic {
  topic: string;
  /** Verbatim from the server: "promoted 2026-09-07", or a dismissal + cooloff. */
  reason: string;
}

// --- research agent -----------------------------------------------------------

export type ResearchRunMode = "weekly" | "manual";
export type ResearchRunStatus = "queued" | "running" | "completed" | "failed";
export type ResearchCandidateStatus =
  | "candidate"
  | "discarded"
  | "shortlisted"
  | "selected"
  | "promoted"
  | "dismissed";
export type EvidenceStatus = "none" | "limited" | "emerging" | "moderate" | "strong";
/** A trending topic's category is the article category it would be filed under. */
export type ResearchCategory = Subject;

/** Optional overrides; anything omitted uses the server's settings. */
export interface ResearchRunRequest {
  targetArticleCount?: number;
  categories?: ResearchCategory[];
  trendWindowDays?: number;
  geo?: string;
  language?: string;
  /** A subset of the sources the server enables; it cannot enable another. */
  enabledSources?: string[];
  maxRequestsPerSource?: number;
  /** Raise the bars for this run; the server's settings are the floor. */
  minTrendScore?: number;
  minDataCoverage?: number;
  /** Topics to look into on top of what the sweep finds (at most 10). */
  queries?: string[];
}

export type TrendStatus = "emerging" | "accelerating" | "steady" | "declining" | "preliminary";

/**
 * The stored scoring record, verbatim — so its keys are snake_case.
 * `unavailable` lists components with no data; they are left out, never zeroed.
 *
 * Runs since the trend search split (`version` "trend-v1") score momentum,
 * coverage, audience, freshness and editorial fit, with no evidence in the
 * score; older rows have no `version` and the evidence-weighted components.
 */
export interface ResearchScores {
  version?: string;
  components: Record<string, number>;
  unavailable: string[];
  weights_used: Record<string, number>;
  overall: number | null;
  /** 0-1: how much of the full score could be measured. */
  data_coverage?: number;
  confidence?: "high" | "medium" | "low" | null;
  /** Why there is no score, when there is none. */
  insufficient?: string | null;
}

/** A collected item shown as evidence that a topic is being discussed. */
export interface ResearchSupportingSource {
  source_name: string;
  source_type: "search" | "news" | "feed" | "web" | "video";
  title: string;
  url: string;
  publisher: string | null;
  published_at: string | null;
}

export interface ResearchEvidenceCounts {
  source: string;
  query: string;
  total: number;
  recent: number;
  reviews_or_meta: number;
  rcts: number;
}

/** Provider data as the run recorded it (snake_case, like `ResearchScores`). */
export interface ResearchSignals {
  subject: string;
  outcome: string;
  reader_question: string | null;
  trend: {
    source: string;
    growth_percent: number | null;
    rising_percent: number | null;
    is_breakout: boolean;
    related_queries: string[];
  } | null;
  web: {
    source: string;
    results_found: number;
    relevant_results: number;
    distinct_domains: number;
    authoritative_results: number;
    top_results: { title: string; url: string }[];
  } | null;
  news: {
    source: string;
    articles_found: number;
    recent: number;
    previous: number | null;
    top_articles: { title: string; url: string; source: string | null }[];
  } | null;
  science: {
    primary: ResearchEvidenceCounts | null;
    cross_check: ResearchEvidenceCounts | null;
    deep: {
      product: string;
      claims: string[];
      counts: ResearchEvidenceCounts;
      top_papers: { pmid: string | null; title: string; year: number | null; study_type: string }[];
    } | null;
    failures: string[];
  };
  /** What the trend score was computed from; absent on rows from before the split. */
  metrics?: {
    window_days: number;
    stories: number;
    independent_publishers: number;
    syndicated_copies: number;
    source_types: string[];
    coverage_measured: boolean;
    questions: number;
  } | null;
  trend_status?: TrendStatus | null;
  key_questions?: string[];
  supporting_sources?: ResearchSupportingSource[];
  in_evidence_pool?: boolean;
}

export interface ResearchCandidate {
  id: string;
  canonicalTopic: string;
  category: ResearchCategory | null;
  queries: string[];
  status: ResearchCandidateStatus;
  discardReason: string | null;
  evidenceStatus: EvidenceStatus | null;
  overall: number | null;
  rank: number | null;
  scores: ResearchScores;
  signals: ResearchSignals;
  pipelineRunId: string | null;
  decidedAt: string | null;
  dismissReason: string | null;
  /** The persistent topic this candidate observes, stable across runs. */
  topicRefId?: string | null;
}

export interface ResearchProviderStatus {
  /** `skipped`: not enabled for the run, or its budget or time ran out. */
  status: "ok" | "partial" | "failed" | "unavailable" | "skipped";
  detail: string | null;
  calls: number;
  failures: number;
  retries?: number;
  items?: number;
}

export interface ResearchRun {
  id: string;
  /** e.g. `research_2026_09_30_a84f` */
  label: string;
  mode: ResearchRunMode;
  status: ResearchRunStatus;
  /** The stage it is on, or the last one reached. */
  stage: string | null;
  params: ResearchRunRequest;
  providerStatus: Record<string, ResearchProviderStatus>;
  stageLog: { stage: string; duration_ms: number; metrics: Record<string, unknown> }[];
  notes: string[];
  error: { message: string } | null;
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  /** Filled on the single-run read, empty on the list. */
  candidates: ResearchCandidate[];
}
