/**
 * The bottom tab bar, below `sm` only.
 *
 * The top bar keeps the wordmark, search and the theme toggle at every width;
 * what it cannot keep on a phone is the nav cluster, which is why About and the
 * account collapse to icons up there and reappear as labelled tabs down here.
 * Four tabs, matching the comp: Feed, Categories, Saved, You.
 *
 * "Categories" opens the drawer rather than navigating, so it is a `<button>`
 * among links. That asymmetry is deliberate and is why the drawer state is
 * duplicated here rather than lifted: two independent triggers for one panel is
 * simpler than one panel with two owners, and the panel itself is stateless.
 */

import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { useReader } from "@/features/reader/auth";
import { CategoryDrawer } from "./CategoryDrawer";
import { TAB_BAR, tabButton } from "./styles";

export function MobileTabBar() {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const { signedIn } = useReader();
  const [drawerOpen, setDrawerOpen] = useState(false);

  const tabs = [
    { name: "Feed", to: "/" },
    { name: "Categories", to: null },
    { name: "Saved", to: "/you" },
    // "Sign in", not "Join": the header shows neither on a phone, and a
    // returning reader has no other way in. The sign-in page links to Join.
    { name: signedIn ? "You" : "Sign in", to: signedIn ? "/you" : "/signin" },
  ];

  return (
    <>
      <nav className={TAB_BAR} aria-label="Sections">
        {tabs.map((tab) => (
          <button
            key={tab.name}
            onClick={() => (tab.to ? navigate(tab.to) : setDrawerOpen(true))}
            aria-current={tab.to && pathname === tab.to ? "page" : undefined}
            className={tabButton(Boolean(tab.to) && pathname === tab.to)}
          >
            {tab.name}
          </button>
        ))}
      </nav>

      <CategoryDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)} />
    </>
  );
}
