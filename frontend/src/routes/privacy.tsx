/**
 * Privacy policy.
 *
 * Every factual sentence here describes code that exists, and the comment
 * beside it names where. That is the same rule as `about.tsx`: if one of these
 * mechanisms changes, this page becomes a false statement rather than a stale
 * one, so whoever changes the mechanism changes the sentence.
 *
 * What the code cannot know — who the controller is, where the servers are,
 * how long logs and contact messages are kept — is a `<Fill>` placeholder,
 * rendered loudly, and listed in COMPLIANCE_BUSINESS_INPUTS.md. The page is a
 * draft until those are filled and a lawyer has read it; the banner at the top
 * says so to anyone who lands here first.
 */

import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import {
  LEGAL_DRAFT_NOTE,
  LEGAL_H2,
  LEGAL_LIST,
  LEGAL_P,
  LEGAL_PAGE,
  LEGAL_PLACEHOLDER,
  LEGAL_TABLE,
  LEGAL_TD,
  LEGAL_TH,
  LEGAL_UPDATED,
  PAGE_BACK_LINK,
  PAGE_KICKER,
  PAGE_TITLE,
} from "@/features/shell/pageStyles";

/** A fact only the operator can supply. */
export function Fill({ children }: { children: ReactNode }) {
  return <span className={LEGAL_PLACEHOLDER}>[{children}]</span>;
}

/** Shown on both legal pages until every placeholder is filled. */
export function DraftNote() {
  return (
    <p className={LEGAL_DRAFT_NOTE} role="note">
      Draft. Text in brackets is still to be confirmed by the operator, and this
      page has not yet been reviewed by a lawyer.
    </p>
  );
}

const DATA_TABLE: { what: string; why: string; basis: string; kept: ReactNode }[] = [
  {
    // readers.email, password_hash (Argon2id), display_name
    what: "Your email address, first name and password (we store only a one-way hash of the password, never the password itself)",
    why: "To create your account and sign you in",
    basis: "Providing the account you asked for",
    kept: "Until you delete your account",
  },
  {
    // readers.interests, reader_folders, reader_saves
    what: "The subjects you pick, your folders and the articles you save",
    why: "To put your subjects first in your feed and keep your shelf across devices",
    basis: "Providing the account you asked for",
    kept: "Until you remove them or delete your account",
  },
  {
    // readers.newsletter — a stored choice; nothing sends email yet
    what: "Whether you asked for the newsletter",
    why: "To record your choice. We do not send a newsletter yet; if we start, we will only send it to people who asked",
    basis: "Your consent, which you can withdraw on your profile at any time",
    kept: "Until you switch it off or delete your account",
  },
  {
    // contact_requests: email, name?, link?, note
    what: "What you send through “Let us know”: your email, optional name, optional link and your message",
    why: "To read your request and reply to it",
    basis: "Responding to your request; our legitimate interest in running the publication",
    kept: <Fill>retention period for contact messages</Fill>,
  },
  {
    // security/login_throttle.py — in memory only
    what: "Your IP address and the email address used when you sign in, confirm deleting your account, or send a message",
    why: "To block password guessing and spam",
    basis: "Our legitimate interest in keeping accounts secure",
    kept: "In server memory only, never written to our database; it stops having any effect within about an hour and is lost when the server restarts",
  },
  {
    // uvicorn access log
    what: "Server logs: IP address, time, and the address of the page or file requested",
    why: "To run the service, fix faults and investigate abuse",
    basis: "Our legitimate interest in operating a secure service",
    kept: <Fill>log retention period</Fill>,
  },
];

export function PrivacyRoute() {
  return (
    <main className={LEGAL_PAGE}>
      <Link to="/" className={PAGE_BACK_LINK}>
        ← Feed
      </Link>
      <div className={PAGE_KICKER}>Privacy</div>
      <h1 className={PAGE_TITLE}>How we handle your information</h1>
      <p className={LEGAL_UPDATED}>
        Last updated: <Fill>date of publication</Fill>
      </p>

      <DraftNote />

      <h2 className={LEGAL_H2}>Who we are</h2>
      <p className={LEGAL_P}>
        You.th is run by <Fill>legal name of the operator</Fill>,{" "}
        <Fill>legal form and registration number</Fill>, of{" "}
        <Fill>postal address</Fill>. We decide how the information described here
        is used (in privacy-law terms, we are its controller and the owner of the
        database). Write to us about anything on this page at{" "}
        <Fill>privacy contact email</Fill>.
      </p>

      <h2 className={LEGAL_H2}>You can read everything without an account</h2>
      <p className={LEGAL_P}>
        The feed and every article are public. You only give us information if you
        create an account or send us a message. You are not legally required to give
        us any of it; without it we simply cannot run an account for you or reply
        to you.
      </p>

      <h2 className={LEGAL_H2}>What we collect, why, and for how long</h2>
      <div className="overflow-x-auto">
        <table className={LEGAL_TABLE}>
          <thead>
            <tr>
              <th scope="col" className={LEGAL_TH}>Information</th>
              <th scope="col" className={LEGAL_TH}>Why</th>
              <th scope="col" className={LEGAL_TH}>Legal basis</th>
              <th scope="col" className={LEGAL_TH}>Kept for</th>
            </tr>
          </thead>
          <tbody>
            {DATA_TABLE.map((row) => (
              <tr key={row.what}>
                <td className={LEGAL_TD}>{row.what}</td>
                <td className={LEGAL_TD}>{row.why}</td>
                <td className={LEGAL_TD}>{row.basis}</td>
                <td className={LEGAL_TD}>{row.kept}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2 className={LEGAL_H2}>What we do not do</h2>
      <ul className={LEGAL_LIST}>
        <li>We do not use analytics, advertising or tracking tools, and we set no cookies.</li>
        <li>We do not sell or rent your information, or share it for advertising.</li>
        <li>
          We do not build a profile of you. The only personalisation is putting the
          subjects you picked first; nothing is hidden from you because of them.
        </li>
      </ul>

      {/* No consent banner, deliberately: the site sets no cookies, and both
          storage items are strictly necessary for something the reader asked
          for (ePrivacy Art 5(3) exemption; the PPA's position is notice for
          essential storage, opt-in for analytics/advertising). Adding any
          analytics, pixel or third-party embed that stores an identifier
          changes that — see COMPLIANCE_AUDIT.md §4.2. */}
      <h2 className={LEGAL_H2}>Cookies and browser storage</h2>
      <p className={LEGAL_P}>
        You.th sets no cookies. We keep two small items in your browser&rsquo;s
        local storage instead. Neither is used to track you, and both are needed for
        something you asked for, so we do not ask for consent to them:
      </p>
      <ul className={LEGAL_LIST}>
        <li>
          <code>youth.reader.token</code> &mdash; keeps you signed in for up to 30
          days. Removed when you sign out.
        </li>
        <li>
          <code>youth.theme</code> &mdash; remembers whether you chose the light or
          dark theme.
        </li>
      </ul>
      <p className={LEGAL_P}>
        If you press play on a YouTube video inside an article, YouTube may then set
        its own cookies; see below. You can clear local storage at any time in your
        browser&rsquo;s settings.
      </p>

      <h2 className={LEGAL_H2}>Articles, AI, and your information</h2>
      <p className={LEGAL_P}>
        Articles are drafted with the help of AI models from published scientific
        studies, checked automatically against those studies, and then read and
        approved by a member of our editorial team before they are published. Some
        illustrations may be generated by an AI image model.
      </p>
      <p className={LEGAL_P}>
        Your account details, saved articles and messages are not used to write
        articles and are not sent to any AI model provider.
      </p>

      <h2 className={LEGAL_H2}>Who else receives information</h2>
      <ul className={LEGAL_LIST}>
        <li>
          Our hosting provider, <Fill>hosting provider and server location</Fill>,
          which stores the database and runs the service on our behalf.
        </li>
        <li>
          <strong>YouTube (Google).</strong> Some articles include a video. On those
          pages your browser loads the video&rsquo;s still image from Google&rsquo;s
          server <code>i.ytimg.com</code>, which receives your IP address. The video
          player itself, from <code>youtube-nocookie.com</code>, loads only when you
          press play; from then on{" "}
          <a
            href="https://policies.google.com/privacy"
            className="underline"
            rel="noreferrer"
            target="_blank"
          >
            Google&rsquo;s privacy policy
          </a>{" "}
          applies.
        </li>
        <li>
          Authorities, if the law requires us to disclose information, and only what
          it requires.
        </li>
      </ul>

      <h2 className={LEGAL_H2}>International transfers</h2>
      <p className={LEGAL_P}>
        We are based in Israel. Your information is stored in{" "}
        <Fill>country or region of the servers</Fill>. The European Commission and
        the United Kingdom recognise Israel as providing an adequate level of data
        protection. <Fill>Describe any transfer outside Israel or the EEA and its safeguard, or remove this sentence</Fill>
      </p>

      <h2 className={LEGAL_H2}>Your rights, and how to use them</h2>
      <ul className={LEGAL_LIST}>
        <li>
          <strong>See and download your information.</strong> On{" "}
          <Link to="/you" className="underline">
            your profile
          </Link>
          , &ldquo;Download my data&rdquo; gives you a file with everything your
          account holds.
        </li>
        <li>
          <strong>Correct it.</strong> Change your subjects and newsletter choice on
          your profile. To change your name or email address, write to us.
        </li>
        <li>
          <strong>Delete it.</strong> &ldquo;Delete my account&rdquo; on your
          profile erases your account, folders and saved articles at once. Copies in
          our backups are removed when those backups expire, within{" "}
          <Fill>backup retention period</Fill>. To delete messages you sent us, or if
          you cannot sign in, write to us.
        </li>
        <li>
          <strong>Withdraw consent.</strong> Switch the newsletter off on your
          profile at any time.
        </li>
        <li>
          <strong>Object or restrict.</strong> Where we rely on legitimate
          interests, you can object; write to us.
        </li>
        <li>
          <strong>Complain.</strong> You can complain to Israel&rsquo;s Privacy
          Protection Authority, and if you are in the EU or UK, to your local data
          protection authority.
        </li>
      </ul>
      <p className={LEGAL_P}>
        When you write to us we may ask you to confirm you control the email address
        concerned, so that nobody else can obtain or erase your information. We
        answer within <Fill>response time, e.g. 30 days</Fill>.
      </p>
      <p className={LEGAL_P}>
        <Fill>EU / UK representative, if one is appointed — or remove</Fill>
      </p>

      <h2 className={LEGAL_H2}>Security</h2>
      <p className={LEGAL_P}>
        Passwords are stored only as Argon2id hashes. Sign-in and the contact form
        are rate-limited. Only our editorial staff can read contact messages, and
        they sign in separately from readers. No system is perfectly secure; if a
        breach affects you, we will tell you and the relevant authorities as the law
        requires.
      </p>

      <h2 className={LEGAL_H2}>Children</h2>
      <p className={LEGAL_P}>
        You.th is written for adults and is not directed at children. Accounts are
        for people aged <Fill>minimum age</Fill> or over.
      </p>

      <h2 className={LEGAL_H2}>Changes</h2>
      <p className={LEGAL_P}>
        If we change how we use your information, we will update this page and its
        date, and tell account holders by <Fill>notice method</Fill> before a
        material change takes effect.
      </p>

      <p className={LEGAL_P}>
        See also our{" "}
        <Link to="/terms" className="underline">
          terms of use
        </Link>
        .
      </p>
    </main>
  );
}
