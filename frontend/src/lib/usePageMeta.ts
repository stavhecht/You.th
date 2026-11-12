/**
 * The tab title and meta description for the page on screen.
 *
 * Every page used to share the one title in `index.html`, so a shared tab, a
 * bookmark and a search result all read "You.th: simplifying your way to stay
 * young" whatever they pointed at. Restored on unmount, so a route that sets
 * nothing shows the site's own.
 *
 * Search engines that run scripts read this. Link previews (WhatsApp, Slack)
 * do not; those are answered by `/api/share/{slug}` behind the proxy.
 */
import { useEffect } from "react";

const SITE = "You.th";

export function usePageMeta(title?: string, description?: string): void {
  useEffect(() => {
    if (!title) return;
    const previousTitle = document.title;
    const meta = document.querySelector('meta[name="description"]');
    const previousDescription = meta?.getAttribute("content") ?? null;

    document.title = `${title} · ${SITE}`;
    if (description) meta?.setAttribute("content", description);

    return () => {
      document.title = previousTitle;
      if (previousDescription !== null) meta?.setAttribute("content", previousDescription);
    };
  }, [title, description]);
}
