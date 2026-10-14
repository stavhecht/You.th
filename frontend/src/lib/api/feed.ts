/**
 * Public feed data access. No auth, no mutations.
 */

import { apiFetch, qs } from "./client";
import type { Article, FeedFacets, FeedPage, Subject, Verdict } from "@/types/api";

/** What the feed is currently narrowed to. Mirrors the URL's search params. */
export interface FeedFilter {
  verdict?: Verdict | null;
  subject?: Subject | null;
  /**
   * Whether a signed-in reader's interests reorder the page.
   *
   * Ordering, never filtering — the personalised feed holds the same articles
   * as the anonymous one, in a different order. Turned off while the reader is
   * browsing a single subject, where a second ordering rule on top of an
   * explicit choice is just confusing.
   */
  personalise?: boolean;
  /**
   * Free-text search, answered by the server over every published article —
   * body included — and ranked by relevance. While it is set the server
   * ignores `personalise`: a search promises best match first.
   */
  query?: string | null;
}

export const feedKeys = {
  all: ["feed"] as const,
  /**
   * The key has to name every server-side narrowing, or two different feeds
   * share a cache entry and the second one renders the first one's articles.
   * `personalise` is in here for the same reason: signing in changes the
   * response for identical filters.
   */
  list: (filter: FeedFilter = {}) =>
    [
      "feed",
      "list",
      filter.verdict ?? "all",
      filter.subject ?? "all",
      filter.personalise === false ? "plain" : "personalised",
      filter.query ?? "",
    ] as const,
  facets: ["feed", "facets"] as const,
  article: (slug: string) => ["feed", "article", slug] as const,
};

/**
 * One page of published cards.
 *
 * Cursor-paginated rather than offset-paginated: the feed grows at the head,
 * and offsets skip or duplicate items when something publishes mid-scroll.
 * Pairs with useInfiniteQuery. With `query` set it is a search, and the
 * cursor it returns is a search cursor — opaque either way.
 */
export async function fetchFeed(
  params: { cursor?: string; limit?: number } & FeedFilter,
): Promise<FeedPage> {
  // Spelled out rather than spread, so a field added to `FeedFilter` for
  // client-side use cannot silently start being sent to the server.
  return apiFetch<FeedPage>(
    `/feed${qs({
      cursor: params.cursor,
      limit: params.limit,
      verdict: params.verdict,
      subject: params.subject,
      personalise: params.personalise,
      q: params.query || undefined,
    })}`,
  );
}

/** Counts for the browse drawer. Cheap, and cached for the session. */
export async function fetchFacets(): Promise<FeedFacets> {
  return apiFetch<FeedFacets>("/feed/facets");
}

/** Full article with citations and sources. */
export async function fetchArticle(slug: string): Promise<Article> {
  return apiFetch<Article>(`/articles/${encodeURIComponent(slug)}`);
}
