/**
 * Route tree.
 *
 * Structured to mirror a Next.js `app/` directory one-for-one, so the port
 * described in DESIGN.md §3.1 is mechanical:
 *
 *   /              -> app/(public)/page.tsx
 *   /a/:slug       -> app/(public)/a/[slug]/page.tsx
 *   /about         -> app/(public)/about/page.tsx
 *   /join, /you    -> app/(public)/...
 *   /contact       -> app/(public)/contact/page.tsx
 *   /privacy, /terms, /accessibility -> app/(public)/...
 *   /review/*      -> app/(review)/...  (noindex, authenticated)
 *
 * **The review desk lives at `/review`, not `/console`, and renders none of the
 * public chrome.** The old tree mounted `SiteHeader` above the router, so the
 * console rendered the public wordmark and the public nav advertised a link
 * nobody without an account could use. Now the two groups share nothing but
 * this file: the public group gets `SiteHeader` and the mobile tab bar, the
 * review group brings its own header, and no public surface links to it. The
 * gate itself is unchanged and is still the only thing that matters for access
 * — a URL is not a secret, `RequireAuth` and the server's bearer check are what
 * keep readers out.
 *
 * Public routes never import from `features/console`, and the review desk never
 * renders public chrome — the same separation the backend has between
 * `api/public` and `api/console`.
 */

import { Suspense, lazy } from "react";
import { Route, Routes } from "react-router-dom";

import { AuthProvider } from "./features/console/auth";
import { ReaderAuthProvider } from "./features/reader/auth";
import { PublicLayout } from "./features/shell/PublicLayout";
import { ROUTE_MESSAGE } from "./features/shell/styles";
import { ToastProvider } from "./features/shell/toast";
import { AboutRoute } from "./routes/about";
import { AccessibilityRoute } from "./routes/accessibility";
import { ArticleRoute } from "./routes/article";
import { ContactRoute } from "./routes/contact";
import { FeedRoute } from "./routes/feed";
import { JoinRoute } from "./routes/join";
import { PrivacyRoute } from "./routes/privacy";
import { TermsRoute } from "./routes/terms";
import { YouRoute } from "./routes/you";

/**
 * The review desk is lazy-loaded, and that is a correctness point rather than a
 * micro-optimisation: it carries the TipTap editor, which is most of the
 * bundle. Statically importing it would ship the whole editor to every public
 * reader — which is exactly what ArticleContent.tsx exists to avoid, so
 * importing it here would quietly undo that decision.
 */
const ReviewRoutes = lazy(() => import("./routes/review"));

export function App() {
  return (
    // Both providers wrap both groups, because a reviewer is also a reader of
    // the public site and may be signed in as both at once. The two tokens are
    // kept apart in `lib/api/client.ts`, not here.
    <ReaderAuthProvider>
      <AuthProvider>
        <ToastProvider>
          <Routes>
            <Route element={<PublicLayout />}>
              <Route path="/" element={<FeedRoute />} />
              <Route path="/a/:slug" element={<ArticleRoute />} />
              <Route path="/about" element={<AboutRoute />} />
              <Route path="/join" element={<JoinRoute />} />
              <Route path="/you" element={<YouRoute />} />
              <Route path="/contact" element={<ContactRoute />} />
              <Route path="/privacy" element={<PrivacyRoute />} />
              <Route path="/terms" element={<TermsRoute />} />
              <Route path="/accessibility" element={<AccessibilityRoute />} />
            </Route>

            <Route
              path="/review/*"
              element={
                <Suspense
                  fallback={<p className={ROUTE_MESSAGE}>Loading the review desk…</p>}
                >
                  <ReviewRoutes />
                </Suspense>
              }
            />
          </Routes>
        </ToastProvider>
      </AuthProvider>
    </ReaderAuthProvider>
  );
}
