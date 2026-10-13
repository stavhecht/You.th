import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";

/*
 * Fonts are bundled, not fetched from Google Fonts.
 *
 * The `<link>` to fonts.googleapis.com sent every visitor's IP address to
 * Google on first paint, before they had done anything — a disclosure with no
 * purpose the reader asked for (COMPLIANCE_AUDIT.md F8). Served from our own
 * origin, the bytes are the same and nobody else learns who is reading.
 *
 * Exactly the faces `index.html` used to request: Archivo at the five weights
 * the site bar and article titles use, Instrument Serif at 400 and its italic.
 * The family names are the same, so no stylesheet changes.
 */
import "@fontsource/archivo/400.css";
import "@fontsource/archivo/500.css";
import "@fontsource/archivo/600.css";
import "@fontsource/archivo/700.css";
import "@fontsource/archivo/800.css";
import "@fontsource/instrument-serif/400.css";
import "@fontsource/instrument-serif/400-italic.css";

import { App } from "./App";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Published articles change rarely; the feed does not need to refetch on
      // every window focus.
      staleTime: 60_000,
      refetchOnWindowFocus: false,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
