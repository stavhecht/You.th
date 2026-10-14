/**
 * About.
 *
 * The copy is the comp's, and the three numbered rules at the bottom are not
 * marketing — each one names a mechanism that exists in the code and can be
 * checked:
 *
 *   1. every published article carries its sources (`article_sources`, and the
 *      citation validation that runs before persistence)
 *   2. evidence grade and quantity cap verdict confidence
 *      (`evidence/grading.py`, invariant #3)
 *   3. `ReviewService.approve()` is the only assignment of `published` in the
 *      system, and a test greps the AST to keep it that way (invariant #1)
 *
 * If any of those stops being true, this page becomes a false claim rather than
 * a stale one — which is the reason for spelling out which is which here.
 */

import { Link } from "react-router-dom";

import {
  ABOUT_CALLOUT,
  ABOUT_LEAD,
  ABOUT_PAGE,
  ABOUT_RULE,
  ABOUT_RULE_BODY,
  ABOUT_RULE_NUMBER,
  ABOUT_RULES,
  ABOUT_TITLE,
  PAGE_BACK_LINK,
} from "@/features/shell/pageStyles";

const RULES = [
  "A claim is only published with the papers it rests on attached.",
  "The strength of the evidence caps how confidently we can word it.",
  "Nothing reaches you without a person reading it first.",
];

export function AboutRoute() {
  return (
    <main className={ABOUT_PAGE}>
      <Link to="/" className={PAGE_BACK_LINK}>
        ← Feed
      </Link>

      <h1 className={ABOUT_TITLE}>Real research, made readable.</h1>
      <div className={ABOUT_RULE} />

      <p className={ABOUT_LEAD}>
        You.th was built out of a real need to stop misinformation on social
        media, and to become a place where medical, health and aesthetic
        information can be passed on honestly. Everything here is based on
        published research.
      </p>
      <p className={ABOUT_LEAD}>
        Our team is driven by an interest in health and lifestyle, and believes
        everyone deserves access to real information. In a fast-moving,
        information-heavy generation, it matters that it arrives in a form you
        can actually use.
      </p>

      <p className={ABOUT_CALLOUT}>
        The information presented here is advice only and does not replace a
        doctor's recommendation.
      </p>

      <div className={ABOUT_RULES}>
        {RULES.map((rule, index) => (
          <div key={rule}>
            <div className={ABOUT_RULE_NUMBER}>{index + 1}</div>
            <div className={ABOUT_RULE_BODY}>{rule}</div>
          </div>
        ))}
      </div>
    </main>
  );
}
