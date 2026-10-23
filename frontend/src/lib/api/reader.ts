/**
 * Reader account data access — sessions, interests, folders, saves.
 *
 * Framework-agnostic like the rest of `lib/api`: plain fetch and typed
 * contracts, no react, no router. The React bindings live in
 * `features/reader/`.
 *
 * Nothing here talks to `/console`. A reader token is minted with a different
 * `typ` claim and is refused by the console's decoder outright, so these two
 * clients cannot be crossed by accident even if a path were mistyped.
 */

import { apiFetch, qs, setReaderToken } from "./client";
import type {
  ContactSubmission,
  Folder,
  Reader,
  SavedIndex,
  SavedPage,
  Subject,
} from "@/types/api";

export const readerKeys = {
  me: ["reader", "me"] as const,
  folders: ["reader", "folders"] as const,
  /** Every save-related query, for a single invalidate after a toggle. */
  saved: ["reader", "saved"] as const,
  savedIn: (folderId: string | null) => ["reader", "saved", folderId ?? "all"] as const,
  savedIndex: ["reader", "saved", "index"] as const,
};

interface TokenResponse {
  access_token: string;
  expires_in: number;
}

/**
 * Create an account and sign in with it.
 *
 * One request, not two: the form that posts here is "build my feed", and
 * bouncing someone to a login screen straight after they typed their password
 * is a step with no purpose.
 */
export async function signup(payload: {
  email: string;
  password: string;
  displayName: string;
  interests: Subject[];
  newsletter: boolean;
}): Promise<{ token: string; reader: Reader }> {
  const response = await apiFetch<TokenResponse>("/readers/signup", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  setReaderToken(response.access_token);
  return { token: response.access_token, reader: await fetchMe() };
}

export async function login(
  email: string,
  password: string,
): Promise<{ token: string; reader: Reader }> {
  const response = await apiFetch<TokenResponse>("/readers/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
  setReaderToken(response.access_token);
  return { token: response.access_token, reader: await fetchMe() };
}

/** Re-validates the stored token as a side effect of loading the reader. */
export async function fetchMe(): Promise<Reader> {
  return apiFetch<Reader>("/readers/me");
}

/**
 * Patch the profile. An omitted field is left alone.
 *
 * `interests: []` is therefore a real instruction — turn personalisation off —
 * and is distinct from not sending the field at all. Anything that "cleans"
 * empty arrays out of this payload breaks the only way to switch it back off.
 */
export async function updateMe(payload: {
  displayName?: string;
  interests?: Subject[];
  newsletter?: boolean;
}): Promise<Reader> {
  return apiFetch<Reader>("/readers/me", {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

/**
 * A copy of everything the account holds, as the server built it.
 *
 * Returned as the parsed JSON rather than turned into a file here: making a
 * download needs `document` and `URL.createObjectURL`, and this layer stays
 * free of browser globals (see `client.ts`). The caller saves it.
 */
export async function exportMyData(): Promise<unknown> {
  return apiFetch<unknown>("/readers/me/export");
}

/**
 * Erase the account, its folders and its saves. Needs the current password.
 *
 * A wrong password answers 403, not 401, so it reads as "try again" rather
 * than as an expired session.
 */
export async function deleteAccount(password: string): Promise<void> {
  return apiFetch<void>("/readers/me", {
    method: "DELETE",
    body: JSON.stringify({ password }),
  });
}

export async function fetchFolders(): Promise<Folder[]> {
  return apiFetch<Folder[]>("/readers/folders");
}

export async function createFolder(name: string): Promise<Folder> {
  return apiFetch<Folder>("/readers/folders", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}

/** The shelf and its folder tabs, in one request. */
export async function fetchSaved(folderId?: string | null): Promise<SavedPage> {
  return apiFetch<SavedPage>(`/readers/saved${qs({ folderId })}`);
}

/** `{ slug: folderId }` — one request for a whole page of tiles. */
export async function fetchSavedIndex(): Promise<SavedIndex> {
  return apiFetch<SavedIndex>("/readers/saved/slugs");
}

/** Idempotent. Re-saving into a different folder moves it rather than copying. */
export async function saveArticle(slug: string, folderId?: string): Promise<void> {
  return apiFetch<void>(`/readers/saved/${encodeURIComponent(slug)}`, {
    method: "PUT",
    body: JSON.stringify({ folderId: folderId ?? null }),
  });
}

export async function unsaveArticle(slug: string): Promise<void> {
  return apiFetch<void>(`/readers/saved/${encodeURIComponent(slug)}`, {
    method: "DELETE",
  });
}

/**
 * "Let us know" — a claim to check or a topic to cover.
 *
 * Unauthenticated: a request is worth having from someone who never signs up.
 * 202, not 201 — the row exists, but the thing being asked for has not
 * happened and may not.
 */
export async function submitContact(payload: ContactSubmission): Promise<void> {
  await apiFetch<{ id: string }>("/contact", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
