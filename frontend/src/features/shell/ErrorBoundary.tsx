/**
 * Catches a render error anywhere below it.
 *
 * React unmounts the whole tree on an uncaught render error, so one bad article
 * left a blank page with nothing to press. A class because error boundaries
 * have no hook form. The reload link is a plain anchor on purpose: the router
 * may be part of what broke.
 */
import { Component, type ErrorInfo, type ReactNode } from "react";

import { ROUTE_ERROR_MESSAGE } from "./styles";

export class ErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("render failed", error, info.componentStack);
  }

  render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    return (
      <main>
        <p role="alert" className={ROUTE_ERROR_MESSAGE}>
          Something went wrong showing this page.{" "}
          <a href="/" className="underline">
            Go to the feed
          </a>
        </p>
      </main>
    );
  }
}
