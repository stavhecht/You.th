# Domain: contracts, models, article body

`contracts.py` is the spec, its docstrings are prompt text, and the body is TipTap JSON.

Loaded when you work under this directory; the root [CLAUDE.md](../../../CLAUDE.md) (commands, invariants, map) still applies. The reasoning behind the design is in [docs/design/03b-article-body-and-media.md](../../../docs/design/03b-article-body-and-media.md) and [05-ai-contracts.md](../../../docs/design/05-ai-contracts.md).

## `contracts.py` is the spec

**`domain/contracts.py` is the spec.** Every pipeline stage's input and output is a Pydantic
model there, and `ExtractionOutput`/`SynthesisOutput` double as the structured-output schemas
the LLM generates under. If a stage's shape isn't in that file, it isn't defined. Field bounds
(sentence counts, claim caps) are validators there rather than a global word count, so thin
evidence yields a short article instead of padding.

## A docstring is prompt text

**A docstring in `domain/contracts.py` is prompt text.** Pydantic uses a class docstring as the
JSON-schema `description`, and `ollama_client.py` passes that schema in as the **generation
grammar** — so every word written there to explain a decision to a maintainer is read by the model
while it decides what to write. The docstrings argued for brevity, correctly and at length:
*"every bound here is a ceiling, not a target"*, *"optional and have no minimum — that is the
whole point"*, *"nothing in this system pads to length"*, and `sections` was described to the
model as *"Optional titled sections expanding on the evidence"*. Two of the first three articles
carried no sections at all.

Both audiences are legitimate and they are not the same one. The rationale stays in the
docstrings; `model_config = ConfigDict(json_schema_extra={"description": ...})` overrides what
reaches the model. `sections` still has **no `min_length`** and must never grow one — that would
be the padding instruction §6 refuses — but its description now states the prompt's own
evidence-to-count rule, whose low end is *none at all*, so thin evidence still yields a short
article. `test_the_generation_grammar_carries_no_developer_rationale` fails when a file name or a
brevity argument reaches the grammar again. This is the same lever as the `STUDY_TYPE_LABELS`
glosses: a local model mimics what it is handed far more reliably than it follows a rule about it.
The synthesis prompt itself opened with "You write short, evidence-checked articles" until
2026-09-29; the word went for the same reason. Its effect is not measured yet.

## Nullable JSON

**Nullable JSONB columns use `NullableJSONB` from `domain/models.py`, never bare `JSONB`.**
SQLAlchemy defaults to `none_as_null=False`, so a Python `None` persists as the JSON literal
`null`, not SQL `NULL`. `COALESCE(edited_content, original_content)` then returns JSON `null`
for an unedited article, and `WHERE error IS NULL` matches no succeeded stage. Python readers
hide it — `json.loads('null')` is `None`, so `edited_content or original_content` is correct —
which means it only breaks in the SQL that DESIGN.md §3.4 and `services/card.py` document as
the enforcement of invariant #4. Both columns were shipping JSON `null` before this was caught.

## Article body and citation markers

**Article body is TipTap JSON, not markdown.** The model emits plain text with `[S1]` markers;
`services/tiptap.py::body_text_to_doc()` parses them into typed citation nodes at assembly.
Parse failure is a validation failure. **`CITATION_MARKER_PATTERN` in `domain/contracts.py` is
the only definition of a marker** — `CITATION_RUN_RE` beside it is built from it (the renderer
splits on it and SYNTHESIZE counts coverage by it), and `extract_handles()` uses it directly. Do not restate the pattern in either place: the parser and
the extractor disagreeing means `was_cited` is false for a source the article visibly cites, and
`check_beats_are_cited` flags a beat that is cited on the page. Both `[S1][S5]` and `[S1, S5]`
are accepted and collapse into one citation node; ranges (`[S1-S8]`) are not, inline. Anything
bracketed that is not a marker is a parse failure — checked by stripping valid markers and
looking for leftover brackets, because the old "not a valid marker" pattern missed unterminated
runs like `[S1, S5` and let them through as literal text. Validation walks the tree rather than regexing prose.
**Sources are full of square brackets, so the model is shown them round.** Measured 2026-09-29: a
draft copied `95 % CI = [-3.83 to -0.55]` out of an abstract into beat 2 and was discarded.
`render_source_block` turns every square bracket in a title, abstract or excerpt round (`_round`),
and the prompt says square brackets are for handles only. Only the rendering changes. The stored
text is the source's own. The parse runs in VALIDATE (`check_body_parses`), not only in PERSIST, so
`metrics.validate.passed` is true only for a draft that will actually be stored as
`pending_review`. Before this, PERSIST added the failure after VALIDATE had recorded `passed:
true`. The failure's `detail` holds the offending block's raw text, because a draft that does not
parse is stored with an empty document.

**The pipeline writes three block types and reviewers can add two more.** `paragraph` and
`heading` (always level 2, one per `ArticleBody` section — DESIGN.md §6) come out of
`body_text_to_doc`, plus the `image` node PERSIST hands it; a reviewer adds `image` and
`youtube` (§3.4b). Only the three beat paragraphs carry `attrs.beat`. Section paragraphs and
reviewer-added ones deliberately do not, which is what keeps `beat_text()` correct however much
sits between the beats — it addresses by `attrs.beat`, never by position.
