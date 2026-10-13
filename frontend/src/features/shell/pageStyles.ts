/**
 * Names for the four standing public pages — About, Join, You, Let us know.
 *
 * These share a shape the feed and the article do not: a narrow measure, a
 * "← Feed" escape at the top, and forms. Keeping their names together rather
 * than in `features/feed/styles.ts` is what stops the feed's file becoming the
 * place every string ends up.
 *
 * The form controls in here are the *public* set. The console has its own in
 * `features/console/controls.ts`, and they are deliberately not shared: the
 * console's primary action is the accent fill because exactly one button there
 * publishes, and reusing that treatment out here would make "Build my feed"
 * look like the most consequential control in the product.
 */

/* ── shared page frame ──────────────────────────────────────────────────── */

export const PAGE = "mx-auto max-w-prose px-gutter pb-20 pt-[34px]";
export const WIDE_PAGE = "mx-auto max-w-page px-gutter pb-20 pt-[34px]";

export const PAGE_BACK_LINK =
  "inline-block pb-[22px] font-body text-micro font-semibold uppercase leading-none tracking-[0.13em] text-ink-3 transition-colors hover:text-ink";

export const PAGE_KICKER =
  "mb-4 font-body text-micro font-bold uppercase tracking-[0.14em] text-accent-ink";
export const PAGE_TITLE =
  "text-pretty font-heading text-[clamp(28px,3.4vw,40px)] font-bold leading-[1.06] tracking-[-0.03em] text-ink";
export const PAGE_STANDFIRST =
  "mt-3.5 max-w-[52ch] font-body text-[15.5px] leading-[1.6] text-ink-2";

/* ── about ──────────────────────────────────────────────────────────────── */

export const ABOUT_PAGE = "mx-auto max-w-[720px] px-gutter pb-20 pt-[34px]";
export const ABOUT_LOGO_WRAP = "mb-[26px] text-center leading-none";
export const ABOUT_LOGO = "youth-mark mx-auto h-auto w-[300px] max-w-full";

/**
 * The one centred display heading in the product.
 *
 * Everything else sets display type flush left. This page is a statement of
 * intent rather than something to be read across, and centring it is what makes
 * it read as a masthead instead of as the first paragraph.
 */
export const ABOUT_TITLE =
  "mx-auto max-w-[16ch] text-balance text-center font-heading text-[clamp(34px,5vw,54px)] font-bold leading-[1.06] tracking-[-0.03em] text-ink";
export const ABOUT_RULE = "my-[30px] h-px bg-rule-soft";
export const ABOUT_LEAD =
  "mb-6 text-pretty text-center font-body text-[19px] leading-[1.6] text-ink";

/** The medical disclaimer, given a panel so it cannot be skimmed past. */
export const ABOUT_CALLOUT =
  "mb-11 rounded-panel bg-surface-2 px-[26px] py-[22px] font-body text-[15px] font-bold leading-[1.6] text-ink";

export const ABOUT_RULES =
  "grid grid-cols-1 gap-x-8 gap-y-6 border-t border-rule-soft pt-5 sm:grid-cols-3";
export const ABOUT_RULE_NUMBER =
  "mb-1.5 font-heading text-[30px] font-bold tracking-[-0.03em] text-ink";
export const ABOUT_RULE_BODY = "font-body text-[13px] leading-[1.5] text-ink-2";

/* ── forms ──────────────────────────────────────────────────────────────── */

export const FORM_GRID = "grid grid-cols-1 gap-3.5 sm:grid-cols-2";
export const FIELD_LABEL =
  "mb-[7px] block font-body text-label-sm font-bold uppercase tracking-[0.13em] text-ink-3";
export const FIELD =
  "w-full rounded-field border border-rule bg-surface px-4 py-[13px] font-body text-[14px] text-ink placeholder:text-ink-4 focus:border-ink-3";
export const TEXTAREA = `${FIELD} min-h-[120px] resize-y leading-[1.55]`;

export const CHECKBOX_ROW = "flex cursor-pointer items-start gap-2.5";
export const CHECKBOX = "mt-0.5 h-4 w-4 flex-none accent-[var(--ew-accent)]";
export const CHECKBOX_LABEL = "font-body text-[12.5px] leading-[1.5] text-ink-2";

/**
 * The public primary action.
 *
 * Accent-filled and full width, unlike the ink pill on an article: this is the
 * one thing to do on the page it appears on, and there is nothing beside it to
 * compete with. Flush-left label, per the design system.
 */
export const SUBMIT_BUTTON =
  "w-full rounded-full border-0 bg-accent px-6 py-4 text-left font-body text-[12px] font-bold uppercase leading-none tracking-[0.12em] text-accent-on transition-colors hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-50";

export const FORM_NOTE = "mt-3.5 font-body text-[11.5px] text-ink-4";

/**
 * The password requirements under the sign-up fields, ticked off as they are
 * met. Ink rather than a red/green pair: an unmet rule is not an error until
 * the form is submitted, and the mark is what changes, not just the colour.
 */
export const PASSWORD_RULE_LIST =
  "mb-6 grid grid-cols-1 gap-x-4 gap-y-1.5 sm:grid-cols-2";
export function passwordRule(met: boolean): string {
  return `flex items-center gap-2 font-body text-[12px] leading-snug ${
    met ? "text-ink-2" : "text-ink-4"
  }`;
}
export const PASSWORD_RULE_MARK = "w-3 flex-none text-center text-accent-ink";

/**
 * A failure the reader has to act on, next to the control that failed.
 *
 * Not the toast: a toast disappears, and a rejected password or a taken email
 * needs to stay on screen while it is being fixed.
 */
export const FORM_ERROR =
  "mt-3.5 rounded-field border-l-2 border-accent bg-accent-wash px-3.5 py-2.5 font-body text-[12.5px] leading-normal text-ink";

/* ── chips ──────────────────────────────────────────────────────────────── */

export const CHIP_ROW = "flex flex-wrap gap-2";

/** A choice among several — interests, contact kinds, folder tabs. */
export function chip(active: boolean): string {
  return `inline-flex items-center gap-2 rounded-full border px-4 py-2.5 font-body text-[12px] font-semibold tracking-[0.02em] transition-colors ${
    active
      ? "border-invert bg-invert text-invert-fg"
      : "border-rule bg-transparent text-ink hover:border-ink"
  }`;
}

export const CHIP_COUNT = "text-[10.5px] opacity-60";

/* ── the two-column pages (Join, Let us know) ───────────────────────────── */

export const SPLIT =
  "mx-auto grid max-w-[1040px] grid-cols-1 gap-8 lg:grid-cols-[1.15fr_0.85fr] lg:gap-14";

/** The dark explainer beside the sign-up form. */
export const PANEL = "self-start rounded-panel bg-panel px-8 py-[34px] text-panel-fg";
export const PANEL_LABEL =
  "mb-[22px] font-body text-label-sm font-bold uppercase tracking-[0.14em] opacity-60";
export const PANEL_LIST = "flex flex-col gap-5";
export const PANEL_ITEM_TITLE = "mb-[5px] font-heading text-[15px] font-bold";
export const PANEL_ITEM_BODY = "font-body text-[13px] leading-[1.55] opacity-75";
export const PANEL_DIVIDER = "h-px bg-current opacity-20";

/* ── the profile page ───────────────────────────────────────────────────── */

export const PROFILE_HEAD =
  "mb-[26px] flex flex-wrap items-end justify-between gap-4 border-b border-rule-soft pb-[22px]";
export const PROFILE_IDENTITY = "flex items-center gap-4";
export const PROFILE_AVATAR =
  "flex h-[58px] w-[58px] flex-none items-center justify-center rounded-full bg-invert font-heading text-[18px] font-bold text-invert-fg";
export const PROFILE_NAME =
  "mb-[5px] font-heading text-[26px] font-bold tracking-[-0.02em] text-ink";
export const PROFILE_META = "font-body text-[12.5px] text-ink-3";

export const GUEST_CARD =
  "mb-7 max-w-[560px] rounded-panel border border-rule-soft bg-surface px-8 py-[34px]";
export const GUEST_TITLE = "mb-2 font-heading text-[19px] font-bold text-ink";
export const GUEST_BODY = "mb-[22px] font-body text-[14px] leading-[1.6] text-ink-2";

export const NEW_FOLDER_ROW = "mb-[26px] mt-2 flex items-center gap-2";
export const NEW_FOLDER_INPUT =
  "w-[170px] border-0 border-b border-rule bg-transparent px-0 py-[7px] font-body text-[12.5px] text-ink placeholder:text-ink-4 focus:border-ink";
export const NEW_FOLDER_ACTION =
  "border-0 bg-transparent px-0 py-[7px] font-body text-label-sm font-bold uppercase tracking-[0.12em] text-accent-ink hover:text-accent";

export const SAVED_GRID = "mt-2";
export const EMPTY_SHELF = "border-t border-rule-soft py-14";
export const EMPTY_SHELF_TITLE = "mb-2 font-heading text-[17px] font-bold text-ink";
export const EMPTY_SHELF_BODY = "font-body text-[13.5px] text-ink-3";

/* ── a completed form ───────────────────────────────────────────────────── */

export const DONE_CARD =
  "max-w-[560px] rounded-panel border border-rule-soft bg-surface px-8 py-[34px]";
export const DONE_TITLE = "mb-2 font-heading text-[19px] font-bold text-ink";
export const DONE_BODY = "mb-[22px] font-body text-[14px] leading-[1.6] text-ink-2";
export const DONE_ACTION =
  "rounded-full border-0 bg-invert px-[22px] py-3.5 font-body text-[11px] font-bold uppercase leading-none tracking-[0.12em] text-invert-fg transition-colors hover:bg-accent-ink";

/* --- legal pages and the site footer ------------------------------------ */

/**
 * Privacy and Terms. Prose pages at the reading measure, set like the article
 * body rather than like the marketing pages, because they are read the same
 * way: top to bottom, looking for one answer.
 */
export const LEGAL_PAGE = "mx-auto max-w-prose px-gutter pb-20 pt-[34px]";
export const LEGAL_UPDATED = "mt-3 font-body text-[12px] text-ink-4";
export const LEGAL_H2 =
  "mb-3 mt-10 font-heading text-[22px] font-bold leading-tight tracking-[-0.01em] text-ink";
export const LEGAL_P = "mb-4 font-body text-[15.5px] leading-[1.65] text-ink-2";
export const LEGAL_LIST =
  "mb-4 list-disc space-y-2 pl-5 font-body text-[15.5px] leading-[1.6] text-ink-2";
export const LEGAL_TABLE =
  "mb-5 w-full border-collapse font-body text-[13.5px] leading-[1.5] text-ink-2";
export const LEGAL_TH =
  "border-b border-rule px-2 py-2 text-left align-top font-bold text-ink";
export const LEGAL_TD = "border-b border-rule-soft px-2 py-2 align-top";
/**
 * A fact only the operator can supply. Marked loudly on purpose — a page that
 * looks finished while naming no controller is worse than one that is plainly
 * a draft. See COMPLIANCE_BUSINESS_INPUTS.md.
 */
export const LEGAL_PLACEHOLDER =
  "rounded-[3px] bg-accent-wash px-1 font-semibold text-ink";
export const LEGAL_DRAFT_NOTE =
  "mb-8 rounded-field border-l-2 border-accent bg-accent-wash px-3.5 py-2.5 font-body text-[12.5px] leading-normal text-ink";

/** The one line under every collection form that says what happens to it. */
export const COLLECTION_NOTICE =
  "mt-3.5 font-body text-[11.5px] leading-[1.5] text-ink-3";

export const SITE_FOOTER =
  "mx-auto flex max-w-page flex-wrap items-center gap-x-5 gap-y-2 border-t border-rule-soft px-gutter py-6 font-body text-[11px] uppercase tracking-[0.12em] text-ink-3";
export const SITE_FOOTER_LINK = "transition-colors hover:text-ink";

/* --- "Your data" on /you --------------------------------------------------- */

export const DATA_SECTION = "mt-14 border-t border-rule-soft pt-8";
export const DATA_TITLE =
  "mb-2 font-heading text-[22px] font-bold leading-tight text-ink";
export const DATA_BODY = "mb-5 max-w-[60ch] font-body text-[14px] leading-[1.6] text-ink-2";
export const DATA_ACTIONS = "mb-6 flex flex-wrap items-center gap-3";
export const DANGER_BUTTON =
  "inline-flex items-center gap-2 rounded-full border border-accent bg-transparent px-3.5 py-[7px] font-body text-[10.5px] font-semibold uppercase leading-none tracking-[0.12em] text-accent-ink transition-colors hover:bg-accent-wash disabled:cursor-not-allowed disabled:opacity-50";
export const DELETE_FORM =
  "mt-2 max-w-[420px] rounded-field border border-rule px-4 py-4";

/** Off-screen until focused, then pinned over the site bar's top-left. */
export const SKIP_LINK =
  "sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-[100] focus:rounded-full focus:bg-ink focus:px-4 focus:py-2.5 focus:font-body focus:text-[12px] focus:font-semibold focus:text-ground";
