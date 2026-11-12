/**
 * Any address the site does not have. Without this route an unknown URL
 * rendered an empty page: no header, no way back.
 */
import { Link } from "react-router-dom";

import {
  ARTICLE_ERROR_PAGE,
  ARTICLE_ERROR_TITLE,
  BACK_TO_FEED_LINK,
} from "@/features/feed/styles";
import { usePageMeta } from "@/lib/usePageMeta";

export function NotFoundRoute() {
  usePageMeta("Page not found");
  return (
    <main className={ARTICLE_ERROR_PAGE}>
      <h1 className={ARTICLE_ERROR_TITLE}>Page not found</h1>
      <Link to="/" className={BACK_TO_FEED_LINK}>
        Back to the feed
      </Link>
    </main>
  );
}
