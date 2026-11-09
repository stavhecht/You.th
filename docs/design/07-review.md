# 7. Human-in-the-loop review

States: `pending_review` → `published` | `rejected`, plus `validation_failed` as a terminal
pre-queue state and `draft_failed` for pipeline errors.

`validation_failed` → `rejected` is also allowed, and it is the console's "discard" action.
Since there is no delete, rejection is the only way a draft leaves a queue tab; without this
edge, failed drafts have no available action at all and accumulate forever in the tab that
exists to make prompt regressions visible. They are still never approvable — the discard path
records a reason, it does not route around invariant #2.

The review screen is the editor and the sources panel side by side:

- **Sources panel** lists each paper's title, journal, year, study type, and a direct DOI/PMID
  link, grouped by the claim it backs. Weak study types are flagged inline, so "this confident
  verdict rests on two cell-culture studies" is visible without opening anything.
- **Validation badge** shows `4/4 citations resolve` — computed at draft time and stored on
  the article, not recomputed in the browser.
- **Editor** is TipTap with autosave (debounced 800ms) writing to `edited_content`.
  `original_content` is never touched, giving a clean audit trail of what the model wrote
  versus what went live.
- **Subject picker** is the one field a reviewer *adds* rather than checks. It sits above the
  decision block, not inside it: it is metadata that stays editable after publication, and
  grouping it with Approve would imply it is part of the irreversible act.

Publishing sets status, `reviewed_by`, `reviewed_at`, `published_at`, and materialises the
derived card fields — headline, excerpt and image — in the same transaction. The public feed
only ever queries `status = 'published'`.

**The publish control names its destination.** It reads "Publish to the public feed", says the
article will land at `/a/{slug}` recorded against the reviewer's name, and links there once it
is live. This is the only path to `published` in the system and it puts the article in front of
every reader; a button whose label is an abstract state change understates what is being
decided.
