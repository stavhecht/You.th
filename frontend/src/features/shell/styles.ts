/**
 * Names for the shared chrome's styling.
 *
 * The site bar is the one surface every public route renders, so its
 * measurements are the ones that must not drift: the header height, the page
 * measure and the gutter are what every route below aligns to.
 *
 * Two rules the comp establishes and this file enforces:
 *
 * - **Actions are pills, content is rounded rectangles.** Now that tiles and
 *   panels have corners, `rounded-full` is what still separates a control from
 *   a card at a glance. Anything clickable and standalone in here is a pill.
 * - **The nav buttons are the product's other sans.** Everything else in the
 *   interface is set in Instrument Serif; the buttons in this bar, in the
 *   drawer and in the tab bar keep Archivo via `font-ui`, because they are
 *   10–12px tracked-out caps and a display serif gives up its shapes there.
 *   Article titles are the only other caller — see `styles/youth.css`.
 * - **The logo is centred and the nav is not.** The bar is a three-column grid
 *   with the mark in the middle column, so the wordmark stays optically centred
 *   on the page regardless of how wide the two nav clusters grow.
 * - **The bar spans the window; the page below does not.** The menu sits at
 *   the window's left edge and the account controls at its right, a gutter in
 *   from each, while content keeps the `max-w-page` measure. Chrome belongs to
 *   the window, content to the column.
 *
 * `ROUTE_MESSAGE` lives here rather than in either feature because all three
 * route-level waiting states — the lazy review boundary, the auth gate and the
 * review screen — render the same centred paragraph. They looked like three
 * coincidentally-identical strings; they are one decision about how a route
 * says "not yet".
 */

/* ── the site bar ───────────────────────────────────────────────────────── */

/** Sticky, and above the filter panel's z-30 so the bar is never overlapped. */
export const SITE_HEADER = "sticky top-0 z-40 border-b border-rule-soft bg-ground";

/**
 * Three columns with the mark in the middle, so it sits on the window's centre
 * line rather than wherever the left cluster happens to end. The outer columns
 * are `minmax(0,1fr)` so a long nav truncates instead of pushing the mark off
 * centre. Full width, not `max-w-page`: the clusters sit at the window's edges.
 */
export const HEADER_BAR =
  "grid h-header w-full grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-center gap-6 px-gutter";

export const HEADER_LEFT = "flex min-w-0 items-center gap-3.5";
export const HEADER_RIGHT = "flex flex-none items-center justify-end gap-3";

/** The wordmark. `youth-mark` is what inverts it on the dark theme. */
export const WORDMARK = "block leading-none";
export const WORDMARK_IMAGE = "youth-mark block h-auto w-[132px] max-w-full sm:w-[168px]";

/** The hamburger — three rules, drawn rather than iconified. */
export const MENU_BUTTON =
  "flex flex-none items-center gap-2.5 border-0 bg-transparent p-1.5 text-ink transition-colors hover:text-accent-ink";
export const MENU_BARS = "flex w-[17px] flex-col gap-[3px]";
export const MENU_BAR = "h-[2px] bg-current";

/** Search. A pill on the surface, so it reads as a field and not as a button. */
export const SEARCH_FORM =
  "flex min-w-0 max-w-[240px] flex-1 items-center gap-2 rounded-full border border-rule-soft bg-surface px-3.5 py-2 focus-within:border-ink-3 focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-accent-ink";
export const SEARCH_ICON = "flex-none text-ink-3";
/** A field, not a button — so it takes the interface serif like other prose. */
export const SEARCH_INPUT =
  "w-full min-w-0 border-0 bg-transparent p-0 font-body text-[12.5px] text-ink outline-none placeholder:text-ink-4";

/**
 * A rule that draws itself under the link from left to right on hover and
 * keyboard focus. `bg-current`, so it takes the link's own colour — ink for
 * About, accent for Join. Scaled rather than widened, so the animation is a
 * transform and never reflows the bar; under reduced motion it simply appears.
 */
const NAV_UNDERLINE =
  "relative after:absolute after:inset-x-0 after:-bottom-[5px] after:h-[1.5px] after:origin-left after:scale-x-0 after:bg-current after:transition-transform after:duration-300 after:ease-out hover:after:scale-x-100 focus-visible:after:scale-x-100 motion-reduce:after:transition-none";

const NAV_LINK = `${NAV_UNDERLINE} whitespace-nowrap font-ui text-micro font-semibold uppercase leading-none tracking-[0.13em] transition-colors`;

/**
 * The current route steps up in ink and keeps its rule drawn, so the underline
 * reads as "you are here" as well as "you could go here". The accent is
 * reserved for "your feed".
 */
export function navLink(isActive: boolean): string {
  return `${NAV_LINK} ${isActive ? "text-ink after:scale-x-100" : "text-ink-3 hover:text-ink"}`;
}

/** The one accented link in the bar — the account, which is the thing to do. */
export const NAV_LINK_ACCENT = `${NAV_LINK} text-accent-ink hover:text-accent`;

/** The avatar. Ink-filled, so it reads as a person rather than a control. */
export const AVATAR =
  "flex h-7 w-7 flex-none items-center justify-center rounded-full bg-invert font-ui text-[10.5px] font-bold leading-none tracking-[0.04em] text-invert-fg";

export const THEME_TOGGLE =
  "flex flex-none items-center justify-center rounded-full border border-rule bg-transparent p-[7px] leading-none text-ink-3 transition-colors hover:border-ink hover:text-ink";

/* ── the categories drawer ──────────────────────────────────────────────── */

export const DRAWER_SCRIM = "fixed inset-0 z-[80] bg-[rgb(20_18_17/0.42)]";
export const DRAWER =
  "fixed inset-y-0 left-0 z-[90] w-[330px] max-w-[84vw] animate-slide-in overflow-y-auto border-r border-rule-soft bg-ground px-6 py-[26px]";
export const DRAWER_HEAD = "mb-[26px] flex items-center justify-between";
export const DRAWER_TITLE =
  "font-body text-micro font-bold uppercase tracking-[0.14em] text-ink-3";
export const DRAWER_CLOSE =
  "border-0 bg-transparent p-1 text-[19px] leading-none text-ink hover:text-accent-ink";
export const DRAWER_LIST = "flex flex-col";

/**
 * A category row. `min-h-[44px]` is the tap target, and it is why these are 15px
 * of vertical padding rather than the 8 the type alone would want.
 */
export function drawerRow(active: boolean): string {
  return `flex min-h-[44px] items-baseline justify-between gap-2.5 border-0 border-b border-rule-soft bg-transparent py-[15px] text-left font-ui text-[17px] tracking-[-0.015em] transition-colors hover:text-accent-ink ${
    active ? "font-bold text-accent-ink" : "font-medium text-ink"
  }`;
}

/** Rides inside a drawer row, so it takes the row's face rather than the page's. */
export const DRAWER_COUNT = "font-ui text-[11.5px] font-normal text-ink-4";
export const DRAWER_FOOTER =
  "mt-7 flex flex-col gap-3.5 border-t border-rule-soft pt-5";
export const DRAWER_LINK =
  "font-ui text-[12px] font-semibold uppercase tracking-[0.12em] text-ink hover:text-accent-ink";
export const DRAWER_LINK_ACCENT =
  "font-ui text-[12px] font-semibold uppercase tracking-[0.12em] text-accent-ink hover:text-accent";

/* ── the mobile tab bar ─────────────────────────────────────────────────── */

/**
 * Shown below `sm` only, where the top bar has no room for the nav cluster.
 * Sticky at the bottom rather than fixed, so it cannot cover the last tile.
 *
 * It is the top nav in its small-screen form, not a fourth surface, so its
 * buttons take `font-ui` for the same reason the bar's do.
 */
export const TAB_BAR =
  "sticky bottom-0 z-40 grid grid-cols-4 border-t border-rule-soft bg-ground sm:hidden";

export function tabButton(active: boolean): string {
  return `min-h-[44px] border-0 border-t-2 bg-transparent px-0 pb-4 pt-3.5 font-ui text-[10px] font-bold uppercase leading-none tracking-[0.11em] transition-colors ${
    active ? "border-accent text-ink" : "border-transparent text-ink-3"
  }`;
}

/* ── the toast ──────────────────────────────────────────────────────────── */

export const TOAST =
  "fixed bottom-7 left-1/2 z-[120] -translate-x-1/2 animate-fade-up rounded-full bg-invert px-[22px] py-3.5 font-body text-[12.5px] font-medium leading-none text-invert-fg shadow-pop";

/* ── route-level states ─────────────────────────────────────────────────── */

/** A route-level waiting or empty state, on the page measure. */
export const ROUTE_MESSAGE =
  "mx-auto max-w-page px-gutter py-16 font-body text-meta text-ink-3";

/** The same, when the route failed rather than is still arriving. */
export const ROUTE_ERROR_MESSAGE =
  "mx-auto max-w-page px-gutter py-16 font-body text-meta text-accent-ink";
