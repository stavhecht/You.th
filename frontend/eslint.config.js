// The site's linter: correctness rules only. Formatting is not checked here.
//
// `react-hooks` is the reason this exists: a hook with a missing dependency
// keeps reading the value from its first render, and nothing else in the
// toolchain says so. Only its two classic rules are on. Its newer "recommended"
// set adds the React Compiler rules (purity, refs during render); turn those on
// one at a time, reading what each flags, rather than all at once.
import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "node_modules", "playwright-report", "test-results"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: { globals: globals.browser },
    plugins: { "react-hooks": reactHooks },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "error",
      // An unused name is a leftover; `_name` marks one kept on purpose.
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_", caughtErrors: "none" },
      ],
    },
  },
  {
    // Node, not the browser: Playwright, Vite and Tailwind configuration.
    // No React here, and Playwright's fixture callback is named `use`, which
    // the hooks rule would read as a React hook.
    files: ["e2e/**", "*.config.ts", "*.config.js"],
    languageOptions: { globals: globals.node },
    rules: { "react-hooks/rules-of-hooks": "off", "react-hooks/exhaustive-deps": "off" },
  },
);
