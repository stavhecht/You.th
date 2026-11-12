# 6. Article format

Primary output is the on-tap article: a three-to-five minute read, ~700–950 words where the
evidence supports it. A three-beat spine — (1) what it claims → (2) what the research shows →
(3) bottom line / caveat — with up to five titled **sections** between beats 2 and 3 working
through the evidence in detail.

The spine stayed three named fields when sections arrived rather than becoming a list, because
three things address the beats by name: `derive_card` reads beat 1 for the feed excerpt,
`check_beats_are_cited` reads beat 2, and `PersistStage` places the generated lead image above
beat 1. Section paragraphs carry no `attrs.beat` — a section is a sibling of the beats, exactly
as a reviewer-added paragraph is, and must not answer to `beat_text`.

**Length is a ceiling, not a floor, and this is where that is easiest to lose.** Sections have
**no minimum**. A `min_length` would be a padding instruction: an article resting on one small
trial would be obliged to invent two more things to say about it. Thin evidence should still
produce a short honest article — one or two sources means no sections at all, and the beats
alone. The prompt states the shape by evidence volume; nothing enforces a floor, and nothing
should.

Bounds are enforced structurally, per field, not as a global word count:

| Field | Bound | Enforced |
|---|---|---|
| `headline` | ≤ 12 words | Pydantic validator |
| `summary` | ≤ 2 sentences | Pydantic validator |
| `body.beat_1_claim` | ≤ 4 sentences | Pydantic validator |
| `body.beat_2_evidence` | ≤ 5 sentences | Pydantic validator |
| `body.beat_3_bottom_line` | ≤ 5 sentences | Pydantic validator |
| `body.sections` | 0–5 sections | Pydantic `max_length` |
| `body.sections[].heading` | ≤ 8 words | Pydantic validator |
| `body.sections[].body` | ≤ 8 sentences | Pydantic validator |
| `verdict` | one label + optional one-clause qualifier | enum + ≤ 15-word qualifier |

A section's `body` may hold blank-line-separated paragraphs; `tiptap.py` splits them into
sibling paragraph nodes. Its `heading` renders as an `h2` and is a **label, never a claim** —
"Dose and duration", not "Magnesium improves sleep" — so a heading cannot assert more than the
sources support, and citation markers are not allowed in one.

Every article renders with an "informational, not medical advice" disclaimer. It is a
render-time constant in a shared layout component, not model output — the model cannot forget
it, reword it, or drop it.

## Register: written for a phone, not a journal

Two rules in the synthesis prompt's Framing section, both aimed at a reader with no medical
training who is using the app between other things.

**No em dashes or en dashes**, in any field including the headline and section headings. They
read as academic, and a comma, colon, full stop or bracket always serves. Ranges go in words
("four to eight weeks").

**Every technical term and acronym is explained the first time it appears**, either in brackets
straight after it or in one or two short sentences, with the words spelled out before the short
form is used ("randomised controlled trial (RCT)", then "RCT"). A term needing more than two
sentences does not belong in the article at all; say the finding in ordinary words instead.

**What made the dash rule stick is worth knowing before editing this file, because the obvious
approach failed.** The prompt itself contained 18 em dashes while instructing against them, and
a local model mimics the register it is handed far more reliably than it follows a rule about
it. Removing them from the prompt took em dashes in output to **0 of 6 runs**. The same lever is
why `STUDY_TYPE_LABELS` carries a plain-English gloss on every entry rather than a bare label:
telling the model to explain "meta-analysis" did nothing, because the bare term was printed on
every source line it read. Do not "tidy" those glosses back to bare labels, and do not
reintroduce an em dash here.

Measured on `llama3.1:8b`, six runs, the same 12 sources: dashes eliminated outright, and the
glossed labels lifted inline citations from 3.2 to 5.0 handles per article with section counts
holding. **The term-explanation rule only half-took**: "RCT" is now usually spelled out, but
"systematic review" and "meta-analysis" still appear unglossed in most runs. That is the local
model's instruction-following ceiling, not a missing rule — the prompt is at ~1,500 words and
additions past this point measurably trade against each other (an earlier four-line addition
about `summary` halved section output). Reach for a larger `OLLAMA_SYNTHESIS_MODEL` before
reaching for more prompt.
