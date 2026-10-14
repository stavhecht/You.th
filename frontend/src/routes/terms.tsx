/**
 * Terms of use.
 *
 * Written for what the service actually is: free reading, a free optional
 * account, a contact form. There are no payments, subscriptions or user
 * publishing anywhere in the code, so there are no clauses for them — add them
 * when the product does, not before. Nothing here tries to exclude a right the
 * law gives a consumer; the liability section says so explicitly, because a
 * clause that purports to waive a mandatory right is unenforceable and, in some
 * jurisdictions, itself unlawful.
 *
 * Placeholders are business facts; see COMPLIANCE_BUSINESS_INPUTS.md.
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

export function TermsRoute() {
  return (
    <main className={LEGAL_PAGE}>
      <Link to="/" className={PAGE_BACK_LINK}>
        ← Feed
      </Link>
      <div className={PAGE_KICKER}>Terms</div>
      <h1 className={PAGE_TITLE}>Terms of use</h1>
      <p className={LEGAL_UPDATED}>
        Last updated: <Fill>date of publication</Fill>
      </p>

      <DraftNote />

      <h2 className={LEGAL_H2}>Who you are dealing with</h2>
      <p className={LEGAL_P}>
        You.th is operated by <Fill>legal name of the operator</Fill>,{" "}
        <Fill>postal address</Fill>, <Fill>contact email</Fill>. By using the site
        you agree to these terms. If you do not agree, please do not use it.
      </p>

      <h2 className={LEGAL_H2}>Not medical advice</h2>
      <p className={LEGAL_P}>
        Articles summarise published research about health, nutrition, fitness,
        skincare and similar subjects. They are general information, not medical
        advice, diagnosis or treatment, and they are not a substitute for a doctor
        or other qualified professional who knows your circumstances. Do not start,
        stop or change a treatment, medicine or supplement because of something you
        read here without asking one. In an emergency, contact your local emergency
        services.
      </p>
      <p className={LEGAL_P}>
        Research changes, and a study can be corrected or withdrawn after we cite
        it. We check cited papers for retractions and mark affected articles, but we
        cannot promise every article is complete or current.
      </p>

      <h2 className={LEGAL_H2}>How articles are made</h2>
      <p className={LEGAL_P}>
        Articles are drafted with the help of AI models from the scientific studies
        they cite, checked automatically against those studies, and read and
        approved by a member of our editorial team before publication.{" "}
        <Fill>name of the person or entity holding editorial responsibility</Fill>{" "}
        is responsible for what we publish. If you think an article gets something
        wrong, <Link to="/contact" className="underline">tell us</Link>.
      </p>

      <h2 className={LEGAL_H2}>Your account</h2>
      <ul className={LEGAL_LIST}>
        <li>An account is free and optional. Everything on the site can be read without one.</li>
        <li>
          You must be <Fill>minimum age</Fill> or older to create one, and the
          details you give must be your own.
        </li>
        <li>Keep your password to yourself. You are responsible for what is done with your account.</li>
        <li>
          You can delete your account at any time from{" "}
          <Link to="/you" className="underline">your profile</Link>.
        </li>
        <li>
          We may suspend or close an account that is used to break these terms or the
          law. Where we can, we will tell you why first.
        </li>
      </ul>

      <h2 className={LEGAL_H2}>Acceptable use</h2>
      <p className={LEGAL_P}>Please do not:</p>
      <ul className={LEGAL_LIST}>
        <li>try to get into accounts or parts of the service that are not yours;</li>
        <li>interfere with the service, or overload it with automated requests;</li>
        <li>use the contact form to send spam, abuse, or anything unlawful;</li>
        <li>
          copy the site&rsquo;s content in bulk, or republish it as your own.
          <Fill>state any permitted reuse or licence, if one is intended</Fill>
        </li>
      </ul>

      <h2 className={LEGAL_H2}>Content and intellectual property</h2>
      <p className={LEGAL_P}>
        The site&rsquo;s articles, design and code belong to the operator or its
        licensors. Titles and abstracts of cited studies belong to their publishers
        and authors; we link to the original papers. Linking to our articles is
        welcome.
      </p>
      <p className={LEGAL_P}>
        When you send us a suggestion through &ldquo;Let us know&rdquo;, you allow us
        to use the idea to decide what to write about. We will not publish your name
        or message without asking you.
      </p>

      <h2 className={LEGAL_H2}>Third-party content</h2>
      <p className={LEGAL_P}>
        Articles link to research papers and may include YouTube videos. Those are
        provided by others under their own terms, and we are not responsible for
        them.
      </p>

      <h2 className={LEGAL_H2}>Liability</h2>
      <p className={LEGAL_P}>
        We work to make articles accurate, but the service is provided as it is and
        may sometimes be unavailable. To the extent the law allows, we are not liable
        for decisions you make based on general information on the site.{" "}
        <strong>
          Nothing in these terms limits any right you have, or any liability we
          have, that cannot be limited by law
        </strong>{" "}
        &mdash; including under the consumer-protection law of the country where you
        live.
      </p>

      <h2 className={LEGAL_H2}>Privacy</h2>
      <p className={LEGAL_P}>
        How we handle your information is described in our{" "}
        <Link to="/privacy" className="underline">privacy policy</Link>.
      </p>

      <h2 className={LEGAL_H2}>Changes and governing law</h2>
      <p className={LEGAL_P}>
        We may update these terms; the date at the top shows when. We will give
        account holders notice of a material change before it applies.
      </p>
      <p className={LEGAL_P}>
        These terms are governed by{" "}
        <Fill>governing law, e.g. the laws of the State of Israel</Fill>, and
        disputes go to <Fill>competent courts</Fill>, without taking away any
        protection the law of your country of residence gives you as a consumer.
      </p>
    </main>
  );
}
