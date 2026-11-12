/**
 * The page a "confirm your email" link opens.
 *
 * The link carries a token; this page hands it to the API once and says what
 * happened. Nothing to type. Signup takes any address, so until this is opened
 * the account cannot send the editors a topic suggestion under that address.
 */
import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import {
  PAGE_BACK_LINK,
  PAGE_KICKER,
  PAGE_STANDFIRST,
  PAGE_TITLE,
  WIDE_PAGE,
} from "@/features/shell/pageStyles";
import { verifyEmail } from "@/lib/api/reader";
import { usePageMeta } from "@/lib/usePageMeta";

export function VerifyRoute() {
  usePageMeta("Confirm your email");
  const [params] = useSearchParams();
  const token = params.get("token");
  const [state, setState] = useState<"checking" | "done" | "failed">(
    token ? "checking" : "failed",
  );
  // React's development double-mount would otherwise send the link twice.
  const sent = useRef(false);

  useEffect(() => {
    if (!token || sent.current) return;
    sent.current = true;
    verifyEmail(token).then(
      () => setState("done"),
      () => setState("failed"),
    );
  }, [token]);

  return (
    <main className={WIDE_PAGE}>
      <Link to="/" className={PAGE_BACK_LINK}>
        ← Feed
      </Link>
      <div className="max-w-[440px]" role="status">
        <div className={PAGE_KICKER}>Your email</div>
        <h1 className={PAGE_TITLE}>
          {state === "checking"
            ? "Confirming…"
            : state === "done"
              ? "Your email is confirmed."
              : "This link has expired."}
        </h1>
        {state === "failed" ? (
          <p className={PAGE_STANDFIRST}>
            Ask for a new one from{" "}
            <Link to="/you" className="underline">
              your account page
            </Link>
            .
          </p>
        ) : null}
      </div>
    </main>
  );
}
