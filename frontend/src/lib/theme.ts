/**
 * Light / dark theme state.
 *
 * The theme is a `data-theme` attribute on `<html>`; every colour in the app
 * resolves through a custom property that flips on that attribute, so switching
 * costs one attribute write and no re-render of anything but the toggle.
 *
 * First paint is handled by the inline script in `index.html`, not here - an
 * effect runs after paint and would flash the light ground. This module owns
 * the same storage key and must stay in step with it.
 *
 * `window` is never touched during render: the initial state is read in a
 * layout effect. That keeps the module safe to import from a server-rendered
 * tree under the Next.js port (DESIGN.md §3.1), where the same inline script
 * would move into `app/layout.tsx`.
 */

import { useCallback, useEffect, useState } from "react";

export type Theme = "light" | "dark";

const STORAGE_KEY = "youth.theme";

function read(): Theme {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "light" || stored === "dark") return stored;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  } catch {
    // Private-mode Safari throws on localStorage. A theme is not worth an
    // error boundary.
    return "light";
  }
}

function apply(theme: Theme): void {
  document.documentElement.setAttribute("data-theme", theme);
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    // Non-persistent is still usable for this session.
  }
}

export function useTheme(): { theme: Theme; toggle: () => void } {
  const [theme, setTheme] = useState<Theme>("light");

  // Adopt whatever the pre-paint script already decided, rather than deciding
  // again - re-deriving it here could disagree with what is on screen.
  useEffect(() => {
    const attr = document.documentElement.getAttribute("data-theme");
    setTheme(attr === "dark" ? "dark" : attr === "light" ? "light" : read());
  }, []);

  const toggle = useCallback(() => {
    setTheme((current) => {
      const next: Theme = current === "light" ? "dark" : "light";
      apply(next);
      return next;
    });
  }, []);

  return { theme, toggle };
}
