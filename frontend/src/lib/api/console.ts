/**
 * Editorial console data access. Every call requires a bearer token.
 */

import { apiFetch, qs, setAuthToken } from "./client";
import type {
  ArticleDetail,
  ArticleStatus,
  CardPreview,
  CollectionKind,
  CollectionRun,
  Product,
  RelatedItem,
  SocialListeningResult,
  GeneratedImagery,
  IllustrationFrame,
  DiscoveryCandidate,
  Subject,
  MediaUpload,
  QueueItem,
  ResearchRun,
  ResearchRunRequest,
  Reviewer,
  RunPage,
  TipTapDoc,
  TrendScan,
  Verdict,
} from "@/types/api";

export const consoleKeys = {
  queue: (status: ArticleStatus) => ["console", "queue", status] as const,
  /** Prefix of every tab's queue - invalidate this when a run lands a draft. */
  queues: ["console", "queue"] as const,
  article: (id: string) => ["console", "article", id] as const,
  /** The derived feed tile. Never reused across an edit - see `fetchCardPreview`. */
  card: (id: string) => ["console", "card", id] as const,
  runs: ["console", "runs"] as const,
  me: ["console", "me"] as const,
  /** Live trend proposals. Invalidated alongside `runs` when one is promoted. */
  candidates: ["console", "candidates"] as const,
  /** The manual scan's state. Polled only while one is running. */
  trendScan: ["console", "trend-scan"] as const,
  /** Research runs, newest first; and one run with its candidates. */
  researchRuns: ["console", "research-runs"] as const,
  researchRun: (id: string) => ["console", "research-run", id] as const,
  /** Collection runs (social listening, product discovery), newest first. */
  collectionRuns: ["console", "collection-runs"] as const,
  socialResult: ["console", "social-result"] as const,
  products: ["console", "products"] as const,
  related: (kind: string, id: string) => ["console", "related", kind, id] as const,
};

/**
 * Exchange credentials for a bearer token and load the reviewer.
 *
 * The token response keeps OAuth 2's snake_case field names - that shape is a
 * standard, not our house style, so the server does not camelCase it.
 */
export async function login(
  email: string,
  password: string,
): Promise<{ token: string; reviewer: Reviewer }> {
  const response = await apiFetch<{ access_token: string }>("/console/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
  setAuthToken(response.access_token);
  const reviewer = await fetchMe();
  return { token: response.access_token, reviewer };
}

/**
 * Change the signed-in reviewer's password. Every other session is signed out,
 * so the answer is a fresh token for this one.
 */
export async function changePassword(
  currentPassword: string,
  newPassword: string,
): Promise<string> {
  const response = await apiFetch<{ access_token: string }>("/console/auth/password", {
    method: "POST",
    body: JSON.stringify({ currentPassword, newPassword }),
  });
  return response.access_token;
}

/** Mail a reset link. Answers the same whether or not the address is a reviewer's. */
export async function forgotReviewerPassword(email: string): Promise<void> {
  await apiFetch("/console/auth/password/forgot", {
    method: "POST",
    body: JSON.stringify({ email }),
  });
}

/** Set a new password from the emailed link. 400 when the link is stale or used. */
export async function resetReviewerPassword(token: string, password: string): Promise<void> {
  return apiFetch<void>("/console/auth/password/reset", {
    method: "POST",
    body: JSON.stringify({ token, password }),
  });
}

/** Re-validates the stored token as a side effect of loading the reviewer. */
export async function fetchMe(): Promise<Reviewer> {
  return apiFetch<Reviewer>("/console/auth/me");
}

/**
 * The review queue.
 *
 * `status` is a parameter because the console also shows a validation_failed
 * tab. Those drafts can never be approved, but they are how a prompt bug
 * becomes visible - hiding them makes the grounding check look like silence.
 *
 *
 */
export async function fetchQueue(params: {
  status?: ArticleStatus;
  cursor?: string;
  limit?: number;
}): Promise<{ items: QueueItem[]; nextCursor: string | null }> {
  return apiFetch(`/console/articles${qs(params)}`);
}

/** Draft + sources + validation report, in one request. */
export async function fetchArticleDetail(id: string): Promise<ArticleDetail> {
  return apiFetch<ArticleDetail>(`/console/articles/${id}`);
}

/**
 * Autosave into edited_content. Debounced ~800ms by the caller.
 *
 * Only ever sends edited content. original_content is not part of this
 * payload and is rejected server-side by a DB trigger if anything tries.
 *
 * STUB.
 */
export async function saveContent(
  id: string,
  content: TipTapDoc,
  base: TipTapDoc | null,
): Promise<void> {
  // `base` is what this editor last loaded or saved; the server answers 412 when
  // another reviewer has saved over it since.
  return apiFetch<void>(`/console/articles/${id}/content`, {
    method: "PATCH",
    body: JSON.stringify({ content, base }),
  });
}

/** Autosave a correction to a published article. Readers do not see it yet. */
export async function saveCorrection(
  id: string,
  content: TipTapDoc,
  base: TipTapDoc | null,
): Promise<void> {
  return apiFetch<void>(`/console/articles/${id}/correction`, {
    method: "PATCH",
    body: JSON.stringify({ content, base }),
  });
}

/** Put the typed correction live. The note is shown to readers with the date. */
export async function publishCorrection(id: string, note: string): Promise<void> {
  return apiFetch<void>(`/console/articles/${id}/correction`, {
    method: "POST",
    body: JSON.stringify({ note }),
  });
}

/** Take a published article off the public site. Terminal; the reason is kept. */
export async function withdrawArticle(id: string, reason: string): Promise<void> {
  return apiFetch<void>(`/console/articles/${id}/withdraw`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

/**
 * Fix a draft's headline, summary or verdict. Omitted fields stay as they are.
 * 409 when the verdict would rise past the ceiling the cited evidence sets.
 */
export async function setMeta(
  id: string,
  meta: { headline?: string; summary?: string; verdict?: Verdict },
): Promise<void> {
  return apiFetch<void>(`/console/articles/${id}/meta`, {
    method: "PATCH",
    body: JSON.stringify(meta),
  });
}

/**
 * Classify what kind of thing the article assesses.
 *
 * `null` clears it, and that is a real answer rather than a missing one - an
 * unclassified article renders in ink, which is the design's resting state.
 *
 * Separate from the content autosave, and allowed after publication, because
 * this drives a colour and a browse listing rather than a word the reader was
 * shown: getting it wrong should be correctable without touching the article.
 */
export async function setSubject(id: string, subject: Subject | null): Promise<void> {
  return apiFetch<void>(`/console/articles/${id}/subject`, {
    method: "PATCH",
    body: JSON.stringify({ subject }),
  });
}

/**
 * Publish. The only path to `published` in the system.
 *
 * Can fail with 409 when editing has orphaned a citation - the error detail
 * names the specific handles, and the UI must show them rather than a generic
 * failure.
 *
 * STUB.
 */
export async function approveArticle(id: string): Promise<void> {
  return apiFetch<void>(`/console/articles/${id}/approve`, { method: "POST" });
}

/** Reject with a required reason. Terminal. STUB. */
export async function rejectArticle(id: string, reason: string): Promise<void> {
  return apiFetch<void>(`/console/articles/${id}/reject`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

/**
 * Store an image the reviewer picked on their own machine.
 *
 * Returns the path to put in the document's image node, and that is the only
 * `src` the server will accept back - images are uploaded, never linked.
 *
 * Not scoped to an article: the store is content-addressed, so the path is the
 * identity and an article id here would be an ownership claim nothing could
 * keep true. 413 over the size ceiling, 415 when the bytes are not a PNG,
 * JPEG, GIF or WebP - checked from the file's own contents, not its name.
 *
 * STUB.
 */
export async function uploadMedia(file: File): Promise<MediaUpload> {
  const form = new FormData();
  form.append("file", file);
  return apiFetch<MediaUpload>("/console/media", { method: "POST", body: form });
}

/**
 * The feed tile this draft would publish as.
 *
 * Server-derived by the same `derive_card()` the publish path runs, so the
 * preview cannot drift from what lands on the feed. That is the whole reason
 * this is a request rather than a function in this file: a TypeScript port of
 * the derivation rules would be a second implementation, free to disagree with
 * the first - and the preview would then be reassuring rather than useful.
 *
 * Fetched on demand rather than folded into `fetchArticleDetail`, because the
 * two have opposite caching lives: a draft is loaded once per review session,
 * while a tile has to be right at the moment somebody looks at it. Flush
 * autosave before calling, the way approving does.
 */
export async function fetchCardPreview(id: string): Promise<CardPreview> {
  return apiFetch<CardPreview>(`/console/articles/${id}/card`);
}

/**
 * Draw this article's pictures again, with a new seed.
 *
 * `frames` picks which - both by default, or just the article's own `lead` or
 * just the feed tile's `cover`. They are seen in different places and judged
 * separately, so a reviewer who likes one and not the other should not have to
 * replace both to fix one.
 *
 * The one console action that bills an external provider per press, so the
 * server keeps a spend budget in front of it and answers 429 with a
 * `Retry-After` when it is spent. The budget counts renders, so asking for one
 * frame costs half of asking for two.
 *
 * Returns both frames and writes neither into the document: the caller swaps
 * the editor's image node to `lead.src` and lets autosave persist it, so the
 * new path goes through the server's media check like any other edit. A
 * one-frame redraw on an article with no imagery at all answers 409 rather
 * than quietly drawing both - the reviewer asked for one render and would
 * otherwise be billed for two.
 */
export async function regenerateIllustration(
  id: string,
  frames: IllustrationFrame[] = ["lead", "cover"],
): Promise<GeneratedImagery> {
  return apiFetch<GeneratedImagery>(`/console/articles/${id}/illustration`, {
    method: "POST",
    body: JSON.stringify({ frames }),
  });
}

/** Enqueue a topic. Returns 202 - generation takes minutes. STUB. */
export async function createRun(topic: string, blurb?: string): Promise<{ id: string }> {
  return apiFetch(`/console/pipeline/runs`, {
    method: "POST",
    body: JSON.stringify({ topic, blurb }),
  });
}

/**
 * Run history, newest first.
 *
 * This is the other half of `createRun` returning 202: the queue is polled for
 * the runs still in flight, so a reviewer who just submitted a topic sees the
 * draft being made rather than an unchanged queue. STUB.
 */
export async function fetchRuns(
  params: { cursor?: string; limit?: number } = {},
): Promise<RunPage> {
  return apiFetch(`/console/pipeline/runs${qs(params)}`);
}

/**
 * Live trend proposals, best first.
 *
 * Unpaginated: the scan writes at most `DISCOVERY_MAX_CANDIDATES` per run and
 * expires what it no longer ranks, so this list is bounded by the scan rather
 * than by the request.
 */
export async function fetchCandidates(limit = 20): Promise<DiscoveryCandidate[]> {
  return apiFetch(`/console/discovery/candidates${qs({ limit })}`);
}

/**
 * Turn a proposal into a queued generation run.
 *
 * Returns the run, so the caller invalidates `consoleKeys.runs` and the
 * reviewer immediately sees the in-flight row they already recognise. This is
 * the one action here that spends money.
 */
export async function promoteCandidate(id: string): Promise<{ id: string }> {
  return apiFetch(`/console/discovery/candidates/${id}/promote`, { method: "POST" });
}

/**
 * Say no, with a reason.
 *
 * The reason is stored and shown in the next scan's output; the substance stays
 * suppressed until both the cooloff elapses and its literature has doubled.
 */
export async function dismissCandidate(id: string, reason: string): Promise<void> {
  return apiFetch(`/console/discovery/candidates/${id}/dismiss`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

/**
 * The manual scan's state - this process's last press, plus the ledger's last
 * success. Cheap enough to poll while one is running.
 */
export async function fetchTrendScan(): Promise<TrendScan> {
  return apiFetch("/console/discovery/scan");
}

/**
 * Run the trend scan now, rather than waiting for the cron slot.
 *
 * 202: the scan is minutes of PubMed requests and the server does not hold the
 * connection open for it, so the caller polls `fetchTrendScan`. Costs nothing
 * generative - a scan proposes, and promoting what it finds is still a separate
 * human decision. A 409 means one is already running.
 */
export async function startTrendScan(): Promise<TrendScan> {
  return apiFetch("/console/discovery/scan", { method: "POST" });
}

// --- research agent ----------------------------------------------------------

/**
 * Find trending topics now. The same call n8n's weekly schedule makes.
 *
 * 202: a run is minutes of Google Trends, search, news and PubMed requests, so
 * the caller polls `fetchResearchRun`. It proposes topics and generates no
 * drafts. A 409 means a research run is already queued or running.
 */
export async function startResearchRun(request: ResearchRunRequest = {}): Promise<ResearchRun> {
  return apiFetch("/console/research/runs", {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export async function fetchResearchRuns(limit = 5): Promise<ResearchRun[]> {
  return apiFetch(`/console/research/runs${qs({ limit })}`);
}

export async function fetchResearchRun(id: string): Promise<ResearchRun> {
  return apiFetch(`/console/research/runs/${id}`);
}

/** Turn a proposed topic into a queued generation run - the step that spends. */
export async function promoteResearchCandidate(id: string): Promise<{ id: string }> {
  return apiFetch(`/console/research/candidates/${id}/promote`, { method: "POST" });
}

export async function dismissResearchCandidate(id: string, reason: string): Promise<void> {
  return apiFetch(`/console/research/candidates/${id}/dismiss`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

// --- social listening and product discovery -----------------------------------

/**
 * Queue a collection run now (the scheduler also queues them when due). It
 * spends YouTube quota and local model time, never a draft. 409 while one of
 * the same kind is queued or running.
 */
export async function startCollectionRun(kind: CollectionKind): Promise<CollectionRun> {
  return apiFetch("/console/collection/runs", {
    method: "POST",
    body: JSON.stringify({ kind }),
  });
}

export async function fetchCollectionRuns(limit = 10): Promise<CollectionRun[]> {
  return apiFetch(`/console/collection/runs${qs({ limit })}`);
}

/** The latest weekly social run's proposals; a 404 means there is none yet. */
export async function fetchSocialResult(): Promise<SocialListeningResult> {
  return apiFetch("/console/social/result");
}

export async function promoteSocialCandidate(id: string): Promise<{ id: string }> {
  return apiFetch(`/console/social/candidates/${id}/promote`, { method: "POST" });
}

export async function dismissSocialCandidate(id: string, reason: string): Promise<void> {
  return apiFetch(`/console/social/candidates/${id}/dismiss`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

export async function fetchProducts(limit = 30): Promise<Product[]> {
  return apiFetch(`/console/products${qs({ limit })}`);
}

/** Items from the other discovery systems, each with its own system's numbers. */
export async function fetchRelated(
  kind: "social_topic" | "product",
  id: string,
): Promise<RelatedItem[]> {
  return apiFetch(`/console/intel/related${qs({ kind, id })}`);
}

export interface WorkerStatus {
  queuedArticles: number;
  queuedResearch: number;
  queuedCollection: number;
  running: { kind: string; label: string; heartbeatAgeSeconds: number | null }[];
  oldestQueuedSeconds: number | null;
  /** Work is waiting, nothing is running, and it has waited too long. */
  stalled: boolean;
  lastRetractionSweepAt: string | null;
}

export async function fetchWorkerStatus(): Promise<WorkerStatus> {
  return apiFetch<WorkerStatus>("/console/status");
}

export interface ContactMessage {
  id: string;
  kind: "topic" | "other" | string;
  name: string | null;
  email: string;
  link: string | null;
  note: string;
  createdAt: string;
}

/** What readers sent through the contact form and "Suggest a topic", newest first. */
export async function fetchContactInbox(): Promise<ContactMessage[]> {
  return apiFetch<ContactMessage[]>("/console/contact");
}
