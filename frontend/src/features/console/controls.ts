/**
 * The console's three control styles, in one place.
 *
 * Small enough to inline, and that is exactly why they are not: the login form,
 * the queue and the review screen all render an approve-shaped button and a
 * reject-shaped button, and when those drift the console stops teaching which
 * action is which. Consistency here is a safety property, not tidiness - this
 * is the surface where one button publishes.
 *
 * They carry the You.th shapes - pill actions, rounded fields - for the same
 * reason. The desk is a different surface from the public site and says so
 * through its chrome, not by being built out of different parts: a reviewer who
 * has to relearn what a button looks like when they cross between the two is
 * being asked to pay attention to the wrong thing.
 */

/**
 * A text field. Inputs sit on the *ground* inside a surface panel - inverted
 * from the usual, so the editable region is the recess rather than the raised
 * thing. That inversion was load-bearing when the system had no radius; it is
 * kept now that it does, because a recessed field still reads as "yours to
 * change" faster than a rounded one on its own does.
 */
export const FIELD =
  "w-full rounded-field border border-rule bg-ground px-3.5 py-[11px] font-body text-[14px] leading-tight text-ink outline-none placeholder:text-ink-4 focus:border-ink-3";

/**
 * The primary action. Flush-left label, per the design system: a button wider
 * than its text starts the text at the left padding edge, never centred.
 *
 * Solid accent is reserved for it. Red is the interaction colour and it is the
 * only fill in the console, so the one button that publishes is unmistakable.
 */
export const PRIMARY =
  "rounded-full border-0 bg-accent px-5 py-[13px] text-left font-body text-[12px] font-bold uppercase leading-none tracking-[0.1em] text-white transition-colors hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-45";

/** The paired secondary or destructive action. Outlined, never filled. */
export const SECONDARY =
  "rounded-full border border-rule bg-transparent px-5 py-3 text-left font-body text-[12px] font-bold uppercase leading-none tracking-[0.1em] text-ink-2 transition-colors hover:border-ink hover:text-ink disabled:cursor-not-allowed disabled:opacity-45";

/** A small caps label above a field or a sidebar section. */
export const SECTION_LABEL =
  "block font-body text-label-sm font-semibold uppercase text-ink-3";
