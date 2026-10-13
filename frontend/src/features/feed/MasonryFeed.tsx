/**
 * The public masonry grid.
 *
 * Virtualised with `masonic`, which only renders tiles near the viewport. That
 * matters more here than in a typical grid: this feed is designed to grow
 * indefinitely and tiles are variable-height by design (see `tileRatio`), so an
 * unvirtualised list degrades on exactly the axis the product is meant to scale
 * on.
 *
 * The design comp lays the feed out with plain CSS `column-count`, which is the
 * simpler mechanism and produces the same picture — but CSS columns render every
 * tile in the document, so it is the one part of the comp not carried over
 * literally. The measurements are: ~190px columns, 14px gutter, which lands at
 * six columns on the 1240px measure and reflows down to two on a phone.
 *
 * Note for a future Next.js port (DESIGN.md §3.1): `masonic` measures DOM
 * nodes, so it is client-only. It would need `dynamic(..., { ssr: false })`
 * with a static grid as the server-rendered fallback — which is also the better
 * mobile layout, so the fallback is not wasted work.
 */

import { useCallback, useEffect, useRef } from "react";
import { Masonry } from "masonic";

import { ArticleCard } from "./ArticleCard";
import {
  CLEAR_FILTERS_ACTION,
  FEED_STATE,
  FEED_STATE_BODY,
  FEED_STATE_TITLE,
  LOADING_MORE,
  RETRY_BUTTON,
  SCROLL_SENTINEL,
  SKELETON_GRID,
  SKELETON_TILE,
} from "./styles";
import type { Feed } from "./useFeed";
import type { FeedCard } from "@/types/api";

interface Props extends Feed {
  /** True when the reader has narrowed the feed, so "empty" needs a way out. */
  filtered: boolean;
  onClearFilters: () => void;
  /** The current text search, for an empty state that can name it. */
  query: string;
}

export function MasonryFeed({
  items,
  status,
  error,
  hasNextPage,
  isFetchingNextPage,
  fetchNextPage,
  refetch,
  filtered,
  onClearFilters,
  query,
  matchedWithin,
}: Props) {
  const sentinel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const node = sentinel.current;
    if (!node || !hasNextPage) return;

    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting && !isFetchingNextPage) fetchNextPage();
      },
      // Start loading before the sentinel is visible, so the next page is
      // usually in place by the time the reader reaches the bottom.
      { rootMargin: "600px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage]);

  const renderTile = useCallback(
    ({ data: card }: { data: FeedCard }) => <ArticleCard card={card} />,
    [],
  );

  if (status === "pending") return <FeedSkeleton />;

  if (status === "error") {
    return (
      <ErrorState
        message={error instanceof Error ? error.message : "Could not load the feed"}
        onRetry={refetch}
      />
    );
  }

  if (items.length === 0) {
    return (
      <EmptyState
        filtered={filtered}
        query={query}
        matchedWithin={matchedWithin}
        onClearFilters={onClearFilters}
      />
    );
  }

  return (
    <>
      <Masonry
        items={items}
        columnWidth={190}
        columnGutter={14}
        overscanBy={2}
        // A list, not masonic's default "grid": a grid role promises rows,
        // cells and arrow-key navigation, and masonic renders gridcells with
        // no rows — invalid ARIA (axe: aria-required-children/-parent), read by
        // screen readers as a broken table. A feed of tiles is a list.
        role="list"
        // Keyed by slug so masonic reuses cells across page appends rather
        // than remounting the whole grid on every fetch.
        itemKey={(card: FeedCard) => card.slug}
        render={renderTile}
      />
      <div ref={sentinel} aria-hidden className={SCROLL_SENTINEL} />
      {isFetchingNextPage ? <p className={LOADING_MORE}>Loading more…</p> : null}
    </>
  );
}

/**
 * Skeleton tiles rather than a spinner: a spinner collapses the layout, so the
 * page jumps when content lands. Varied heights match the real masonry shape.
 */
function FeedSkeleton() {
  const heights = [230, 190, 260, 200, 240, 180, 250, 210, 230, 190, 260, 200];
  return (
    <div className={SKELETON_GRID} aria-busy="true" aria-label="Loading articles">
      {heights.map((height, index) => (
        <div key={index} style={{ height }} className={SKELETON_TILE} />
      ))}
    </div>
  );
}

/**
 * Nothing to show, and the three reasons are not interchangeable.
 *
 * A text search matched nothing *in what has loaded* — which is a smaller claim
 * than "nothing exists", and saying the larger one would be a lie about the
 * archive, because search runs client-side (see `useFeed`). A filter matched
 * nothing published. Or the feed itself is empty, which on a human-reviewed
 * publication is a normal early state rather than a failure.
 */
function EmptyState({
  filtered,
  query,
  matchedWithin,
  onClearFilters,
}: {
  filtered: boolean;
  query: string;
  matchedWithin: number;
  onClearFilters: () => void;
}) {
  const searching = Boolean(query);

  return (
    <div className={FEED_STATE}>
      <p className={FEED_STATE_TITLE}>
        {searching
          ? `Nothing matching “${query}” yet.`
          : filtered
            ? "Nothing published under that yet."
            : "Nothing published yet."}
      </p>
      <p className={FEED_STATE_BODY}>
        {searching
          ? `Searched the ${matchedWithin} ${
              matchedWithin === 1 ? "article" : "articles"
            } loaded so far. Scroll the full feed to widen the search, or clear it and browse by category.`
          : "Every article here is read and approved by a person before it goes live, so the shelf fills slowly on purpose."}
      </p>
      {filtered ? (
        <button onClick={onClearFilters} className={CLEAR_FILTERS_ACTION}>
          Clear and show everything
        </button>
      ) : null}
    </div>
  );
}

function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className={FEED_STATE}>
      <p className={FEED_STATE_TITLE}>{message}</p>
      <button onClick={onRetry} className={RETRY_BUTTON}>
        Try again
      </button>
    </div>
  );
}
