// Theme applied before first paint.
//
// A blocking script in <head>, not a React effect: an effect runs after the
// first paint, so a reader on the dark theme would get a full-screen flash of
// the light ground on every load. A file rather than an inline script so the
// production Content-Security-Policy can refuse every inline script (see
// ops/Caddyfile). Kept in sync with src/lib/theme.ts, which owns the same key.
(function () {
  try {
    var stored = localStorage.getItem("youth.theme");
    var dark = stored
      ? stored === "dark"
      : window.matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  } catch (_) {
    document.documentElement.setAttribute("data-theme", "light");
  }
})();
