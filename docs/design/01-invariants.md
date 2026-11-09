# 1. The four invariants

These are not conventions — each is enforced by a specific mechanism, named here so a reviewer
can check the mechanism rather than trust the prose.

| # | Invariant | Enforced by |
|---|---|---|
| 1 | Nothing publishes without a human | No code path from the pipeline reaches `published`. The pipeline's terminal write is `pending_review`; `published` is set only by `ReviewService.approve()`, which requires an authenticated `reviewer_id`. DB `CHECK` constraint requires `reviewed_by`/`reviewed_at` to be non-null when status is `published`. |
| 2 | Every citation is real and was in the prompt | `evidence/validation.py` runs after synthesis, before persistence. It checks each emitted `S`-handle against the exact handle set built for that prompt, then that each maps to a source row with a resolvable PMID or DOI. A draft that fails is written as `validation_failed`, not `pending_review`, and never enters the queue. |
| 3 | Evidence grade **and quantity** cap verdict confidence | `evidence/grading.py::max_verdict_for_claims()`. Two questions: how good the best cited source is, and how many there are. A `supported` verdict backed only by in-vitro/animal sources is a **validation failure**, not a style note — and so is one resting on a single trial, since `supported` needs two sources at a supported-tier grade, **per claim**. The cap is applied twice: as an instruction in the synthesis prompt, and as a hard post-check the model cannot talk its way past. |
| 4 | The AI draft is immutable | `articles.original_content` is written once by the pipeline and never updated (enforced by a DB trigger). Human edits write to `edited_content`. The public feed renders `COALESCE(edited_content, original_content)`. |

A fifth, structural rather than ethical: **the feed card is derived, never generated.**
`services/card.py::derive_card()` computes headline + verdict badge + first sentence of beat 1
from the article body. There is no separate card generation call, so card and article cannot
contradict each other.

The generated cover (§3.4c) is the one thing on a card that is not literally in the body, and
it is held to the same statement by a pairing rule rather than exempted from it: `derive_card`
uses the portrait frame **only while the document's first image is still the landscape one it
belongs to**. The tile's picture was drawn for the article the reviewer is publishing, or the
card falls back to the body — it is never an independently chosen image. Note how narrow the
claim is: the two frames are two renders of one subject, not one photograph cropped twice, and
a reviewer may redraw either alone, so they need not even come from one generation (§3.4c).

**Where to check each one:**

| Invariant | Implementation | Test |
|---|---|---|
| #1 human-in-the-loop | `services/review.py::approve` — the only assignment of `published` in the codebase | `test_publish_path.py` (AST guard), `test_db_invariants.py` (CHECK constraint) |
| #2 grounded | `evidence/validation.py::validate_draft` | `test_invariants.py` — hallucinated handle, unresolvable source, uncited beat |
| #3 evidence caps verdict | `evidence/grading.py::max_verdict_for_claims` | `test_invariants.py` — including that an *uncited* strong source cannot raise the ceiling — and `test_evidence_quorum.py` for the two-source rule and per-claim scoping |
| #4 immutable draft | DB trigger `articles_original_content_immutable` | `test_db_invariants.py` |
| card derived | `services/card.py::derive_card` | `test_content.py` — card verdict always equals article verdict; the generated cover is dropped when the lead image is replaced, removed, or displaced |
