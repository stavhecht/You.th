/**
 * Console auth state.
 *
 * The token lives in `sessionStorage`, not `localStorage`: it expires with the
 * tab, which suits a review tool used in sittings and limits exposure on a
 * shared machine. This is not XSS-proof - nothing readable by JS is - but the
 * console is a small authenticated surface and an httpOnly-cookie flow would
 * mean CSRF handling for no benefit at this size. Revisit if the console grows
 * public-facing surface area.
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
import { Navigate, useLocation } from "react-router-dom";

import { setAuthToken } from "@/lib/api/client";
import { fetchMe, login as loginRequest } from "@/lib/api/console";
import { ROUTE_MESSAGE } from "@/features/shell/styles";
import type { Reviewer } from "@/types/api";

const TOKEN_KEY = "youth.console.token";

interface AuthState {
  reviewer: Reviewer | null;
  status: "loading" | "authenticated" | "anonymous";
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [reviewer, setReviewer] = useState<Reviewer | null>(null);
  const [status, setStatus] = useState<AuthState["status"]>("loading");

  // Restore a session on reload. The token is re-validated against /me rather
  // than trusted: it may have expired, or the reviewer may have been
  // deactivated since it was issued.
  useEffect(() => {
    const stored = sessionStorage.getItem(TOKEN_KEY);
    if (!stored) {
      setStatus("anonymous");
      return;
    }
    setAuthToken(stored);
    fetchMe()
      .then((me) => {
        setReviewer(me);
        setStatus("authenticated");
      })
      .catch(() => {
        sessionStorage.removeItem(TOKEN_KEY);
        setAuthToken(null);
        setStatus("anonymous");
      });
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const { token, reviewer: me } = await loginRequest(email, password);
    sessionStorage.setItem(TOKEN_KEY, token);
    setReviewer(me);
    setStatus("authenticated");
  }, []);

  const logout = useCallback(() => {
    sessionStorage.removeItem(TOKEN_KEY);
    setAuthToken(null);
    setReviewer(null);
    setStatus("anonymous");
  }, []);

  const value = useMemo(
    () => ({ reviewer, status, login, logout }),
    [reviewer, status, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}

/** Gate for every console route. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { status } = useAuth();
  const location = useLocation();

  if (status === "loading") {
    return (
      <p className={ROUTE_MESSAGE}>Checking session…</p>
    );
  }
  if (status === "anonymous") {
    // Remember where they were headed so login can return them there.
    return <Navigate to="/review/login" state={{ from: location }} replace />;
  }
  return <>{children}</>;
}
