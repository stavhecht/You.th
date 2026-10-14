/**
 * The public site bar: browse, search, wordmark, account, theme.
 *
 * Under the Next.js port this is the root `layout.tsx` for the `(public)` group
 * — and only that group. The review desk renders its own chrome, which is the
 * structural half of moving it behind its own endpoint: the two surfaces now
 * share no header, so nothing on a reviewer's screen can be mistaken for the
 * public site and no public page advertises the desk.
 *
 * Search and the category choice live in the **URL**, not in a context. Both
 * are things a reader might reasonably bookmark, share or reach with the back
 * button, and a provider above the router would make all three impossible while
 * adding a second source of truth for the feed to disagree with.
 */

import { useState } from "react";
import { Link, NavLink, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { Moon, Search, Sun } from "lucide-react";

import { useReader, readerInitials } from "@/features/reader/auth";
import { useTheme } from "@/lib/theme";
import { CategoryDrawer } from "./CategoryDrawer";
import {
  AVATAR,
  HEADER_BAR,
  HEADER_LEFT,
  HEADER_RIGHT,
  MENU_BAR,
  MENU_BARS,
  MENU_BUTTON,
  NAV_LINK_ACCENT,
  SEARCH_FORM,
  SEARCH_ICON,
  SEARCH_INPUT,
  SITE_HEADER,
  THEME_TOGGLE,
  WORDMARK,
  WORDMARK_IMAGE,
  navLink,
} from "./styles";

export function SiteHeader() {
  const { theme, toggle } = useTheme();
  const { reader, signedIn } = useReader();
  // Local, not in the URL: nobody wants a history entry for opening a menu.
  const [drawerOpen, setDrawerOpen] = useState(false);

  return (
    <>
      <header className={SITE_HEADER}>
        <div className={HEADER_BAR}>
          <div className={HEADER_LEFT}>
            <button
              onClick={() => setDrawerOpen(true)}
              aria-label="Browse categories"
              aria-expanded={drawerOpen}
              className={MENU_BUTTON}
            >
              <span aria-hidden className={MENU_BARS}>
                <span className={MENU_BAR} />
                <span className={MENU_BAR} />
                <span className={MENU_BAR} />
              </span>
            </button>

            <SearchField />
          </div>

          <Link to="/" className={WORDMARK} aria-label="You.th — home">
            {/*
              Width and height are both set so the bar does not reflow when the
              image lands — the wordmark is the widest thing in the header and a
              late layout shift moves every control beside it.
            */}
            <img
              src="/media/you.th-logo.png"
              alt="You.th"
              width={939}
              height={270}
              className={WORDMARK_IMAGE}
            />
          </Link>

          <div className={HEADER_RIGHT}>
            <NavLink to="/about" className={({ isActive }) => navLink(isActive)}>
              <span className="hidden sm:inline">About</span>
            </NavLink>

            <Link to={signedIn ? "/you" : "/join"} className={NAV_LINK_ACCENT}>
              <span className="hidden sm:inline">
                {signedIn ? "Your feed" : "Join"}
              </span>
            </Link>

            <Link
              to={signedIn ? "/you" : "/join"}
              aria-label={signedIn ? "Your profile" : "Create an account"}
              className={AVATAR}
            >
              {readerInitials(reader)}
            </Link>

            <button
              onClick={toggle}
              // The label names the destination, not the current state: a
              // control reading "Light" while you are on the light theme is
              // ambiguous about whether it reports or acts.
              aria-label={`Switch to the ${theme === "light" ? "dark" : "light"} theme`}
              title={`Switch to the ${theme === "light" ? "dark" : "light"} theme`}
              className={THEME_TOGGLE}
            >
              {theme === "light" ? (
                <Moon size={15} aria-hidden />
              ) : (
                <Sun size={15} aria-hidden />
              )}
            </button>
          </div>
        </div>
      </header>

      <CategoryDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)} />
    </>
  );
}

/**
 * Free-text search over the feed.
 *
 * A `<form>` rather than a live-updating input, and the difference matters:
 * typing writes into the URL on submit only, so the browser's history gets one
 * entry per search rather than one per keystroke. It also means the feed
 * refetches once instead of on every character.
 *
 * Submitting from anywhere on the site lands on the feed — searching from an
 * article should show results, not filter the article you are reading.
 */
function SearchField() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const location = useLocation();

  return (
    <form
      role="search"
      className={SEARCH_FORM}
      onSubmit={(event) => {
        event.preventDefault();
        const query = new FormData(event.currentTarget).get("q");
        const next = new URLSearchParams(location.pathname === "/" ? params : undefined);
        if (typeof query === "string" && query.trim()) next.set("q", query.trim());
        else next.delete("q");
        navigate({ pathname: "/", search: next.toString() });
      }}
    >
      <Search size={14} strokeWidth={2} aria-hidden className={SEARCH_ICON} />
      <input
        name="q"
        type="search"
        // Keyed on the current query so the field re-syncs when the URL changes
        // underneath it — clearing a search from the feed has to empty the box.
        key={params.get("q") ?? ""}
        defaultValue={params.get("q") ?? ""}
        placeholder="Search article"
        aria-label="Search published articles"
        className={SEARCH_INPUT}
      />
    </form>
  );
}
