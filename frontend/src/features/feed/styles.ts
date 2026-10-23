/**
 * Names for the public feed's styling — hero, tiles, the article page and its
 * source list.
 *
 * Grouped by surface, in reading order, so this file can be scanned the way the
 * product is read rather than the way the components happen to be split.
 *
 * Three things worth knowing before editing:
 *
 * - **The measures are arguments.** `max-w-article` (800px) is the article
 *   page, and the prose now fills it rather than sitting in a narrower column
 *   inside it — one measure, not two. That is a voice decision, not a layout
 *   one: the picture and the body run the full width of the page the headline
 *   sits on.
 *
 *   They were 760px and `max-w-[64ch]`, then 800px around a 600px column.
 *   `max-w-prose` still exists at 760px and still frames the short public
 *   pages — see `tailwind.config.ts`. What has to be watched when this moves
 *   is the character count, not the pixel: see PROSE_MEASURE.
 * - **`shadow-panel` and `shadow-pop` are the only shadows in the system.** Both
 *   belong to things that float over the page. Nothing resting on the page gets
 *   one; the tiles use radius and a scrim instead.
 * - **Tile text is white, not ink.** It sits over a photograph nobody controls,
 *   so it is the one place in the product where a fixed colour beats a token —
 *   see `.ew-tile-scrim` in `styles/youth.css`.
 */

import { mediaWrapClass, type MediaAlign } from "@/lib/media";

/* ── the feed page ──────────────────────────────────────────────────────── */

export const FEED_PAGE = "pb-16";

/**
 * The hero, full-bleed.
 *
 * `w-screen` with a negative margin rather than a wrapper without the page
 * measure: the video runs edge to edge while everything under it stays on the
 * 1240px grid, and `calc(50% - 50vw)` is what pulls it out of the centred
 * column without a layout wrapper of its own.
 */
export const HERO =
  "relative ml-[calc(50%-50vw)] aspect-[16/9] max-h-[68vh] w-screen overflow-hidden bg-tile";
export const HERO_VIDEO = "h-full w-full object-cover saturate-[0.72]";
/**
 * Pause/play for the hero footage: a bare ▶ / ⏸ glyph in the statement's own
 * colour and 70% opacity, so it reads as part of the same quiet layer rather
 * than as a control competing with it. Full opacity on hover and focus, so it
 * is findable when someone is actually reaching for it. 32px square keeps a
 * comfortable hit area around a small glyph.
 */
export const HERO_TOGGLE =
  "absolute bottom-3 right-3 flex h-8 w-8 items-center justify-center border-0 bg-transparent p-0 font-body text-[15px] leading-none text-[#f4f0ed] opacity-70 transition-opacity hover:opacity-100 focus-visible:opacity-100";

/**
 * The statement over the hero video.
 *
 * This was the product's one Playfair element, and it no longer needs a font of
 * its own: Instrument Serif is now the interface face, and a display serif at
 * 60px is what it is drawn for. So the flourish stays and the second file does
 * not. It is set at 400 because that is the only weight the family ships —
 * `font-bold` here would ask the browser to smear the outline.
 *
 * Sits at 70% opacity over the footage rather than at full white: the statement
 * is a mood, and type at full strength over moving video reads as a caption
 * demanding to be finished before the picture is looked at.
 */
export const HERO_STATEMENT =
  "pointer-events-none absolute left-1/2 top-1/2 w-[min(90%,16ch)] -translate-x-1/2 -translate-y-1/2 text-balance text-center font-heading text-[clamp(28px,3.6vw,60px)] uppercase leading-[1.1] text-[#f4f0ed] opacity-70";

export const FEED_BODY = "mx-auto max-w-page px-gutter";

export const FEED_HEAD =
  "flex flex-wrap items-baseline justify-between gap-3 pb-[18px] pt-7";
export const FEED_TITLE =
  "font-heading text-[22px] font-bold tracking-[-0.02em] text-ink";
export const FEED_COUNT = "font-body text-[12.5px] text-ink-3";

export const FEED_ACTIONS = "flex flex-wrap items-center gap-2";

/** A pill that clears or toggles something. Outlined — never the accent fill. */
export const PILL_BUTTON =
  "inline-flex items-center gap-2 rounded-full border border-rule bg-transparent px-3.5 py-[7px] font-body text-[10.5px] font-semibold uppercase leading-none tracking-[0.12em] text-ink transition-colors hover:border-ink";

/** The same pill, filled, for a narrowing that is currently on. */
export const PILL_BUTTON_ON =
  "inline-flex items-center gap-2 rounded-full border border-invert bg-invert px-3.5 py-[7px] font-body text-[10.5px] font-semibold uppercase leading-none tracking-[0.12em] text-invert-fg transition-opacity hover:opacity-90";

/**
 * The standing promise under the feed.
 *
 * Centred and on a narrow measure, which is the one place the product centres
 * body copy: it is a statement about the whole page rather than part of the
 * reading flow.
 */
export const FEED_FOOTER =
  "mx-auto mt-16 max-w-[60ch] border-t border-rule-soft pt-[22px] text-center font-body text-[15px] leading-normal text-ink-2";

/* ── shared ─────────────────────────────────────────────────────────────── */

/** A small-caps label over a section. */
export const SECTION_LABEL =
  "font-body text-label-sm font-semibold uppercase text-ink-3";

/**
 * A text action drawn as an accent underline rather than a button. Used where
 * the action is an escape hatch — clear a filter, go back — and a filled button
 * would compete with the content it sits beside.
 */
export const ACCENT_TEXT_ACTION =
  "border-b border-accent pb-px font-body text-micro font-semibold leading-none text-accent-ink";

/* ── the feed tile ──────────────────────────────────────────────────────── */

/**
 * One tile. The aspect ratio is set inline per card — see `tileRatio` in
 * `ArticleCard.tsx` — because it varies, which is what gives the masonry its
 * rhythm.
 */
export const TILE = "relative overflow-hidden rounded-tile bg-tile";
export const TILE_LINK = "absolute inset-0 block";
export const TILE_IMAGE = "h-full w-full object-cover";

/**
 * The typographic fallback, for an article with no picture.
 *
 * Not a placeholder graphic and not a grey box: the headline is set large on
 * the tile ground, so a tile without an image is a legitimate design rather
 * than a missing asset. This is the normal case, not the exception.
 */
export const TILE_TYPESET =
  "flex h-full w-full flex-col justify-end gap-2 p-4";
/**
 * An article's title, so `font-ui` — see the type block in `styles/youth.css`.
 * The tile headline is the smallest type in the product that has to be read
 * rather than glanced at, and at 11.5–15px over a photograph the serif's
 * modulation is the first thing to go.
 */
export const TILE_TYPESET_HEADLINE =
  "text-pretty font-ui text-[15px] font-bold leading-[1.2] tracking-[-0.015em] text-ink";
export const TILE_TYPESET_KICKER =
  "font-body text-kicker font-bold uppercase text-ink-3";

/** The scrim and the text over it. `pointer-events-none` so the link wins. */
export const TILE_SCRIM =
  "ew-tile-scrim pointer-events-none absolute inset-x-0 bottom-0 z-[3] max-h-full pt-9";
export const TILE_TEXT = "px-3 pb-3";
export const TILE_KICKER =
  "mb-1 flex items-center gap-1.5 font-body text-[8.5px] font-bold uppercase leading-none tracking-[0.13em] text-[rgb(255_253_250/0.78)]";
export const TILE_QUALIFIER =
  "mb-1 line-clamp-1 font-body text-[9px] leading-tight text-[rgb(255_253_250/0.66)]";
/** The same title, over the picture. `font-ui` for the reason above. */
export const TILE_HEADLINE =
  "line-clamp-3 text-pretty font-ui text-[11.5px] font-bold leading-[1.28] tracking-[-0.012em] text-[#fffdfa]";

/**
 * The Save control, floated over the tile.
 *
 * `z-[4]` puts it above both the scrim and the link overlay, which is what
 * makes it pressable at all — everything below it is one big anchor.
 */
export function tileSaveButton(saved: boolean): string {
  return `absolute right-2 top-2 z-[4] rounded-full border-0 px-[11px] py-1.5 font-body text-[9.5px] font-bold uppercase leading-none tracking-[0.1em] transition-opacity hover:opacity-90 disabled:opacity-60 ${
    saved
      ? "bg-[rgb(26_24_23/0.92)] text-[#fffdfa]"
      : "bg-[rgb(255_253_250/0.92)] text-[#201e1d]"
  }`;
}

/* ── the grid and its states ────────────────────────────────────────────── */

/** Zero-height; the IntersectionObserver target for the next page. */
export const SCROLL_SENTINEL = "h-px";
export const LOADING_MORE = "py-6 font-body text-meta text-ink-3";

/**
 * Skeleton tiles, not a spinner: a spinner collapses the layout, so the page
 * jumps the moment content lands.
 */
export const SKELETON_GRID =
  "grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-3.5";
export const SKELETON_TILE = "animate-pulse rounded-tile bg-tile";

export const FEED_STATE = "border-t border-rule-soft py-16";
export const FEED_STATE_TITLE =
  "font-heading text-[17px] font-bold leading-tight text-ink";
export const FEED_STATE_BODY = "mt-2 max-w-[60ch] font-body text-[13.5px] text-ink-3";
export const CLEAR_FILTERS_ACTION = `mt-4 inline-block ${ACCENT_TEXT_ACTION}`;
export const RETRY_BUTTON = `mt-4 ${PILL_BUTTON}`;

/* ── the article page ───────────────────────────────────────────────────── */

export const ARTICLE_PAGE = "mx-auto max-w-article px-gutter pb-[70px] pt-[34px]";
export const ARTICLE_ERROR_PAGE = "mx-auto max-w-article px-gutter py-24";
export const ARTICLE_ERROR_TITLE = "font-heading text-headline font-extrabold text-ink";
export const BACK_TO_FEED_LINK = `mt-4 inline-block ${ACCENT_TEXT_ACTION} uppercase tracking-[0.06em]`;

export const ARTICLE_BACK_LINK =
  "inline-block border-0 bg-transparent p-0 pb-[26px] font-body text-micro font-semibold uppercase leading-none tracking-[0.13em] text-ink-3 transition-colors hover:text-ink";

export function articleKicker(subjectText: string): string {
  return `mb-4 font-body text-micro font-bold uppercase tracking-[0.14em] ${subjectText}`;
}

/**
 * The article's own title — the one heading that stays on Archivo, and the one
 * element on the page that is centred.
 *
 * Centred *alone*, deliberately: the kicker, lede, byline and verdict bar stay
 * flush left. A centred headline over a left-aligned page reads as a masthead;
 * centring the block under it would make the whole header a poster, and the
 * verdict bar is a row of facts rather than a title card.
 */
export const ARTICLE_TITLE =
  "text-pretty text-center font-ui text-title font-bold leading-[1.04] tracking-[-0.03em] text-ink";
/** Sized off the body, not fixed: it must stay a step above `text-prose`. */
export const ARTICLE_LEDE =
  "mt-5 text-pretty font-body text-[23px] leading-[1.45] text-ink-2";

/** The byline strip — pipe-separated facts, not a table. */
export const ARTICLE_BYLINE =
  "mt-6 flex flex-wrap items-center gap-x-2.5 gap-y-1.5 pb-4 font-body text-[12px] text-ink-3";
export const ARTICLE_BYLINE_STRONG = "font-semibold text-ink";
export const ARTICLE_BYLINE_SEP = "text-ink-4";

/**
 * The two byline facts about *this page* — how long it takes to read and that a
 * person signed it off — stepped up to full ink, but only on the dark theme.
 *
 * The **only** `dark:` variant in the app, and it is deliberate rather than a
 * crack in the token system: everything else flips because its token flips, and
 * that is still the rule. This one asks for a different *relationship* to the
 * byline in each theme — the same step up that reads as emphasis on the dark
 * ground is heavier than the byline wants on the light one — and a token cannot
 * express "one ramp step on one theme and none on the other".
 *
 * Safe because `dark:` is configured as `html[data-theme="dark"]`
 * (`tailwind.config.ts`) and the pre-paint script in `index.html` resolves the
 * system preference *into* that attribute. There is no bare
 * `prefers-color-scheme` rule anywhere, so the variant and the palette switch
 * on exactly the same condition. Add a media-query palette and this silently
 * stops following it.
 */
export const ARTICLE_BYLINE_LIFT = "dark:text-ink";

/** Verdict, its scope limit and the grade rungs, on one ruled line. */
export const ARTICLE_VERDICT_BAR =
  "mb-[30px] flex flex-wrap items-center gap-x-3 gap-y-2 border-y border-rule-soft py-3.5";
export const ARTICLE_VERDICT_QUALIFIER = "font-body text-[12.5px] text-ink-3";
export const VERDICT_GLOSS_TEXT =
  "-mt-2 mb-7 max-w-[60ch] font-body text-[13px] leading-normal text-ink-3";

/**
 * The retraction notice, above the headline rather than beside the sources.
 *
 * Placed first because it changes how the rest of the page should be read, and
 * a reader who stops after the verdict must still have seen it. It uses the
 * accent ink and a heavy left rule — the same restraint as the rest of the
 * design, one step louder — because this is the only element on a public page
 * that says our own article may be wrong.
 */
export const RETRACTION_BANNER =
  "mb-6 rounded-field border-l-2 border-accent bg-surface px-4 py-3 font-body text-meta text-ink";
export const RETRACTION_BANNER_LABEL =
  "block font-body text-kicker font-bold uppercase tracking-[0.06em] text-accent-ink";
export const RETRACTION_BANNER_TEXT = "mt-1.5 block max-w-[64ch] text-ink-2";

/** Marks the withdrawn paper in the source list, so the banner is actionable. */
export const SOURCE_RETRACTED =
  "mt-1 font-body text-micro font-semibold uppercase tracking-[0.04em] text-accent-ink";

/** Tracks the prose measure, so it sits under the body rather than beside it. */
export const ARTICLE_DISCLAIMER =
  "mt-8 max-w-[800px] font-body text-[11.5px] leading-[1.5] text-ink-4";

/* ── save and share ─────────────────────────────────────────────────────── */

export const ARTICLE_ACTIONS =
  "mt-10 flex flex-wrap items-center gap-2.5 border-t border-rule-soft pt-5";

/** A native select, restyled as a pill. The chevron is drawn beside it. */
export const FOLDER_SELECT_WRAP = "relative inline-flex items-center";
export const FOLDER_SELECT =
  "appearance-none rounded-full border border-rule bg-surface py-3 pl-[18px] pr-10 font-body text-[12px] text-ink";
export const FOLDER_SELECT_CHEVRON =
  "pointer-events-none absolute right-[17px] top-1/2 h-[7px] w-[7px] -translate-y-[70%] rotate-45 border-b-[1.6px] border-r-[1.6px] border-ink-3";

/** The filled action. Ink, not accent — the accent belongs to the console. */
export const PRIMARY_PILL =
  "rounded-full border-0 bg-invert px-5 py-3 font-body text-[11px] font-bold uppercase leading-none tracking-[0.12em] text-invert-fg transition-colors hover:bg-accent-ink disabled:opacity-50";
export const SECONDARY_PILL =
  "rounded-full border border-rule bg-transparent px-5 py-3 font-body text-[11px] font-bold uppercase leading-none tracking-[0.12em] text-ink transition-colors hover:border-ink";

/* ── read more ──────────────────────────────────────────────────────────── */

export const RECS_SECTION = "mt-11 border-t border-rule-soft pt-[22px]";
export const RECS_TITLE =
  "font-heading text-[17px] font-bold tracking-[-0.015em] text-ink";
export const RECS_STANDFIRST = "mt-1 font-body text-[13px] text-ink-3";
export const RECS_GRID = "mt-5 grid grid-cols-1 gap-3.5 sm:grid-cols-3";
export const REC_CARD =
  "block rounded-tile border border-rule-soft bg-surface px-5 pb-[22px] pt-5 transition-colors hover:border-ink";
export const REC_KICKER =
  "mb-2.5 font-body text-[9.5px] font-bold uppercase tracking-[0.13em] text-ink-3";
/** An article title again, in the recommendation cards. */
export const REC_TITLE =
  "text-pretty font-ui text-[15.5px] font-bold leading-[1.22] tracking-[-0.015em] text-ink";

/* ── skeletons ──────────────────────────────────────────────────────────── */

export const ARTICLE_SKELETON_PAGE =
  "mx-auto max-w-article animate-pulse px-gutter pb-[70px] pt-10";
export const SKELETON_KICKER = "h-5 w-40 rounded-full bg-surface-2";
export const SKELETON_TITLE = "mt-6 h-12 w-4/5 rounded-field bg-surface-2";
export const SKELETON_LEDE = "mt-4 h-6 w-full rounded-field bg-surface-2";
export const SKELETON_LEAD_IMAGE = "mt-8 aspect-[16/9] w-full rounded-tile bg-surface-2";
export const SKELETON_PROSE = "mt-8 space-y-3";
export const SKELETON_PARAGRAPH = "h-24 rounded-field bg-surface-2";

/* ── article prose and citation chips ───────────────────────────────────── */

/**
 * The body column: **the article frame's full width** — 800px less the 28px
 * gutters, so ~744px of type, which is ~111 characters a line at `text-prose`
 * (20px Instrument Serif).
 *
 * The count is measured, not estimated, and it is the number to watch here, not
 * the pixel. Instrument Serif averages **5.71px a character at 17px** — run the
 * font's advance widths over a paragraph of real article prose and divide. That
 * is ~0.34em, far narrower than a text serif, because it is a display face
 * doing a body job. Two numbers in this file were wrong because nobody had run
 * that:
 *
 *   - `max-w-[64ch]` rendered as 441px, ~77 characters. `ch` is the width of
 *     the *wrapper's* zero and the wrapper inherits 15px while the paragraphs
 *     are larger, so the unit was measuring the wrong font.
 *   - 520px was then written down as "~82 characters, the top of the
 *     comfortable range". It was ~91 — already past it.
 *
 * The column then sat at 600px / ~89 characters for a while, with `text-prose`
 * raised to 20px to hold that count. Filling the frame takes it to ~111, which
 * is past where that tuning left it: **the lever, if the line reads long, is
 * `text-prose`, not this width.** The relationship is
 * `chars ≈ 2.98 × width ÷ size`, so ~25px would put 744px back at ~89.
 *
 * `flow-root` contains the floats a reviewer may have placed; without it a
 * picture floated beside the last paragraph hangs below the prose and over the
 * disclaimer.
 */
export const PROSE_MEASURE = "flow-root max-w-[800px]";
export const PROSE_PARAGRAPH =
  "mb-[22px] text-pretty font-body text-prose text-ink-2 last:mb-0";

/**
 * A section title inside the article.
 *
 * `font-ui` because it is a *title*, and titles in this product are the one
 * place the sans is used — `ARTICLE_TITLE` is the same face. A serif subhead at
 * this size sits too close to the serif body to break it up, which is the whole
 * job of the element.
 *
 * The asymmetric margin is the point: a heading belongs to the prose beneath
 * it, so the space above it is nearly three times the space below. Set flush
 * with `text-ink` against the body's `text-ink-2`, so the hierarchy is carried
 * by weight and colour rather than by size alone — it must read as a step below
 * the 32–50px `h1` without competing with it.
 */
export const PROSE_HEADING =
  "mb-2.5 mt-9 text-pretty font-ui text-[19px] font-bold leading-[1.25] tracking-[-0.02em] text-ink first:mt-0";

/** `whitespace-nowrap` so a chip never wraps away from the word it follows. */
export const CHIP_ANCHOR = "relative whitespace-nowrap";

/**
 * Sized to be pressed. The comp weighed a superscript and a dotted underline;
 * a superscript is a 4px tap target that vanishes at body size, and underlining
 * the sentence makes the *claim* look uncertain rather than its source
 * available. `.ew-chip` supplies the brackets via `::before`/`::after`, so the
 * handle alone is what gets copied and read aloud.
 */
export const CITATION_CHIP =
  "ew-chip ml-0.5 inline-block font-body font-semibold leading-none tracking-[0.03em] text-ink-3 transition-colors hover:border-accent hover:text-accent-ink";

export const SOURCE_POPOVER =
  "absolute left-0 top-[calc(100%+9px)] z-30 block w-[296px] whitespace-normal rounded-field border border-rule-soft bg-surface px-3.5 pb-3.5 pt-[13px] shadow-pop";
export const POPOVER_KICKER =
  "block font-body text-kicker font-bold uppercase text-ink-3";
export const POPOVER_TITLE =
  "mt-2 block font-heading text-[14px] font-semibold leading-[1.35] text-ink";
export const POPOVER_META = "mt-[5px] block font-body text-meta text-ink-3";
export const POPOVER_LINK =
  "mt-2.5 inline-block border-b border-accent pb-0.5 font-body text-micro font-semibold uppercase leading-none tracking-[0.06em] text-accent-ink";
export const POPOVER_UNRESOLVED = "mt-2 block font-body text-meta text-ink-3";

/* ── pictures and video a reviewer added ────────────────────────────────── */

/**
 * Media sits inside the prose measure rather than breaking out of it.
 *
 * A full-bleed image is the magazine move, and this is not a magazine: the
 * column is ~89 characters because that is where the evidence reads well, and a
 * picture that escapes it announces itself as the more important thing on the
 * page.
 *
 * The width and the wrap come from `.ew-media` in `styles/youth.css` — the same
 * classes the console's editor uses, which is what makes the reviewer's layout
 * something they can actually see before approving it. Everything inside those
 * classes stops applying below 640px, so a floated picture becomes a full-width
 * block on a phone without anyone deciding that per article.
 */
export function articleFigure(align: MediaAlign): string {
  return `${mediaWrapClass(align)} last:mb-0`;
}

export const ARTICLE_IMAGE = "block h-auto w-full rounded-tile";

/**
 * Video is a facade until it is pressed.
 *
 * An embedded player is roughly a megabyte of Google's JavaScript, executed on
 * every reader of every article that has one, to render a rectangle most of
 * them will not press. The still frame is one image request, and the iframe
 * arrives on the click that asks for it — which is also the click that makes
 * being tracked by YouTube something the reader chose.
 */
export const VIDEO_FRAME =
  "relative block aspect-video w-full overflow-hidden rounded-tile bg-tile";
export const VIDEO_COVER = "absolute inset-0 h-full w-full object-cover";
export const VIDEO_IFRAME = "absolute inset-0 h-full w-full";

/**
 * The play affordance.
 *
 * A solid accent block rather than a translucent overlay: the thumbnail's
 * colours are decided by whoever uploaded the video, and the ink ramp does not
 * carry opacity (see the note in `tailwind.config.ts`), so the only mark that
 * stays legible over an unknown image is an opaque one.
 */
export const VIDEO_PLAY_BUTTON =
  "group absolute inset-0 flex items-center justify-center";
export const VIDEO_PLAY_MARK =
  "flex h-[54px] w-[78px] items-center justify-center rounded-full bg-accent transition-transform group-hover:scale-105";
export const VIDEO_PLAY_TRIANGLE =
  "ml-1 border-y-[11px] border-l-[18px] border-y-transparent border-l-white";
export const VIDEO_CAPTION = "mt-2 font-body text-micro text-ink-3";

/* ── the source list ────────────────────────────────────────────────────── */

/**
 * The sources sit in a panel under the article rather than in a sidebar.
 *
 * The previous design pinned them beside the prose, which answered "what is
 * this resting on?" without scrolling. This one answers it at the end, and the
 * citation chips are what serve the mid-article question — each one opens the
 * paper it points at in place. On this measure a 320px sidebar would leave the
 * prose too narrow to be the thing the page is for.
 */
export const SOURCES_SECTION =
  "mt-9 rounded-panel border border-rule-soft bg-surface-2 px-7 py-[26px]";
export const SOURCES_HEAD = "flex items-baseline justify-between gap-2.5";
export const SOURCES_TITLE = "font-heading text-[14px] font-bold text-ink";
export const SOURCES_COUNT = "font-body text-[11.5px] text-ink-3";
export const SOURCES_EMPTY_NOTE = "mt-3 font-body text-micro text-ink-3";
export const SOURCES_FOOTNOTE = "mt-4 font-body text-micro text-ink-3";
export const SOURCE_LIST = "mt-3.5 flex list-none flex-col gap-2.5 p-0";

/**
 * The row the reader just jumped to is washed with the accent, so the anchor
 * lands somewhere visible. `scroll-mt-[90px]` keeps it clear of the site bar.
 */
export function sourceRow(active: boolean): string {
  return `scroll-mt-[90px] rounded-field px-2 py-1.5 transition-colors ${
    active ? "bg-accent-wash" : "bg-transparent"
  }`;
}

export const SOURCE_ROW_HEAD = "flex items-baseline gap-2.5";

/** The handle, as a pill — the same shape as the chip in the prose. */
export const SOURCE_HANDLE =
  "flex-none rounded-full border border-rule-soft px-2 py-0.5 font-body text-[10.5px] font-bold leading-none tracking-[0.06em] text-accent-ink";
export const SOURCE_TITLE_LINK =
  "text-pretty font-body text-[13px] leading-[1.5] text-ink-2 hover:text-accent-ink";

/**
 * Weak study types are drawn in the accent ink — the one place colour touches
 * evidence, and it flags the *source*, never the verdict.
 */
export function sourceStudyType(weak: boolean): string {
  return `mt-1 font-body text-micro ${weak ? "text-accent-ink" : "text-ink-3"}`;
}

export const SOURCE_META = "font-body text-micro text-ink-3";

/** The study type and journal, indented under the title they describe. */
export const SOURCE_SUBLINE = "ml-[38px]";

export const CITATION_MAP = "mt-4";
export const CITATION_MAP_SUMMARY =
  "cursor-pointer font-body text-micro text-ink-3 hover:text-ink";
export const CITATION_MAP_LIST = "mt-2 flex list-none flex-col gap-1.5 p-0";
export const CITATION_MAP_ITEM = "font-body text-micro text-ink-3";
export const CITATION_MAP_CLAIM = "font-semibold text-ink-2";
export const CITATION_MAP_HANDLE = "underline";
