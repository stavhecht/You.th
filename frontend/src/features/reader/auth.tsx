/**
 * Reader session state.
 *
 * Deliberately different from the console's in one respect: the token lives in
 * `localStorage`, not `sessionStorage`. A reviewer works in sittings and a
 * session that ends with the tab is a feature there; a reader comes back next
 * week and should not have to think about it, which is why the backend gives
 * their token a thirty-day life rather than twelve hours.
 *
 * The rest of the shape matches `features/console/auth.tsx`, including
 * re-validating a restored token against `/me` rather than trusting it: it may
 * have expired, and thirty days is long enough for that to be the common case
 * rather than the rare one.
 *
 * Everything below degrades to signed-out. Nothing on the public site requires
 * an account, so a failed restore is a quiet fall back to the anonymous feed,
 * never an error screen.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";

import { setReaderToken } from "@/lib/api/client";
import {
  fetchMe,
  login as loginRequest,
  signup as signupRequest,
  updateMe as updateMeRequest,
} from "@/lib/api/reader";
import type { Reader, Subject } from "@/types/api";

const TOKEN_KEY = "youth.reader.token";

export interface SignupInput {
  email: string;
  password: string;
  displayName: string;
  interests: Subject[];
  newsletter: boolean;
}

interface ReaderAuthState {
  reader: Reader | null;
  status: "loading" | "authenticated" | "anonymous";
  signedIn: boolean;
  login: (email: string, password: string) => Promise<void>;
  signup: (input: SignupInput) => Promise<void>;
  update: (patch: {
    displayName?: string;
    interests?: Subject[];
    newsletter?: boolean;
  }) => Promise<void>;
  logout: () => void;
}

const ReaderAuthContext = createContext<ReaderAuthState | null>(null);

export function ReaderAuthProvider({ children }: { children: ReactNode }) {
  const [reader, setReader] = useState<Reader | null>(null);
  const [status, setStatus] = useState<ReaderAuthState["status"]>("loading");
  const queryClient = useQueryClient();

  useEffect(() => {
    let stored: string | null = null;
    try {
      stored = localStorage.getItem(TOKEN_KEY);
    } catch {
      // Private-mode Safari throws. Browsing signed-out is a complete
      // experience here, so there is nothing to recover from.
    }

    if (!stored) {
      setStatus("anonymous");
      return;
    }

    setReaderToken(stored);
    fetchMe()
      .then((me) => {
        setReader(me);
        setStatus("authenticated");
      })
      .catch(() => {
        forgetToken();
        setReaderToken(null);
        setStatus("anonymous");
      });
  }, []);

  /**
   * Adopt a session and drop every cached response from the previous one.
   *
   * The feed's cache key includes `personalised`, but the *saved* queries are
   * keyed only by folder - so without this reset, signing in as someone else
   * on a shared machine shows the previous reader's shelf until a refetch
   * lands. Clearing is the only version of this that cannot be got wrong by
   * forgetting a key later.
   */
  const adopt = useCallback(
    (token: string, me: Reader) => {
      try {
        localStorage.setItem(TOKEN_KEY, token);
      } catch {
        // Non-persistent is still a usable session for this visit.
      }
      setReader(me);
      setStatus("authenticated");
      void queryClient.resetQueries();
    },
    [queryClient],
  );

  const login = useCallback(
    async (email: string, password: string) => {
      const { token, reader: me } = await loginRequest(email, password);
      adopt(token, me);
    },
    [adopt],
  );

  const signup = useCallback(
    async (input: SignupInput) => {
      const { token, reader: me } = await signupRequest(input);
      adopt(token, me);
    },
    [adopt],
  );

  const update = useCallback(
    async (patch: {
      displayName?: string;
      interests?: Subject[];
      newsletter?: boolean;
    }) => {
      const next = await updateMeRequest(patch);
      setReader(next);
      // Interests reorder the feed server-side, so the cached pages are stale
      // the moment they change.
      void queryClient.invalidateQueries({ queryKey: ["feed"] });
    },
    [queryClient],
  );

  const logout = useCallback(() => {
    forgetToken();
    setReaderToken(null);
    setReader(null);
    setStatus("anonymous");
    void queryClient.resetQueries();
  }, [queryClient]);

  const value = useMemo(
    () => ({
      reader,
      status,
      signedIn: status === "authenticated",
      login,
      signup,
      update,
      logout,
    }),
    [reader, status, login, signup, update, logout],
  );

  return (
    <ReaderAuthContext.Provider value={value}>{children}</ReaderAuthContext.Provider>
  );
}

function forgetToken(): void {
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    // Nothing was stored, so nothing to remove.
  }
}

export function useReader(): ReaderAuthState {
  const context = useContext(ReaderAuthContext);
  if (!context) throw new Error("useReader must be used inside ReaderAuthProvider");
  return context;
}

/** Initials for the avatar. `?` while signed out - a guest is not a person. */
export function readerInitials(reader: Reader | null): string {
  if (!reader) return "?";
  const letters = reader.displayName
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((word) => word[0]?.toUpperCase() ?? "")
    .join("");
  return letters || "Y";
}
