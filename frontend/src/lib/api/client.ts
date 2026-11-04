/**
 * The single HTTP boundary. Framework-agnostic on purpose.
 *
 * Nothing in this directory imports from react-router, and nothing imports
 * react. That is what keeps the Next.js port mechanical (DESIGN.md §3.1): this
 * layer runs unchanged in a server component, a loader, or a client hook. The
 * moment a router hook or a `window` reference lands here, that stops being
 * true - and it stops being true quietly.
 */

const API_BASE = "/api";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * The two bearer tokens, and why they are two.
 *
 * A reviewer and a reader are different accounts in different tables, and the
 * backend mints their tokens with different `typ` claims - presenting one to
 * the other's surface is rejected before any lookup happens. Keeping a single
 * `authToken` here would mean a reviewer signing in on their own laptop
 * silently replaced their reader session, and every subsequent feed request
 * would carry a token the public API refuses to personalise with.
 *
 * Which one is sent is decided by the path, not by the caller: `/console/*` is
 * the reviewer surface and everything else is public. A caller choosing per
 * request is a caller that can choose wrong, and the wrong choice here leaks a
 * console token to an endpoint that has no business seeing one.
 *
 * Both are injected rather than read from storage here, so this module stays
 * free of browser globals - see the module docstring.
 */
let reviewerToken: string | null = null;
let readerToken: string | null = null;

export function setAuthToken(token: string | null): void {
  reviewerToken = token;
}

export function setReaderToken(token: string | null): void {
  readerToken = token;
}

function tokenFor(path: string): string | null {
  return path.startsWith("/console") ? reviewerToken : readerToken;
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  // Only a string body is JSON. A `FormData` body - the media upload - has to
  // reach `fetch` without a Content-Type, because the multipart boundary is
  // generated per request and only the browser knows it; setting the header
  // here strips the boundary and the server sees a body it cannot parse.
  if (typeof init.body === "string") headers.set("Content-Type", "application/json");
  const token = tokenFor(path);
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });

  if (response.status === 204) return undefined as T;

  if (!response.ok) {
    // A 409 from approve carries which citations broke - surfacing the detail
    // is the difference between "publish failed" and "citation S3 was deleted
    // from the body". Never collapse it into a generic message.
    const detail = await response.json().catch(() => undefined);
    throw new ApiError(response.status, `Request failed: ${response.status}`, detail);
  }

  return (await response.json()) as T;
}

/** Build a query string, dropping null/undefined. `false` is kept - it is an
 *  answer, and dropping it would silently mean "use the default", which for
 *  `personalise` is the opposite of what was asked. */
export function qs(
  params: Record<string, string | number | boolean | null | undefined>,
): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined) search.set(key, String(value));
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}
