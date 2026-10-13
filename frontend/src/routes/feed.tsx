/**
 * The public feed.
 *
 * The masthead the previous design opened with — a display-size statement of
 * what the product is — is gone, replaced by the comp's hero: the footage full
 * bleed with one serif line over it. The claim is the same and the register is
 * not, which is the point of the redesign. The promise it used to make in a
 * standfirst is now the line under the grid, where a reader arrives at it
 * having already seen what it is about.
 *
 * Narrowing lives in the URL and is read through `useBrowseState`, so the
 * header's search box, the drawer's categories and this page are three views of
 * one piece of state rather than three copies of it.
 */

import { useEffect, useRef, useState } from "react";

import { MasonryFeed } from "@/features/feed/MasonryFeed";
import {
  FEED_ACTIONS,
  FEED_BODY,
  FEED_COUNT,
  FEED_FOOTER,
  FEED_HEAD,
  FEED_PAGE,
  FEED_TITLE,
  HERO,
  HERO_STATEMENT,
  HERO_TOGGLE,
  HERO_VIDEO,
  PILL_BUTTON,
  PILL_BUTTON_ON,
} from "@/features/feed/styles";
import { useFeed } from "@/features/feed/useFeed";
import { VERDICT_LABELS } from "@/features/evidence/labels";
import { SUBJECT_LABELS } from "@/features/evidence/subject";
import { useReader } from "@/features/reader/auth";
import { useBrowseState } from "@/features/shell/useBrowseState";

export function FeedRoute() {
  const { query, verdict, subject, plain, filtered, setPlain, clear } =
    useBrowseState();
  const { reader, signedIn } = useReader();

  const personalised =
    signedIn && !plain && (reader?.interests.length ?? 0) > 0 && subject === null;

  const feed = useFeed({
    query,
    verdict,
    subject,
    // Turned off while browsing one subject: a second ordering rule layered on
    // top of an explicit choice just makes the choice look ignored.
    personalise: personalised,
  });

  const count = feed.items.length;

  return (
    <main className={FEED_PAGE}>
      <Hero />

      <div className={FEED_BODY}>
        <div className={FEED_HEAD}>
          <h1 className={FEED_TITLE}>{heading({ query, verdict, subject, personalised })}</h1>

          <div className={FEED_ACTIONS}>
            {feed.status === "success" ? (
              <span className={FEED_COUNT}>
                {count} {count === 1 ? "article" : "articles"} · every one
                editor-approved
              </span>
            ) : null}

            {/*
              Only offered to a reader whose feed is actually being reordered.
              A toggle that does nothing is worse than no toggle: it teaches
              that the control is decorative.
            */}
            {signedIn && (reader?.interests.length ?? 0) > 0 && subject === null ? (
              <button
                onClick={() => setPlain(!plain)}
                aria-pressed={personalised}
                className={personalised ? PILL_BUTTON_ON : PILL_BUTTON}
              >
                {personalised ? "For you" : "Newest first"}
              </button>
            ) : null}

            {filtered ? (
              <button onClick={clear} className={PILL_BUTTON}>
                Clear
              </button>
            ) : null}
          </div>
        </div>

        <MasonryFeed
          {...feed}
          filtered={filtered}
          onClearFilters={clear}
          query={query}
        />

        <p className={FEED_FOOTER}>
          Every claim on this page was read against the studies behind it. No
          influencers, no affiliate links, no supplements we sell.
        </p>
      </div>
    </main>
  );
}

/**
 * What the grid below is currently showing.
 *
 * Named rather than always "Latest research", because the drawer and the search
 * box can both narrow the feed from somewhere the reader cannot see — a grid
 * that silently holds one category is how a reader concludes the site is empty.
 */
function heading({
  query,
  verdict,
  subject,
  personalised,
}: {
  query: string;
  verdict: string | null;
  subject: keyof typeof SUBJECT_LABELS | null;
  personalised: boolean;
}): string {
  if (query) return `“${query}”`;
  if (subject) return SUBJECT_LABELS[subject];
  if (verdict) return VERDICT_LABELS[verdict as keyof typeof VERDICT_LABELS];
  return personalised ? "For you" : "Latest research";
}

/**
 * The hero.
 *
 * `autoPlay muted playsInline loop` is the only combination browsers will start
 * without a gesture, and `muted` has to be on the element itself rather than set
 * later — Safari decides at parse time, so a `ref` that mutes after mount is
 * already too late and the video simply never plays.
 *
 * No audio track in use: this is wallpaper with a sentence over it, not
 * something to watch. `aria-hidden` keeps the video itself out of the
 * accessibility tree, and the statement over it is real text that is read.
 *
 * **But it has a pause button, and does not start for a reader who asked for
 * less motion.** Footage that moves for more than five seconds alongside other
 * content needs a way to stop it (WCAG 2.2.2, Level A — part of IS 5568): for
 * someone with a vestibular or attention disorder, a looping beach is not
 * wallpaper. `autoPlay` stays on the element for the Safari reason above; the
 * effect pauses it at once under `prefers-reduced-motion`.
 */
function Hero() {
  const video = useRef<HTMLVideoElement>(null);
  const [playing, setPlaying] = useState(true);

  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      video.current?.pause();
      setPlaying(false);
    }
  }, []);

  function toggle() {
    const element = video.current;
    if (!element) return;
    if (element.paused) {
      void element.play();
      setPlaying(true);
    } else {
      element.pause();
      setPlaying(false);
    }
  }

  return (
    <div className={HERO}>
      <video
        ref={video}
        src="/media/hero.mp4"
        autoPlay
        loop
        muted
        playsInline
        aria-hidden
        // A poster would be a second asset to keep in step with the video for
        // the few hundred milliseconds before the first frame decodes; the tile
        // ground behind it covers that, in the right colour, for free.
        className={HERO_VIDEO}
      />
      <p className={HERO_STATEMENT}>Simplifying your way to stay young</p>
      <button
        type="button"
        onClick={toggle}
        // The glyph is decoration; the name is what a screen reader says.
        aria-label={playing ? "Pause video" : "Play video"}
        className={HERO_TOGGLE}
      >
        {/* U+FE0E asks for the text glyph, not the colour emoji iOS and
            Windows otherwise substitute for ⏸ and ▶. */}
        <span aria-hidden>{playing ? "\u23F8\uFE0E" : "\u25B6\uFE0E"}</span>
      </button>
    </div>
  );
}
