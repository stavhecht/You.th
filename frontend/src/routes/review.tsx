/**
 * The review desk, as a lazy-loaded bundle.
 *
 * Default-exports its own nested `<Routes>` so App.tsx can `lazy()` the whole
 * subtree - the desk carries the TipTap editor, and the public feed must not pay
 * for it.
 *
 * **It renders its own chrome and none of the public site's.** That is the
 * structural half of moving it off `/console` and behind `/review`: the two
 * surfaces now share nothing but the route table, so a reviewer is never looking
 * at a reader's search box and the public header never carries a link to a
 * screen only staff can open. Access itself is unchanged and is not about the
 * URL - a path is not a secret. `RequireAuth` gates the client, and every
 * `/api/console/*` route is behind a router-level bearer check that a reader's
 * token cannot satisfy: it is minted with a different `typ` claim and is
 * rejected before the user lookup even runs.
 *
 * Under a Next.js port this becomes a `(review)` route group marked noindex:
 * the queue must never be crawlable.
 */

import { Route, Routes } from "react-router-dom";

import { RequireAuth } from "@/features/console/auth";
import { LoginForm } from "@/features/console/LoginForm";
import { ReviewDetail } from "@/features/console/ReviewDetail";
import { ReviewHeader } from "@/features/console/ReviewHeader";
import { ReviewQueue } from "@/features/console/ReviewQueue";

export default function ReviewRoutes() {
  return (
    /*
      `ew-type-console` retypes the whole desk back to Archivo - see the type
      scopes in `styles/youth.css`. It sits here rather than on each screen so
      a new console route cannot forget it, and it is a wrapper element rather
      than a class toggled onto `<html>` so nothing has to be cleaned up when
      the reviewer navigates back to the public site.
    */
    <div className="ew-type-console">
      <ReviewHeader />
      <Routes>
        {/* Paths are relative to /review/* */}
        <Route path="login" element={<LoginForm />} />
        <Route
          index
          element={
            <RequireAuth>
              <ReviewQueue />
            </RequireAuth>
          }
        />
        {/*
          `article/:id`, not `:id` - a bare parameter here would swallow
          `login` on any router that ranked them the other way round, and the
          failure mode is a login page that renders "article not found".
        */}
        <Route
          path="article/:id"
          element={
            <RequireAuth>
              <ReviewDetail />
            </RequireAuth>
          }
        />
      </Routes>
    </div>
  );
}
