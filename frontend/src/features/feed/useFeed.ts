/**
 * The feed query, split out from the grid that draws it.
 *
 * The feed heading needs to know how many articles are showing; the grid needs
 * the same items to lay out. Rather than fetch twice or thread a callback back
 * up out of the grid, the route owns the query through this hook and hands the
 * result to both.
 *
 * This is the React binding only — the fetch itself stays in `lib/api/feed.ts`,
 * framework-agnostic, so the Next.js port can call it from a server component
 * without dragging react-query along (DESIGN.md §3.1).
 *
 * **Every narrowing happens on the server**, free text included. The feed is
 * paginated, so narrowing client-side would only narrow the pages already
 * loaded — a category with nothing on page one would look empty, and a search
 * would miss everything not yet scrolled to. Free text used to be the
 * exception, matched against loaded cards only, because the alternative on
 * offer was a `LIKE` over card columns that would quietly miss every
 * article's body. `GET /feed?q=` now searches the whole approved text of every
 * published article (migration 0006), so "nothing matched" is a claim about
 * the archive and the empty state can make it.
 */

import { useMemo } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";

import { feedKeys, fetchFeed, type FeedFilter } from "@/lib/api/feed";
import type { FeedCard, FeedPage } from "@/types/api";

export interface Feed {
  items: FeedCard[];
  status: "pending" | "error" | "success";
  error: unknown;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  fetchNextPage: () => void;
  refetch: () => void;
  /**
   * How a search was answered; null for the feed. `similar` means nothing
   * matched the words and these are close spellings, which the page says.
   * Read from the first page: the server decides the mode there and keeps it.
   */
  match: FeedPage["match"];
}

export function useFeed(filter: FeedFilter): Feed {
  const serverFilter: FeedFilter = {
    verdict: filter.verdict ?? null,
    subject: filter.subject ?? null,
    personalise: filter.personalise,
    query: filter.query?.trim() || null,
  };

  const query = useInfiniteQuery({
    queryKey: feedKeys.list(serverFilter),
    queryFn: ({ pageParam }) => fetchFeed({ cursor: pageParam, ...serverFilter }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.nextCursor ?? undefined,
  });

  const items = useMemo(
    () => query.data?.pages.flatMap((page) => page.items) ?? [],
    [query.data],
  );

  return {
    items,
    status: query.status,
    error: query.error,
    hasNextPage: query.hasNextPage,
    isFetchingNextPage: query.isFetchingNextPage,
    fetchNextPage: () => void query.fetchNextPage(),
    refetch: () => void query.refetch(),
    match: query.data?.pages[0]?.match ?? null,
  };
}
