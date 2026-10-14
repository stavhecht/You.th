/**
 * The review desk's own bar.
 *
 * Deliberately not the public `SiteHeader`, and the differences are the point
 * rather than styling: no search over published articles, no account avatar, no
 * category drawer, and a wordmark that says which surface you are on. A reviewer
 * who cannot tell the queue from the site is a reviewer who can mistake a draft
 * for something readers can already see.
 *
 * The one link out is to the public feed, so a reviewer can check what they just
 * published — that direction is safe. The public site carries no link back.
 */

import { Link } from "react-router-dom";
import { Moon, Sun } from "lucide-react";

import { useTheme } from "@/lib/theme";
import { useAuth } from "./auth";
import {
  REVIEW_BAR,
  REVIEW_BRAND,
  REVIEW_BRAND_LOGO,
  REVIEW_BRAND_TAG,
  REVIEW_HEADER,
  REVIEW_NAV,
  REVIEW_NAV_LINK,
} from "./styles";
import { SIGNED_IN_AS, SIGN_OUT_ACTION } from "./styles";
import { THEME_TOGGLE } from "@/features/shell/styles";

export function ReviewHeader() {
  const { theme, toggle } = useTheme();
  const { reviewer, logout } = useAuth();

  return (
    <header className={REVIEW_HEADER}>
      <div className={REVIEW_BAR}>
        {/*
          Three columns, mark in the middle — the public bar's layout. What
          names the surface is the accent tag in the left column, where the
          public site puts browse and search.
        */}
        <span className={REVIEW_BRAND_TAG}>Review desk</span>

        <Link to="/review" className={REVIEW_BRAND} aria-label="Review desk — queue">
          <img
            src="/media/you.th-logo.png"
            alt="You.th"
            width={315}
            height={110}
            className={REVIEW_BRAND_LOGO}
          />
        </Link>

        <nav className={REVIEW_NAV}>
          {reviewer ? (
            <>
              <Link
                to="/"
                // Opens the live site in its own tab: a reviewer checking a
                // published article should not lose the queue position they
                // were working through to do it.
                target="_blank"
                rel="noreferrer"
                className={REVIEW_NAV_LINK}
              >
                Public feed ↗
              </Link>

              <span className={SIGNED_IN_AS}>
                {reviewer.displayName}
                <button onClick={logout} className={SIGN_OUT_ACTION}>
                  Sign out
                </button>
              </span>
            </>
          ) : null}

          <button
            onClick={toggle}
            aria-label={`Switch to the ${theme === "light" ? "dark" : "light"} theme`}
            className={THEME_TOGGLE}
          >
            {theme === "light" ? <Moon size={15} aria-hidden /> : <Sun size={15} aria-hidden />}
          </button>
        </nav>
      </div>
    </header>
  );
}
