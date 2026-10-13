/**
 * The public group's shell: site bar above, tab bar below, route in between.
 *
 * Under the Next.js port this is `app/(public)/layout.tsx` verbatim, with
 * `<Outlet/>` becoming `{children}`.
 *
 * Rendered as a layout route rather than above `<Routes>` so the review desk
 * does not inherit it — the header is the public site's, and a reviewer working
 * through a queue should not be looking at a search box for articles they have
 * not published yet.
 */

import { useEffect } from "react";
import { Link, Outlet, useLocation } from "react-router-dom";

import { MobileTabBar } from "./MobileTabBar";
import { SiteHeader } from "./SiteHeader";
import { SITE_FOOTER, SITE_FOOTER_LINK, SKIP_LINK } from "./pageStyles";

export function PublicLayout() {
  const { pathname } = useLocation();

  /*
   * Scroll to the top on navigation.
   *
   * The browser restores scroll position on Back, which is right, but a fresh
   * push keeps whatever offset the previous page had — so opening an article
   * from halfway down the feed lands you halfway down the article. Keyed on
   * `pathname` only, deliberately: the search string changes when a filter is
   * applied, and yanking the reader to the top for that would be hostile.
   */
  useEffect(() => {
    window.scrollTo(0, 0);
  }, [pathname]);

  return (
    <>
      {/* First focusable element on every public page: jumps past the site
          bar (menu, search, nav, theme) to the page itself. WCAG 2.4.1, and
          the first thing an IS 5568 audit tries with a keyboard. Hidden until
          focused. */}
      <a href="#content" className={SKIP_LINK}>
        Skip to content
      </a>
      <SiteHeader />
      {/* tabIndex -1 so the skip link moves keyboard focus here, not just the
          scroll position; no outline, because nothing here is a control. */}
      <div id="content" tabIndex={-1} className="outline-none">
        <Outlet />
      </div>
      {/* Above the tab bar, which is sticky to the bottom on phones, so the
          links stay reachable there too. The same links sit in the category
          drawer for anyone who never scrolls to the end of the feed. */}
      <footer className={SITE_FOOTER}>
        <Link to="/about" className={SITE_FOOTER_LINK}>About</Link>
        <Link to="/contact" className={SITE_FOOTER_LINK}>Contact</Link>
        <Link to="/privacy" className={SITE_FOOTER_LINK}>Privacy</Link>
        <Link to="/terms" className={SITE_FOOTER_LINK}>Terms</Link>
        <Link to="/accessibility" className={SITE_FOOTER_LINK}>Accessibility</Link>
      </footer>
      <MobileTabBar />
    </>
  );
}
