/**
 * Names for the console's styling — queue, review screen, sources panel, login.
 *
 * The shared control shapes stay in `controls.ts`: `FIELD`, `PRIMARY`,
 * `SECONDARY` and `SECTION_LABEL` are the pieces whose *consistency* is a
 * safety property, and they are composed here rather than restated. Everything
 * in this file is the surrounding layout, per screen, in reading order.
 *
 * The accent is load-bearing on this side of the product: it is the only fill
 * in the console, so the one button that publishes is unmistakable. Anything
 * new that reaches for `bg-accent` is competing with Approve.
 */

import { FIELD, PRIMARY, SECONDARY } from "./controls";

/* ── shared console chrome ──────────────────────────────────────────────── */

/** Wider than the public measure — the queue is a working surface, not prose. */
export const CONSOLE_PAGE = "mx-auto max-w-console px-gutter pb-[90px]";

/**
 * The desk's own bar, and it does not look like the public site's.
 *
 * A 2px rule where the public header has a hairline, and a wordmark that names
 * the surface. A reviewer who cannot tell the queue from the live site at a
 * glance is a reviewer who can mistake a draft for something readers can
 * already see.
 */
export const REVIEW_HEADER = "sticky top-0 z-40 border-b-2 border-rule bg-ground";

/**
 * The same three-column grid as the public bar, so the mark sits on the page's
 * centre line rather than wherever the left cluster happens to end. The outer
 * columns are `minmax(0,1fr)` so a long reviewer name truncates instead of
 * pushing the logo off centre.
 *
 * The 2px bottom rule against the public bar's hairline is the whole visual
 * difference, and it is deliberate: the desk should be recognisably the same
 * product and unmistakably not the live site.
 */
export const REVIEW_BAR =
  "mx-auto grid h-header max-w-console grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-center gap-4 px-gutter";
export const REVIEW_BRAND = "block justify-self-center leading-none";
export const REVIEW_BRAND_LOGO = "youth-mark block h-auto w-[132px] sm:w-[150px]";

/** Names the surface, in the left column where the public bar puts browse. */
export const REVIEW_BRAND_TAG =
  "justify-self-start whitespace-nowrap font-body text-micro font-bold uppercase leading-none tracking-[0.13em] text-accent-ink";
export const REVIEW_NAV =
  "flex min-w-0 flex-wrap items-center justify-end gap-3 justify-self-end";
/** A nav button in a bar, so it takes `font-ui` like the public site's do. */
export const REVIEW_NAV_LINK =
  "whitespace-nowrap font-ui text-micro font-semibold uppercase leading-none tracking-[0.11em] text-ink-3 transition-colors hover:text-ink";

export const SIGNED_IN_AS =
  "inline-flex items-baseline gap-[7px] font-body text-[12px] leading-normal text-ink-3";
export const SIGN_OUT_ACTION =
  "border-b border-accent pb-px font-body text-[12px] font-semibold leading-normal text-accent-ink";

/* ── the review queue ───────────────────────────────────────────────────── */

export const QUEUE_HEADER =
  "flex flex-wrap items-baseline justify-between gap-4 border-b-2 border-rule pb-4 pt-[38px]";
export const QUEUE_TITLE = "font-heading text-headline font-extrabold text-ink";
export const QUEUE_STANDFIRST = "mt-[9px] font-body text-field leading-normal text-ink-3";
export const QUEUE_MESSAGE = "py-8 font-body text-meta text-ink-3";
export const QUEUE_LIST = "list-none p-0";
export const QUEUE_FOOTNOTE = "mt-5 font-body text-[12px] leading-normal text-ink-3";

export const TAB_BAR = "mt-[22px] flex gap-0.5 border-b border-rule-soft";

/** `-mb-px` pulls the active tab's rule over the bar's, so they read as one. */
export function queueTab(current: boolean): string {
  return `-mb-px border-b-2 px-[13px] py-[11px] font-body text-[12px] font-semibold leading-none tracking-[0.05em] transition-colors ${
    current ? "border-ink text-ink" : "border-transparent text-ink-3 hover:text-ink"
  }`;
}

/** Only the open tab's count is known — the queue is fetched one status at a time. */
export const TAB_COUNT = "ml-1.5 font-normal text-ink-4";

/**
 * The row carries the verdict mark and the grade bar at once, because triage is
 * really the question "which of these is a confident verdict on thin evidence?"
 * — and that is a question about the pair.
 */
export function queueRow(subjectBorderLeft: string): string {
  return `grid grid-cols-1 items-start gap-4 border-b border-l-[3px] border-rule-soft py-[18px] pl-[15px] pr-0.5 sm:grid-cols-[minmax(0,1fr)_200px] sm:gap-[26px] ${subjectBorderLeft}`;
}

export const QUEUE_ROW_LINK = "min-w-0 transition-colors hover:text-ink";
export const QUEUE_ROW_SIGNALS = "flex flex-wrap items-center gap-[9px]";
export const QUEUE_VERDICT_WORDING =
  "font-body text-label font-semibold uppercase leading-none tracking-[0.09em] text-ink-3";
export const QUEUE_VALIDATION_BADGE = "font-body text-micro leading-none text-ink-3";

/** Outlined, never filled: a prompt to check the sources, not a rejection. */
export const WEAK_EVIDENCE_FLAG =
  "border border-accent px-[7px] py-1 font-body text-label font-semibold normal-case leading-none tracking-normal text-accent-ink";

/** An article title, so Archivo — the same face the tile and the page use. */
export const QUEUE_ROW_HEADLINE =
  "mt-[9px] text-pretty font-ui text-row font-semibold text-ink";
export const QUEUE_ROW_META = "mt-1 font-body text-meta text-ink-3";
export const QUEUE_ROW_ERROR = "mt-1 font-body text-micro text-accent-ink";
export const QUEUE_ROW_ASIDE = "flex items-start justify-between gap-3";
export const QUEUE_ROW_GRADE = "flex flex-col items-start gap-[7px]";
export const QUEUE_ROW_GRADE_LABEL = "font-body text-micro text-ink-3";

/** Discard is a rejection, not a delete — there is no DELETE on articles. */
export const DISCARD_BUTTON =
  "shrink-0 border border-rule-soft p-1.5 text-ink-3 transition-colors hover:border-ink hover:text-accent-ink disabled:opacity-45";

/* ── a run still in flight ──────────────────────────────────────────────── */

/**
 * The placeholder row for a draft still being generated.
 *
 * Same rule, padding and left-rule *width* as `queueRow`, so it sits in the
 * list rather than on top of it — but the left rule is the hairline rather than
 * `queueRow`'s structural `border-l-rule`, and there is no aside. Both are the
 * same statement: this row has no verdict, no grade and nothing to act on, and
 * stubbing those greyed-out would read as a draft in a bad state rather than
 * one that has not been written. `cursor-wait` and the absent link are what say
 * it is not a target.
 */
export const PENDING_RUN_ROW =
  "cursor-wait border-b border-l-[3px] border-rule-soft py-[18px] pl-[15px] pr-0.5";

/**
 * A spinner, against this codebase's general preference for skeletons.
 *
 * The feed uses skeletons because a spinner collapses a layout it is standing
 * in for. Nothing is being stood in for here: generation takes minutes, the row
 * is reporting that work is under way elsewhere, and a pulsing grey block would
 * read as a draft that failed to load. `motion-reduce` slows the rotation
 * rather than stopping it — a frozen spinner reads as a hung job, which is the
 * one thing this row must not say by accident.
 */
export const PENDING_RUN_SPINNER =
  "shrink-0 animate-spin text-accent motion-reduce:[animation-duration:2.4s]";

/**
 * Deliberately the same treatment as the verdict wording it will be replaced
 * by — the row swaps for a real one in place, and a kicker that changed size or
 * weight at that moment would read as the list reflowing rather than resolving.
 */
export const PENDING_RUN_KICKER = QUEUE_VERDICT_WORDING;

/** The topic, at row weight — it is what the reviewer typed, not a headline. */
export const PENDING_RUN_TOPIC =
  "mt-[9px] text-pretty font-heading text-row font-semibold text-ink-2";
export const PENDING_RUN_NOTE = "mt-1 font-body text-meta text-ink-3";

/* ── queue a new draft ──────────────────────────────────────────────────── */

export const NEW_RUN_FORM = "mt-5 border border-rule-soft bg-surface p-4";
export const NEW_RUN_ROW = "flex flex-wrap gap-2";
export const NEW_RUN_TOPIC_FIELD = `${FIELD} flex-1 basis-64`;
export const NEW_RUN_BLURB_FIELD = `${FIELD} mt-2 resize-y`;
export const NEW_RUN_CONFIRMATION = "mt-2 font-body text-micro text-ink-3";
export const NEW_RUN_ERROR = "mt-2 font-body text-micro text-accent-ink";

/* ── the review screen ──────────────────────────────────────────────────── */

export const REVIEW_PAGE = "mx-auto max-w-page px-gutter pb-24";
export const REVIEW_TOP_BAR =
  "flex flex-wrap items-center justify-between gap-3.5 pt-[22px]";
export const BACK_TO_QUEUE =
  "font-body text-micro font-semibold uppercase leading-none tracking-[0.11em] text-ink-3 hover:text-ink";

/**
 * The right slot of the top bar. Deliberately not `PRIMARY`.
 *
 * Solid accent is the only fill in the console and `controls.ts` reserves it
 * for the one button that publishes. A filled button up here would compete
 * with Approve from the place a reviewer's hand rests *before* they have read
 * anything — which is the same argument that keeps Approve itself out of this
 * bar. A hairline box in the queue's own micro-caps: visible, pressable,
 * unmistakably not the decision.
 */
export const TOP_BAR_ACTION =
  "whitespace-nowrap border border-rule px-2.5 py-1.5 font-body text-micro font-semibold uppercase leading-none tracking-[0.11em] text-ink-2 transition-colors hover:border-ink hover:text-ink disabled:cursor-not-allowed disabled:opacity-45";

/**
 * Editor left, evidence right. Side by side is the requirement — the reviewer
 * cross-checks one against the other continuously — and the page scrolls with
 * the evidence column pinned rather than two panes scrolling independently, so
 * the wheel is never trapped in whichever pane the cursor is over.
 */
export const REVIEW_COLUMNS =
  "mt-4 grid grid-cols-1 items-start gap-12 lg:grid-cols-[minmax(0,1fr)_330px]";
export const REVIEW_BODY_COLUMN = "min-w-0";
export const REVIEW_SIDEBAR = "min-w-0 lg:sticky lg:top-[86px]";

export const DRAFT_STATUS_ROW =
  "flex flex-wrap items-center gap-2.5 border-t-2 border-rule pt-3.5";
export const STATUS_DIVIDER = "h-3 w-px bg-rule-soft";
export const DRAFT_VERDICT_WORDING =
  "font-body text-label font-semibold uppercase leading-none tracking-[0.09em] text-ink-2";
export const DRAFT_META = "font-body text-meta text-ink-3";

/**
 * Read-only: the headline is not part of the autosave contract.
 *
 * `font-ui` because it is an article title, and a reviewer should be reading it
 * in the face it publishes in — see `styles/youth.css`.
 */
export const DRAFT_HEADLINE =
  "mt-[18px] border-b-2 border-rule-soft pb-3 font-ui text-subhead font-extrabold text-ink";
export const EDITOR_SLOT = "mt-[18px]";

export const SIDEBAR_SOURCES = "mt-6";

/**
 * The decision controls sit at the bottom of the evidence column, under what
 * they follow from. Putting Approve in the top bar — the reflex — places it
 * where a reviewer's hand rests *before* they have read anything.
 */
export const DECISION_BLOCK = "mt-6 border-t-2 border-rule pt-[13px]";
export const APPROVE_BUTTON = `${PRIMARY} mt-3 w-full`;
export const REJECT_REASON_FIELD = `${FIELD} mt-2.5 py-2.5 text-meta`;
export const REJECT_BUTTON = `${SECONDARY} mt-2 w-full`;
export const REASON_MISSING_ALERT = "mt-2.5 font-body text-micro text-accent-ink";

/** Carries the server's detail verbatim — a 409 names which handles broke. */
export const ACTION_ERROR_ALERT =
  "mt-2.5 border-l-2 border-accent bg-accent-wash px-[11px] py-[9px] font-body text-micro font-semibold leading-[1.45] text-accent-ink";
export const DECISION_NOTE = "mt-[11px] font-body text-micro text-ink-3";
export const DECISION_LINK = "border-b border-accent pb-px font-semibold text-accent-ink";

/* ── classification ─────────────────────────────────────────────────────── */

/**
 * The subject picker.
 *
 * Sits above the decision block rather than inside it: this is metadata a
 * reviewer sets while reading, editable after publication, and grouping it with
 * Approve would imply it is part of the irreversible act.
 */
export const SUBJECT_BLOCK = "mt-6 border-t border-rule-soft pt-[13px]";
export const SUBJECT_ROW = "mt-2.5 flex flex-wrap gap-1.5";
export const SUBJECT_NOTE = "mt-2 font-body text-micro text-ink-3";

export function subjectChip(active: boolean): string {
  return `rounded-full border px-2.5 py-[5px] font-body text-[11px] font-semibold leading-none transition-colors disabled:opacity-50 ${
    active
      ? "border-ink bg-ink text-ground"
      : "border-rule-soft bg-transparent text-ink-2 hover:border-ink"
  }`;
}

/* ── the sources panel ──────────────────────────────────────────────────── */

export const PANEL_BLOCK = "border-t-2 border-rule pt-[13px]";
export const CLAIM_GROUP = "mt-3.5";
export const CLAIM_HEADING = "font-body text-micro font-semibold text-ink-2";
export const PANEL_SOURCE_LIST = "mt-2.5 flex list-none flex-col gap-3 p-0";

/**
 * Two independent signals on one row. `focused` washes the row the reviewer
 * just clicked through to; uncited sources are dimmed but never hidden —
 * what the model left out is the failure mode human review exists to catch.
 */
export function panelSourceRow(focused: boolean, wasCited: boolean): string {
  return `border-l-2 pl-2.5 transition-colors ${
    focused ? "border-l-accent bg-accent-wash" : "border-l-transparent"
  } ${wasCited ? "" : "opacity-45"}`;
}

export const PANEL_SOURCE_HEAD = "flex items-baseline gap-2";
export const PANEL_SOURCE_HANDLE =
  "flex-none font-body text-label font-semibold leading-[1.3] tracking-[0.06em] text-ink-3";
export const PANEL_SOURCE_TITLE_LINK =
  "text-pretty font-body text-meta font-semibold leading-[1.35] text-ink hover:text-accent-ink";

export function panelSourceStudyType(weak: boolean): string {
  return `ml-[26px] mt-[3px] font-body text-micro ${
    weak ? "text-accent-ink" : "text-ink-3"
  }`;
}

export const PANEL_SOURCE_META = "ml-[26px] font-body text-micro text-ink-3";

/**
 * The full-text passages the model was shown. Collapsed: most rows have none,
 * and a reviewer opens them to check a number the abstract does not state.
 */
export const PANEL_EXCERPTS = "ml-[26px] mt-1 font-body text-micro text-ink-3";
export const PANEL_EXCERPTS_SUMMARY =
  "cursor-pointer select-none font-semibold text-ink-2 hover:text-ink";
export const PANEL_EXCERPT = "mt-1.5 border-l border-rule pl-2 leading-normal text-ink-2";
export const PANEL_EXCERPT_SECTION = "font-semibold text-ink-3";

/* ── the validation summary ─────────────────────────────────────────────── */

export const VALIDATION_HEADLINE =
  "mt-2.5 font-heading text-[14px] font-semibold leading-tight text-ink";

/** A failing report is a list of what broke, never a colour. */
export const VALIDATION_FAILURES = "mt-2 flex list-none flex-col gap-1.5 p-0";
export const VALIDATION_FAILURE = "font-body text-micro text-accent-ink";

export const PANEL_GRADE_BAR = "mt-3";
export const PANEL_GRADE_NOTE = "mt-[9px] font-body text-[12px] leading-normal text-ink-3";
export const WEAK_EVIDENCE_WARNING =
  "mt-[11px] border-l-2 border-accent bg-accent-wash px-[11px] py-[9px] font-body text-[12px] font-semibold leading-[1.45] text-accent-ink";

/* ── login ──────────────────────────────────────────────────────────────── */

/**
 * One rounded card on the paper, vertically centred.
 *
 * The card is the You.th panel — same radius and same surface as the source
 * block on an article — because a reviewer arriving here should recognise the
 * product before they recognise the tool. What tells them which one they are on
 * is the accent kicker and the bar above, not a different set of shapes.
 *
 * `min-h` rather than a fixed height so a visible-keyboard viewport on a phone
 * scrolls instead of clipping the submit button.
 */
// 68px is `spacing.header` in tailwind.config.ts — the bar this page sits
// under. Written as a literal because a custom property would be a second
// place to keep the number, and the config is already the first.
export const LOGIN_PAGE =
  "mx-auto flex min-h-[calc(100vh-68px)] max-w-page items-center justify-center px-gutter pb-24 pt-12";
export const LOGIN_COLUMN = "w-full max-w-[420px]";
export const LOGIN_CARD =
  "rounded-panel border border-rule-soft bg-surface px-7 py-8 shadow-panel";
export const LOGIN_KICKER =
  "mb-3.5 font-body text-micro font-bold uppercase tracking-[0.14em] text-accent-ink";
export const LOGIN_TITLE =
  "font-heading text-[28px] font-bold leading-[1.1] tracking-[-0.025em] text-ink";
export const LOGIN_STANDFIRST =
  "mt-3 font-body text-[13.5px] leading-[1.55] text-ink-3";
export const LOGIN_FORM = "mt-6";
export const LOGIN_FIELD = `${FIELD} mt-2`;
export const LOGIN_FIELD_GROUP = "mt-4";

/** Kept inside the card, above the button, so it is read before the retry. */
export const LOGIN_ERROR =
  "mt-4 rounded-field border-l-2 border-accent bg-accent-wash px-3.5 py-2.5 font-body text-[12.5px] leading-normal text-ink";
export const LOGIN_SUBMIT = `${PRIMARY} mt-5 w-full`;

/** The one way back to the public site from here. */
export const LOGIN_FOOTNOTE = "mt-5 text-center font-body text-micro text-ink-4";
export const LOGIN_FOOTNOTE_LINK = "text-ink-3 underline underline-offset-2 hover:text-ink";

export const FIELD_LABEL =
  "block font-body text-kicker font-semibold uppercase tracking-[0.12em] text-ink-3";

/* ── the editor ─────────────────────────────────────────────────────────── */

/**
 * Passed to TipTap as a raw attribute rather than rendered by React, so the
 * arbitrary-variant selectors are how paragraph spacing gets set at all.
 *
 * The `.ProseMirror-selectednode` rule is what makes an image or a video block
 * feel like an object. They are atoms: a click selects the whole thing and
 * Backspace removes it — but only if the reviewer can see that it is selected,
 * and a block with no visible selection state reads as an un-deletable
 * fixture.
 *
 * `flow-root` makes the surface contain its floats. Without it a picture
 * floated beside the last beat hangs out of the bottom of the editing panel
 * and over whatever follows it.
 *
 * The `h2` rule mirrors `PROSE_HEADING` on the public side — same face, same
 * weight, same asymmetric margin. This editor is at a narrower measure than the
 * article, so the two cannot be identical, but a section heading has to *read*
 * as one here or the reviewer is judging a structure they cannot see. Same
 * argument `mediaWrapClass()` makes for pictures: the reviewer approves what
 * publishes, layout included.
 */
export const EDITOR_PROSE =
  "flow-root font-body text-[15px] leading-[1.72] text-ink focus:outline-none [&_p]:mb-4 [&_p:last-child]:mb-0 [&_h2]:mb-2 [&_h2]:mt-7 [&_h2]:font-ui [&_h2]:text-[17px] [&_h2]:font-bold [&_h2]:leading-[1.25] [&_h2]:tracking-[-0.02em] [&_h2:first-child]:mt-0 [&_.ProseMirror-selectednode]:outline [&_.ProseMirror-selectednode]:outline-2 [&_.ProseMirror-selectednode]:outline-offset-2 [&_.ProseMirror-selectednode]:outline-accent";

/** `-mt-px` collapses the toolbar's bottom rule into the surface's top one. */
export const EDITOR_SURFACE = "-mt-px border border-rule-soft bg-surface p-[18px]";
export const EDITOR_STATUS_ROW =
  "mt-3 flex flex-wrap items-baseline justify-between gap-3";
export const EDITOR_NOTE = "font-body text-micro text-ink-3";

export const TOOLBAR =
  "flex flex-wrap items-center gap-0.5 border border-rule-soft bg-surface p-1.5";

export function toolbarButton(active: boolean): string {
  return `px-2.5 py-[7px] font-body text-[12px] font-semibold leading-none transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${
    active ? "bg-ink text-ground" : "text-ink-2 hover:bg-ground hover:text-ink"
  }`;
}

/** Separates the two marks from the two things that insert a block. */
export const TOOLBAR_DIVIDER = "mx-1.5 h-4 w-px bg-rule-soft";

/* ── media in the editor ────────────────────────────────────────────────── */

/**
 * The frame inside a media node view.
 *
 * The *layout* — the float and the width — is not here. TipTap's React
 * renderer wraps a node view in an element of its own, and that element is the
 * one sitting in the editor's flow, so it is the one that has to float. It
 * gets `mediaWrapClass()` from `lib/media.ts` through the node's `attrs`
 * option (see `MediaNodes.ts`), which is the same class the published article
 * uses.
 *
 * What is left for the figure is the part that only exists while editing:
 * `relative` to anchor the control bar and the resize grip, and a named group
 * so the grip can appear on hover.
 */
export const MEDIA_FRAME = "group/media relative block";

/**
 * Unstyled beyond a hairline. The reviewer is checking that the right picture
 * is in the right place, and a treatment the published page does not share
 * would be a lie about what they are approving.
 */
export const EDITOR_IMAGE = "block h-auto w-full border border-rule-soft";

/**
 * The video is a 16:9 still, not a player.
 *
 * The frame matches the shape that publishes — a reviewer sizing a video needs
 * to see the rectangle a reader will get, and the old thumbnail-and-label card
 * was a different shape at a fixed size. A live iframe is still out: inside a
 * contenteditable it swallows every click, so the node could never be selected
 * or deleted, and it would load YouTube's player to tell a reviewer something
 * a still already tells them.
 */
export const EDITOR_VIDEO_FRAME =
  "relative block aspect-video w-full border border-rule-soft bg-ground";
export const EDITOR_VIDEO_STILL = "absolute inset-0 h-full w-full object-cover";

/** Sits over the still so the id stays checkable at any size. */
export const EDITOR_VIDEO_OVERLAY =
  "absolute inset-x-0 bottom-0 flex flex-wrap items-baseline gap-x-2 gap-y-0.5 bg-ink px-2.5 py-1.5";
export const EDITOR_VIDEO_KICKER =
  "font-body text-kicker font-semibold uppercase leading-none tracking-[0.13em] text-ground";
export const EDITOR_VIDEO_ID =
  "truncate font-body text-micro font-semibold leading-none text-ground";

/* ── the controls on a selected picture or video ────────────────────────── */

/**
 * A bar over the selected block, not more buttons in the main toolbar.
 *
 * The controls are where the reviewer's attention already is, and the toolbar
 * stays as short as it was — six permanently-inert buttons would be worse than
 * no buttons. `-top-*` lifts it clear of the frame; it is `absolute` so it
 * never changes the size the reviewer is trying to judge.
 */
export const MEDIA_CONTROLS =
  "absolute -top-[38px] left-0 z-20 flex items-center gap-0.5 whitespace-nowrap border border-rule-soft bg-surface px-1 py-1 shadow-panel";
export const MEDIA_CONTROL_DIVIDER = "mx-1 h-3.5 w-px bg-rule-soft";

export function mediaControlButton(active: boolean): string {
  return `px-2 py-1.5 font-body text-[11px] font-semibold leading-none transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
    active ? "bg-ink text-ground" : "text-ink-2 hover:bg-ground hover:text-ink"
  }`;
}

/** The live percentage. Tabular so the bar does not twitch as it is dragged. */
export const MEDIA_WIDTH_READOUT =
  "min-w-[38px] text-center font-body text-[11px] font-semibold tabular-nums leading-none text-ink-3";

/** Destructive, so it is the accent — the same red as everything that acts. */
export const MEDIA_REMOVE_BUTTON =
  "px-2 py-1.5 font-body text-[11px] font-semibold leading-none text-accent-ink transition-colors hover:bg-accent-wash";

/**
 * The corner grip.
 *
 * Only on hover or selection: a permanent handle on every picture turns the
 * draft into a page of controls. `cursor-nwse-resize` is what tells a reviewer
 * this is a resize rather than a second drag target — dragging the picture
 * itself moves it between paragraphs.
 */
export const MEDIA_RESIZE_HANDLE =
  "absolute -bottom-1.5 -right-1.5 z-20 h-4 w-4 cursor-nwse-resize border border-ground bg-accent opacity-0 transition-opacity group-hover/media:opacity-100 focus-visible:opacity-100";
export const MEDIA_RESIZE_HANDLE_VISIBLE = "opacity-100";

/** A failed upload, next to the toolbar that started it. */
export const MEDIA_ERROR =
  "mt-2 font-body text-micro font-semibold text-accent-ink";

/** A failed save is the one editor state that blocks Approve, so it is bolder. */
export function saveIndicator(failed: boolean): string {
  return `font-body text-micro ${failed ? "font-semibold text-accent-ink" : "text-ink-3"}`;
}

/* ── the feed-tile preview ──────────────────────────────────────────────── */
/*
 * The console's first overlay, and the reason it earns one: the reviewer's
 * question is "how will this look on the feed", and a tile alone in a sidebar
 * cannot answer it. Scale is comparative — a headline that clamps at three
 * lines, a picture that reads at 190px, a verdict kicker that holds its own —
 * and all of that is only legible beside other tiles. So the overlay clears
 * the review screen and rebuilds a few cells of the real feed around it.
 */

/** Full-bleed scrim. The panel scrolls inside it, never the page behind. */
export const PREVIEW_OVERLAY =
  "fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-[rgb(18_16_15/0.62)] p-4 sm:p-8";

export const PREVIEW_PANEL =
  "relative my-auto w-full max-w-[860px] rounded-panel bg-ground p-5 shadow-panel sm:p-7";

export const PREVIEW_HEAD =
  "flex flex-wrap items-baseline justify-between gap-3 border-b-2 border-rule pb-3";
export const PREVIEW_TITLE =
  "font-heading text-[19px] font-bold leading-tight tracking-[-0.015em] text-ink";
export const PREVIEW_CLOSE =
  "font-body text-micro font-semibold uppercase leading-none tracking-[0.11em] text-ink-3 transition-colors hover:text-ink";

/**
 * Redraw beside Close, at the head of the panel.
 *
 * The tile's own regenerate lives here rather than in the editor's toolbar
 * because this is the only screen where its effect is visible, and it wears
 * `TOP_BAR_ACTION` — the same hairline box the review bar's actions use, and
 * pointedly not the accent fill `controls.ts` reserves for Approve. A filled
 * button inside a preview would be the loudest thing on a screen whose whole
 * job is to show the reviewer something quietly.
 */
export const PREVIEW_ACTIONS = "flex items-baseline gap-3";

/**
 * The feed's own measure: `MasonryFeed` runs `columnWidth={190}` at
 * `columnGutter={14}`. Written as literals because the masonry's numbers are
 * the original and a shared constant would be a second place to keep them —
 * but they must agree, since a tile previewed at 260px clamps its headline
 * differently from the one that ships, and that difference is the whole point
 * of looking.
 */
/**
 * `ew-type-feed` is not decoration: the desk is scoped to Archivo at the route
 * level, and these are the real feed tiles, so without it the preview would
 * show a reviewer their card in a face the feed never uses. See the type scopes
 * in `styles/youth.css`.
 */
export const PREVIEW_GRID =
  "ew-type-feed mt-4 grid grid-cols-[repeat(auto-fill,190px)] justify-center gap-[14px]";

/**
 * The reviewer's own tile, marked so it is findable among the neighbours.
 *
 * Outlined, never filled — the same call `WEAK_EVIDENCE_FLAG` makes. Solid
 * accent is reserved for the button that publishes, and while a label cannot
 * literally be clicked instead of Approve, spending the console's one fill on
 * decoration is how that reservation stops meaning anything.
 */
export const PREVIEW_MINE = "relative outline outline-2 outline-offset-[3px] outline-accent";
// Back to the desk's face inside the feed-typed grid: the tag is console
// furniture pointing at the tile, not part of the tile.
export const PREVIEW_MINE_TAG =
  "ew-type-console absolute -top-[9px] left-2 z-10 border border-accent bg-ground px-1.5 py-0.5 font-body text-[8.5px] font-bold uppercase leading-none tracking-[0.11em] text-accent-ink";

export const PREVIEW_NOTE =
  "mt-4 border-t border-rule-soft pt-3 font-body text-micro leading-[1.55] text-ink-3";
export const PREVIEW_MESSAGE = "mt-4 font-body text-micro text-ink-3";


/* --- trend candidates ---------------------------------------------------- */
/*
 * The desk half of the fortnightly scan. Shares the New-run panel's frame on
 * purpose: both are "start a run", and the ranked list is the evidence-backed
 * version of the free-text box beneath it.
 */

export const CANDIDATES_PANEL = "mt-5 border border-rule-soft bg-surface";

/**
 * The header holds two controls, so it is a row rather than one big button.
 *
 * It was a single full-width toggle until Run scan joined it, and a button
 * inside a button is not a thing HTML has — the disclosure had to give up the
 * whole row to gain a sibling.
 */
export const CANDIDATES_HEADER = "flex w-full items-center gap-2 px-4 py-3";

/**
 * The disclosure: an arrow and the title, and no surface of its own.
 *
 * No fill and no hover wash on purpose — this panel already sits on `surface`
 * inside `ground`, and a third tone on the header would read as a nested card.
 * The arrow rotating is the whole affordance, which is enough because the state
 * it reports is visible directly beneath it.
 */
export const CANDIDATES_TOGGLE =
  "-m-1 flex min-w-0 items-center gap-2 rounded-field p-1 text-left";

export const CANDIDATES_TITLE =
  "font-body text-label-sm font-semibold uppercase tracking-[0.08em] text-ink-2";

/** Points right when closed, down when open. Transform, so it turns. */
export function candidatesChevron(open: boolean): string {
  return `size-4 shrink-0 text-ink-3 transition-transform ${open ? "rotate-90" : ""}`;
}

/**
 * The count, as a mark rather than a sentence.
 *
 * Accent fill — the console's one fill, otherwise reserved for the button that
 * publishes — because this is the only other thing on the desk asking to be
 * noticed from across the room. It is rendered *only* when there is something
 * to count: a circled 0 is a badge saying nothing is here, which is worse than
 * no badge at all.
 */
export const CANDIDATES_BADGE =
  "inline-flex size-[22px] shrink-0 items-center justify-center rounded-full bg-accent px-1 font-body text-[11px] font-bold leading-none text-white";

export const CANDIDATES_COUNT = "font-body text-micro text-ink-3";

/**
 * Run the scan now. Deliberately the quietest control in the panel: it costs
 * nothing generative, but it is minutes of somebody else's API budget, and it
 * must not compete with Generate draft for a reviewer's attention.
 */
export const CANDIDATES_SCAN =
  "ml-auto inline-flex shrink-0 items-center gap-1.5 rounded-full border border-rule px-3 py-1.5 font-body text-[11px] font-bold uppercase leading-none tracking-[0.08em] text-ink-3 transition-colors hover:border-ink hover:text-ink disabled:cursor-not-allowed disabled:opacity-45";

/** What the scan is doing or found. Sits under the header, above the list. */
export const CANDIDATES_SCAN_NOTE =
  "border-t border-rule-soft px-4 py-2 font-body text-micro text-ink-3";

/**
 * The trends the scan held back, under the note that counts them.
 *
 * Quieter than a candidate row and never interactive: there is nothing to do
 * about these, and the whole point is that they explain a *short* list rather
 * than competing with it. `ink-4` and the micro size put them a step below the
 * scan note itself, which is already the panel's quietest text.
 */
export const CANDIDATES_SUPPRESSED =
  "border-t border-rule-soft px-4 pb-2 pt-1 font-body text-micro text-ink-4";

export const CANDIDATES_SUPPRESSED_ROW = "flex flex-wrap items-baseline gap-x-2 py-0.5";

export const CANDIDATES_SUPPRESSED_TOPIC = "text-ink-3";

export const CANDIDATES_SUPPRESSED_WHY = "text-ink-4";

export const CANDIDATES_LIST = "border-t border-rule-soft";

export const CANDIDATE_ROW =
  "flex flex-wrap items-start justify-between gap-3 border-b border-rule-soft px-4 py-3 last:border-b-0";

export const CANDIDATE_MAIN = "min-w-0 flex-1 basis-64";

export const CANDIDATE_TOPIC = "font-body text-[14px] leading-tight text-ink";

/** The arithmetic, in words. See the component for why it is not a bare score. */
export const CANDIDATE_EVIDENCE = "mt-1 font-body text-micro text-ink-3";

export const CANDIDATE_MIX = "mt-0.5 font-body text-micro text-ink-4";

export const CANDIDATE_ACTIONS = "flex shrink-0 items-center gap-2";

export const CANDIDATE_DISMISS_ROW = "mt-2 flex w-full flex-wrap gap-2";

export const CANDIDATE_DISMISS_FIELD = `${FIELD} flex-1 basis-56`;

export const CANDIDATES_EMPTY = "px-4 py-3 font-body text-micro text-ink-3";

export const CANDIDATES_ERROR = "px-4 py-3 font-body text-micro text-accent-ink";

/*
 * The research agent's panel. The same frame, header and rows as the trend
 * candidates panel above — both are "topics proposed for a draft" — plus an
 * options row and a quieter list of what was considered and turned away.
 */

/** The run options, shown when the panel is open. */
export const RESEARCH_OPTIONS =
  "flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-rule-soft px-4 py-2 font-body text-micro text-ink-3";

export const RESEARCH_OPTION = "inline-flex items-center gap-1.5";

export const RESEARCH_SELECT = `${FIELD} w-auto py-1 text-micro`;

export const RESEARCH_CATEGORY = "inline-flex items-center gap-1 text-ink-3";

/** "Also considered" — a label, not a control, above the discarded list. */
export const RESEARCH_SUBHEAD =
  "border-t border-rule-soft px-4 pb-1 pt-3 font-body text-[11px] font-bold uppercase tracking-[0.08em] text-ink-4";

/** The score, spelled out by component, one step quieter than the evidence line. */
export const RESEARCH_SCORE = "mt-0.5 font-body text-micro text-ink-4";

/** Where the reviewer should look first: the reader's question, in the topic's voice. */
export const RESEARCH_QUESTION = "mt-0.5 font-body text-micro italic text-ink-3";

/** "Draft queued" in place of the buttons once a topic is promoted. */
export const RESEARCH_DONE = "font-body text-micro text-ink-3";

/** Questions people asked about a topic, quoted as collected. */
export const RESEARCH_QUESTIONS = "mt-0.5 font-body text-micro text-ink-3";

/**
 * What a trend rests on: a few of the collected articles, posts and videos.
 * Links, so a reviewer can check the trend before spending a draft on it.
 */
export const RESEARCH_SOURCES = "mt-1 space-y-0.5 font-body text-micro text-ink-4";

export const RESEARCH_SOURCE_LINK =
  "text-ink-3 underline decoration-rule underline-offset-2 hover:text-ink";

