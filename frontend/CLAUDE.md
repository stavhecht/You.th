# Frontend

Vite SPA: public site and review desk.

Loaded when you work under this directory; the root [CLAUDE.md](../CLAUDE.md) (commands, invariants, map) still applies. The reasoning behind the design is in [docs/design/03a-frontend-auth-pipeline.md](../docs/design/03a-frontend-auth-pipeline.md) §3.1 and [03c-models-cost-visual.md](../docs/design/03c-models-cost-visual.md) §3.8.

## Theme

**The theme is `styles/youth.css`; `styles/design-system.css` is vendored and read-only.**
The second is Modernist, copied from the Claude Design project — retune it *there* and re-copy,
because hand-edits are lost on the next sync and that project is what the readme and the
component pages render from. `youth.css` declares the `--ew-*` tokens components speak and
re-points Modernist's `--color-*` roles at them, so the focus ring and `::selection` follow the
active theme. Every Tailwind value resolves to a custom property: a hex in a component is a bug,
because it cannot follow the theme and is invisible to anyone retuning the system. Two
deliberate exceptions, both because the surface underneath is a photograph nobody controls —
feed-tile text and `.ew-tile-scrim` are fixed white on fixed dark, and the verdict *mark* is
dropped on an image tile because ink is invisible on a scrim and a white version would be a
second mark meaning the same thing. `features/console/controls.ts` carries the same pill-and-
rounded-field shapes as the public side, on purpose: a reviewer crossing between the two
surfaces should not have to relearn what a button looks like, and that file's own rule is that
those three controls staying consistent is a safety property.

## Structure and route groups

**Frontend is a Vite SPA shaped for a cheap Next.js port.** Keep all data access in
`src/lib/api/*` framework-agnostic (plain fetch + typed contracts), keep `src/routes/`
mirroring a Next `app/` tree one-for-one, and keep `window` out of render paths above the
route level.

**Two route groups that share only `App.tsx`.** The public site (`/`, `/a/:slug`, `/about`,
`/join`, `/signin`, `/forgot-password`, `/you`, `/contact`, `/privacy`, `/terms`, `/accessibility`) renders under `PublicLayout` — site bar, category drawer, mobile
tab bar. The review desk is at **`/review`**, is lazy-loaded, and renders `ReviewHeader`
instead. Nothing public links to it. That is chrome and discoverability, *not* access control:
a URL is not a secret, and what actually keeps readers out is `RequireAuth` plus the
router-level bearer check on every `/api/console/*` route. Public routes must never import
from `features/console`, and the desk must never render public chrome.

**The desk's "Show draft" preview imports the other way, on purpose.** `FeedPreview.tsx`
renders the *real* `features/feed/ArticleCard` among real published tiles at the feed's own
200px measure (`MasonryFeed`'s `columnWidth`), fed by `GET /console/articles/{id}/card` — which
runs the *same* `derive_card()` `approve()` runs. Both halves are deliberate: a console-local
tile clone, or a TypeScript port of `derive_card`, would be a second definition free to drift,
and a preview that drifts from what publishes is worse than none because it is reassuring
rather than useful. Console → feed is the permitted direction; the reverse is what the boundary
forbids. Every tile in the overlay is `inert` — `ArticleCard` renders a live Save button and
links to `/a/:slug`, which 404s until publication. The button flushes autosave before opening,
for the same reason Approve does.

**The review desk's "Need to be checked" tab is derived in the browser** (`features/console/
checks.ts`, rendered by `WhyItFailed.tsx`): the evaluation's per-case failure list, rebuilt from
what a live draft carries — validation report, source stances, and each cited sentence beside
its sources' abstracts (`ReviewSourceOut.abstract`, the only API addition). Retrieval precision
and the judge's hallucination call stay eval-only: they need a gold list and a model call per
sentence. Four things to keep: `article_sources` is one row per paper **per claim**, so checks
count *handles*, not rows; the "Not in the cited source" hint is a word match (numbers as whole
tokens, topic words by 5-letter stem, a stoplist for "found"/"after") — a pointer for the
eye, never a verdict; the "fixed" ticks are session-only reviewer bookkeeping keyed by sentence
text, so both tabs stay mounted (`hidden`) to keep them; and a new Tailwind colour token needs
`docker compose restart web`, because Vite does not reload `tailwind.config.ts`.
Failed-check text is `text-danger-ink` (`--ew-danger-ink`), the one red in the theme.

## Navigation

**The browse drawer offers subject only.** It listed the four verdicts for a while and they
were removed deliberately: a standing menu of Supported / Mixed / Weak / No evidence is a
scoreboard, and offering it as a primary way into the feed invites browsing by score rather
than by subject — the framing the content rules exist to avoid. `?verdict=` is still a real
narrowing and still works from a shared link; the navigation just does not propose it.

**Phones get no header nav** (below `sm`): About and Sign in are hidden there, so the tab
bar's last tab is **Sign in** for a guest (You when signed in), and search is a 36px button
that opens across the bar on focus (`SEARCH_FORM`). **The contact page is the general form
only; "Suggest a topic" lives on `/you`**, for signed-in readers (the API refuses it signed
out). The review desk's bar keeps the mark centred below `sm`, with a smaller mark and the tag
wrapped to two lines, and drops the reviewer's name and the public-feed link (`WIDE_ONLY`).

## Editor block types

**A new block type has to be declared in the console editor's `StarterKit`, or it is silently
destroyed.** TipTap drops nodes its schema does not know: with `heading: false` the editor
parsed a sectioned draft, discarded every heading, and the first autosave wrote an
`edited_content` with the article flattened — nothing about the saved document otherwise wrong.
This is the same failure `BeatAttribute.ts` exists to prevent for `attrs.beat`. The public
renderer needs a case for it too (`ArticleContent.tsx`), or it falls through and renders as a
paragraph.
