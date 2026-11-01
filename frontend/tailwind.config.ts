import type { Config } from "tailwindcss";

/**
 * Every value here resolves to a design-system custom property rather than a
 * literal. Components therefore say `text-ink-3` / `border-rule-soft`, and the
 * theme — including the light/dark flip — is decided entirely in
 * `src/styles/youth.css`. A hex in a component is a bug: it cannot follow the
 * theme, and it is invisible to anyone retuning the system.
 *
 * The old `verdict.*` ramp (green / amber / orange / stone) is deliberately
 * gone. The design direction takes verdict off the colour axis entirely and
 * carries it in geometry instead — see `features/evidence/VerdictMark.tsx`.
 * Colour now means *subject*, never judgment.
 *
 * The consequence to know about: colours defined as `var(...)` do not support
 * Tailwind's slash-opacity syntax (`text-ink/50` will not work). That is
 * intentional pressure toward the four-step ink ramp, which hits its contrast
 * ratios predictably on both grounds where a faded ink does not.
 */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: ["class", 'html[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        ground: "var(--ew-bg)",
        surface: "var(--ew-surface)",
        // A third ground, warmer and one step down from `surface`: the resting
        // state of a tile before its picture loads, and the fill behind the
        // source block on an article.
        "surface-2": "var(--ew-surface-2)",
        tile: "var(--ew-tile)",

        // The inverted pair, named for the relationship rather than the
        // colour, so they flip with the theme instead of becoming a
        // light-on-light bug. `bg-invert text-invert-fg` is the filled
        // control; `bg-panel text-panel-fg` is the dark explainer block.
        invert: {
          DEFAULT: "var(--ew-invert-bg)",
          fg: "var(--ew-invert-fg)",
        },
        panel: {
          DEFAULT: "var(--ew-panel)",
          fg: "var(--ew-panel-fg)",
        },

        // The ink ramp. Each step has a contrast job: see the palette table on
        // the design-system page for the measured ratios.
        ink: {
          DEFAULT: "var(--ew-ink)", // headlines, body, verdict marks
          2: "var(--ew-ink-2)", //     excerpts, secondary prose
          3: "var(--ew-ink-3)", //     meta, qualifiers, study type
          4: "var(--ew-ink-4)", //     rules and 14px+ only, never body copy
        },

        rule: {
          DEFAULT: "var(--ew-rule)", //  the strong 2px structural rule
          soft: "var(--ew-rule-soft)", // hairline borders
        },

        accent: {
          DEFAULT: "var(--ew-accent)", // chrome, marks, focus — not text
          ink: "var(--ew-accent-ink)", // the accent at text size
          hover: "var(--ew-accent-hover)",
          wash: "var(--ew-accent-wash)",
          on: "var(--ew-on-accent)", // text on an accent fill
        },

        "danger-ink": "var(--ew-danger-ink)", // a failed check at text size

        // The only chromatic axis in the product. Subject, never verdict.
        subject: {
          fitness: "var(--ew-cat-fitness)",
          nutrition: "var(--ew-cat-nutrition)",
          supplements: "var(--ew-cat-supplements)",
          "sleep-recovery": "var(--ew-cat-sleep-recovery)",
          lifestyle: "var(--ew-cat-lifestyle)",
          "preventive-health": "var(--ew-cat-preventive-health)",
          "general-health": "var(--ew-cat-general-health)",
          wellness: "var(--ew-cat-wellness)",
          other: "var(--ew-cat-other)",
        },
      },

      fontFamily: {
        /**
         * Both resolve to Instrument Serif — see the type block in
         * `styles/youth.css`. They stay two names because they carry two
         * intents, and a future retype that splits the face again should not
         * have to rediscover which of two hundred class strings meant which.
         */
        heading: "var(--font-heading)",
        body: "var(--font-body)",
        /**
         * The exception, and the only sans in the product: article titles and
         * the nav buttons in the site bar, the drawer and the tab bar. Both are
         * type that has to survive at 10–12px, tracked out, frequently over a
         * photograph — which is the one job a display serif does badly.
         *
         * Anywhere else this is the wrong face; reach for `font-body`. A third
         * kind of caller here is how an interface ends up set in two typefaces
         * with no rule about which.
         */
        ui: "var(--ew-font-ui)",
      },

      /**
       * A fixed scale, not a ratio. Sizes are named for the job they do, so a
       * component cannot quietly promote a caption to a headline; the two fluid
       * steps are the only ones that respond to viewport width.
       */
      fontSize: {
        display: ["clamp(40px,5.4vw,66px)", { lineHeight: "1.02", letterSpacing: "-0.03em" }],
        title: ["clamp(32px,4vw,50px)", { lineHeight: "1.04", letterSpacing: "-0.03em" }],
        headline: ["30px", { lineHeight: "1.1", letterSpacing: "-0.02em" }],
        subhead: ["27px", { lineHeight: "1.14", letterSpacing: "-0.025em" }],
        lede: ["20px", { lineHeight: "1.5" }],
        prose: ["20px", { lineHeight: "1.62" }],
        standfirst: ["16px", { lineHeight: "1.55" }],
        row: ["16px", { lineHeight: "1.3", letterSpacing: "-0.01em" }],
        excerpt: ["14px", { lineHeight: "1.55" }],
        field: ["13.5px", { lineHeight: "1.3" }],
        meta: ["12.5px", { lineHeight: "1.45" }],
        micro: ["11.5px", { lineHeight: "1.4" }],
        label: ["11px", { lineHeight: "1", letterSpacing: "0.11em" }],
        "label-sm": ["10.5px", { lineHeight: "1", letterSpacing: "0.12em" }],
        kicker: ["10px", { lineHeight: "1", letterSpacing: "0.13em" }],
      },

      boxShadow: {
        pop: "var(--ew-shadow-pop)",
        panel: "var(--ew-shadow-panel)",
      },

      /**
       * Modernist is a zero-radius system and You.th is not, which is the
       * single largest visual difference between the two. Three steps only,
       * named for what they belong to rather than by size, so a component
       * cannot pick "the medium one" and drift:
       *
       *   `tile`  — anything showing a picture: feed tiles, lead images.
       *   `panel` — a block of content resting on the page: source lists,
       *             the dark explainer, the sign-up card.
       *   `field` — text inputs and textareas, softer than a panel so a form
       *             row does not read as a stack of cards.
       *
       * Actions are `rounded-full`, not one of these. A pill against a rounded
       * rectangle is what still separates a control from a card now that both
       * have corners.
       */
      borderRadius: {
        tile: "20px",
        panel: "22px",
        field: "14px",
      },

      keyframes: {
        // The category drawer, in from the left edge it is anchored to.
        "slide-in": {
          from: { transform: "translateX(-100%)" },
          to: { transform: "translateX(0)" },
        },
        // The toast, up into place rather than simply appearing — a message
        // that arrives without motion reads as something that was always
        // there and went unnoticed.
        "fade-up": {
          from: { opacity: "0", transform: "translateY(6px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        // The heart on a feed tile: swells past its size and settles, so a save
        // reads as having happened rather than as the colour quietly changing.
        "heart-pop": {
          "0%": { transform: "scale(1)" },
          "40%": { transform: "scale(1.35)" },
          "100%": { transform: "scale(1)" },
        },
      },
      animation: {
        "slide-in": "slide-in 0.22s ease-out",
        "fade-up": "fade-up 0.2s ease-out",
        "heart-pop": "heart-pop 0.35s ease-out",
      },

      // The comp's page frame: a 1240px measure inset 28px. The feed is a grid
      // and an article is a column, and they are not the same page width.
      //
      // `article` and `prose` were one value at 760px until articles grew to a
      // three-to-five minute read. They separated rather than both moving:
      // `prose` still frames /about, /join, /contact and /you, which are short
      // pages where 760px is right, while an article carries a longer headline
      // and a source panel. Widening the shared token would have quietly
      // restyled four pages that did not change.
      //
      // 800px and not more. The frame holds the headline, byline, verdict bar
      // and source panel, and the body now fills it (see PROSE_MEASURE) rather
      // than sitting in a 600px column inside it — so this number is the
      // article's one measure, and every px added to it lengthens the line.
      // 880 was tried as a frame around the narrower column and read as a
      // ribbon of text in a wide box; that complaint does not apply now the
      // prose fills the frame, but the character count does: 744px of type at
      // the body's 20px is ~111 characters, so widening this again means
      // raising `text-prose` with it.
      maxWidth: { page: "1240px", article: "800px", prose: "760px", console: "1040px" },
      spacing: { gutter: "28px", header: "68px" },
    },
  },
  plugins: [],
} satisfies Config;
