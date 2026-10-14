/**
 * Accessibility statement (הצהרת נגישות).
 *
 * Required by reg. 35ה of Israel's Equal Rights for Persons with Disabilities
 * (Service Accessibility Adjustments) Regulations 2013: what was made
 * accessible, who the accessibility coordinator is, and how to report a
 * barrier. Exempt operators (turnover ≤ NIS 100,000 for a site launched after
 * 2017) must still publish a way to get the service accessibly.
 *
 * **It claims no conformance level, on purpose.** Everything listed under
 * "what we have done" was checked on 2026-10-13 and is recorded in
 * COMPLIANCE_IMPLEMENTATION.md — an automated axe-core scan of every public
 * page in both themes, plus keyboard checks. That is not an IS 5568 audit: no
 * screen reader was used and many success criteria need a person to judge.
 * Saying "conforms to IS 5568 at AA" before an auditor has said so would be a
 * false statement on exactly the page people with disabilities rely on. When
 * the audit is done, replace the placeholder with its result.
 */

import { Link } from "react-router-dom";

import {
  LEGAL_H2,
  LEGAL_LIST,
  LEGAL_P,
  LEGAL_PAGE,
  LEGAL_UPDATED,
  PAGE_BACK_LINK,
  PAGE_KICKER,
  PAGE_TITLE,
} from "@/features/shell/pageStyles";
import { DraftNote, Fill } from "./privacy";

export function AccessibilityRoute() {
  return (
    <main className={LEGAL_PAGE}>
      <Link to="/" className={PAGE_BACK_LINK}>
        ← Feed
      </Link>
      <div className={PAGE_KICKER}>Accessibility</div>
      <h1 className={PAGE_TITLE}>Accessibility statement</h1>
      <p className={LEGAL_UPDATED}>
        Last updated: <Fill>date of publication</Fill>
      </p>

      <DraftNote />

      <p className={LEGAL_P}>
        We want everyone to be able to read You.th, including people who use a
        screen reader, a keyboard instead of a mouse, magnification, or reduced
        motion. We are working towards the Israeli Standard IS 5568 (based on the
        W3C&rsquo;s WCAG guidelines) at level AA, as Israel&rsquo;s Service
        Accessibility Regulations require.
      </p>

      <h2 className={LEGAL_H2}>Where we stand</h2>
      <p className={LEGAL_P}>
        <Fill>
          Result of the accessibility audit: who carried it out, when, the level
          reached, and any parts that do not yet conform — or, until then: &ldquo;The
          site has not yet been audited by an accessibility auditor.&rdquo;
        </Fill>
      </p>

      <h2 className={LEGAL_H2}>What we have done</h2>
      <ul className={LEGAL_LIST}>
        <li>
          Text and controls meet the WCAG AA contrast ratio in both the light and
          dark themes.
        </li>
        <li>
          Every page can be used with a keyboard alone. A &ldquo;Skip to
          content&rdquo; link is the first thing you reach, focused elements show
          a clear outline, and the categories menu keeps focus inside it until you
          close it with Escape.
        </li>
        <li>
          The video on the home page has a pause button, and does not play if your
          device is set to reduce motion.
        </li>
        <li>
          Form fields have visible labels, the password requirements are linked
          to the password field and say which ones are met, and error messages
          are announced to screen readers.
        </li>
        <li>
          Pages use headings, landmarks and lists so they can be navigated with a
          screen reader; images in articles carry text descriptions.
        </li>
        <li>
          Videos embedded in articles load only when you press play, and can be
          used with the YouTube player&rsquo;s own keyboard controls.
        </li>
        <li>
          We do not use an accessibility &ldquo;overlay&rdquo; widget; the
          adjustments are in the site itself.
        </li>
      </ul>

      <h2 className={LEGAL_H2}>Tested with</h2>
      <p className={LEGAL_P}>
        <Fill>browsers, devices and assistive technologies used in the audit</Fill>
      </p>

      <h2 className={LEGAL_H2}>Known limitations</h2>
      <ul className={LEGAL_LIST}>
        <li>
          Linked research papers are published by other organisations, and we
          cannot make their sites accessible.
        </li>
        <li>
          The text over the home-page video sits on moving footage, so its contrast
          changes from frame to frame. The same words are not needed to use the
          site.
        </li>
        <li>
          <Fill>anything else the audit finds</Fill>
        </li>
      </ul>

      <h2 className={LEGAL_H2}>Report a barrier or ask for help</h2>
      <p className={LEGAL_P}>
        If something on the site is hard to use, tell us and we will help you get the
        information another way and work on fixing it. Our accessibility
        coordinator is <Fill>name</Fill>, reachable at{" "}
        <Fill>accessibility email</Fill> or <Fill>phone</Fill>. You can also use{" "}
        <Link to="/contact" className="underline">
          the contact form
        </Link>
        . Please tell us the page, what you were trying to do, and the browser or
        assistive technology you use. We aim to reply within{" "}
        <Fill>response time</Fill>.
      </p>
    </main>
  );
}
