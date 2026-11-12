import { ApiError } from "@/lib/api/client";

/**
 * Surface the server's detail verbatim where there is one.
 *
 * The 409 from approve says *which* handles no longer resolve; replacing that
 * with "Publish failed" would send the reviewer hunting through the body.
 */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    const detail = (error.detail as { detail?: string } | undefined)?.detail;
    if (typeof detail === "string" && detail) return detail;
    if (error.status === 401) return "Your session has ended. Sign in again in another tab, then retry.";
    if (error.status === 409) return "This draft can no longer be published.";
  }
  return error instanceof Error ? error.message : "Something went wrong.";
}
