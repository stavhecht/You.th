# Imagery: the generated illustration

How the image prompt is built and why it can never make a claim.

Loaded when you work under this directory; the root [CLAUDE.md](../../../CLAUDE.md) (commands, invariants, map) still applies. The reasoning behind the design is in [docs/design/03b-article-body-and-media.md](../../../docs/design/03b-article-body-and-media.md) §3.4c.

## Rules

**Image generation never fails a run, and that is the deliberate opposite of the throttle
rule.** A picture is decorative; an article without one is publishable and already renders as a
typographic tile. `IllustrateStage` catches every image failure, records a `cause`
(`not_configured` / `disabled` / `provider_error` / `unexpected`) in `metrics.illustrate`, and
returns the context untouched. Compare `retrieval/throttle.py`, which must fail hard because
recall lost to a 429 reads as a correct, cautious answer. The rule is *fail on the degradations
nobody can see* — a missing picture is visible on the reviewer's very next screen.

**The image prompt is assembled here, never by a model, and the verdict never reaches it.**
A picture of someone looking healthier beside a supplement is an efficacy claim no citation
backs, made in the one register readers do not read critically. The headline is excluded for the
same reason: it is a *claim sentence*, and an image model handed a claim tries to depict it.

**A person may be in the frame, on the one path where the subject is an activity.** The line is
depicting the subject versus depicting a *result*: a protocol **is** something someone does, so
a body mid-stretch on a mat or a forearm mid-lift is a photograph of what the article is about.
A body beside a jar, a bowl or a tube is the other case — nothing in frame is doing anything, so
the only thing the person can be communicating is an outcome. So people are gated on the motif
(`_PEOPLE_MOTIFS`, currently `PROTOCOL` alone) rather than switched on globally, and adding a key
there is a decision about claims, not styling. Two guards go with it. **Only a confident signal
opens the gate** — a motif matched against `product` itself (a reviewer's category maps to
no `PROTOCOL` motif, so it cannot open the gate on its own); a motif
inferred from the *topic* does not, because topics name outcomes and "magnesium for sleep"
matches `sleep`. And `EXCLUSIONS_WITH_PEOPLE` replaces the body ban rather than dropping it: the
clinic, text, branding and before-and-after clauses are **identical** in both, because none of
them was ever about whether a person was present. What is newly forbidden is the body being
*displayed* — a physique, a transformation, eye contact with the camera — which is the form the
efficacy claim takes when a person makes it.

**`imagery/prompt.py` splits on one line: treatment locked, composition free.** `TREATMENT`
(palette, matte surfaces, register) is fixed and applies to every render, both paths — that is
what makes twenty articles read as one publication, and it is also what stops the people renders
from becoming stock fitness photography. Framing, arrangement, light and surface are chosen by
the **seed** from a per-path vocabulary (`_FRAMINGS`/`_ARRANGEMENTS`/`_SURFACES` for objects,
`_PEOPLE_FRAMINGS`/`_POSES`/`_PEOPLE_SETTINGS` for a person, `_LIGHTS` shared), giving 1,296
compositions either way. Do not add a composition clause to `TREATMENT`; that is exactly how this
module once produced the same beige photograph for every article, with three words of eighty
varying — and the genre word (`Still life` / `Unposed documentary photograph`) was moved out to
`_Path.genre` for that reason, being composition hiding in the treatment string.

**The motif is a fifth axis, and swapping it is the only change with real amplitude.** Each
subject held one string, so every article the hint table called a `SUPPLEMENT` — most of a
wellness feed — rendered the same jar of capsules, and the composition axes could only
re-photograph it: five hundred angles on one still life read as one picture. `_MOTIFS`,
`_PEOPLE_MOTIFS`, `_DEFAULT_MOTIFS` and `_DEFAULT_MOTIFS_UNNAMED` now hold `_MOTIF_RADIX` (4)
variants each — **every tuple the same length, checked at import**, or the digit stops being
uniform across subjects — for 5,184 prompts (`_PERIOD`). It is deliberately the **slowest**
digit (`_MOTIF_STRIDE` is past all four composition axes), so neighbouring seeds move the camera
and distant ones change the objects; a seed is an article's stable composition, not a reroll.
The noun also moved to the head of the sentence (`Still life of Ashwagandha KSM-66: …`) from a
trailing `, suggesting <noun>` — arriving after three concrete object nouns that had already
specified the frame, it was doing almost nothing. The default-motif path passes no head noun,
because that scene already names it.

**A product that names no motif gets a scene for what the article is about**
(`_OUTCOME_SCENES`, 2026-10-23): ten areas (gut, back, heart, brain and mood, sleep, skin,
joints, longevity, weight and blood sugar, immunity), each `_MOTIF_RADIX` object-only variants,
picked by hint words in `product`, then in the claims (`build_prompt(outcome=...)`), never the
topic. Only the scene's own words reach the prompt, never the claim, and no head noun is used,
since "cancer survivorship care" at the head invites a clinical picture. Before it, "Back pain"
fell through to "objects associated with back pain" and FLUX drew pharmacy bottles with garbled
labels. A product motif still wins (a magnesium article keeps its jar).

**Tonal range comes from `_SURFACES` and `_LIGHTS`, never from hue.** Surfaces vary material
*and value* (pale paper through dark oiled wood); lights run from even overcast through hard
directional sunlight to near-dark. The original five pale neutrals under four flavours of soft
daylight were nearly a constant at feed-tile size on a four-step distilled checkpoint. This is
the only range available once the palette is locked — which it stays.

**The strides are a mixed radix, not coprime numbers.** Each is the product of the axis lengths
*before* it (`_STRIDES` is derived from `_AXIS_RADIX`, so it cannot drift when someone adds a
seventh framing — widening `(5, 5, 4, 5)` to `(6, 6, 6, 6)` touched no stride), which makes the
axes exact digits of `seed % 5184` — every combination once per 5,184 consecutive seeds. The previous `(1, 7, 53, 401)` was chosen for coprimality between the
strides, which buys nothing: stride 7 against a 5-option axis gives
`(seed % 5, seed // 7 % 5) == (r % 5, r // 7)` for `seed = 35q + r`, so 10 framing×arrangement
pairs came up exactly twice as often as the other 15 (χ² 49073 on 16 dof over 400k seeds; 511 on
499 after). The quantity that matters is each stride against the product of the preceding axis
lengths — which is also why both paths must keep the same option counts, checked at import, and
why the motif digit's stride is the product of all four axes rather than a number that looked
big enough. Sampling seeds is no longer an adequate content check at 5,184 combinations, so
`test_no_option_in_any_vocabulary_names_a_claim_shaped_thing` checks every string in every
vocabulary directly: it fails when a banned word is *written*, not when a seed selects it.

**The seed selects the prompt, not the sampler, and that distinction is the whole design.** The
provider ignores the seed it is sent (measured — see `seed_for_run`), so randomising harder
cannot help: those identical renders already came from different noise. The prompt was the
narrow thing. `seed_for_run` therefore gives an article a stable composition across retries and
`fresh_seed` makes Regenerate genuinely recompose, both by our own selection.

Three traps when editing. `subject` is `None` on every pipeline call (reviewer-set; the article
row does not exist yet), so `infer_motif_subject` — a keyword map over product, then topic and
ingredients — picks the motif. **That is not a classification**: it chooses which objects get
photographed, is never shown to a reader, and never writes `articles.subject`, which stays
reviewer-set because a wrong subject is a false statement on the page while a wrong motif is a
slightly odd still life. **Its arguments are in trust order and it stops at the first field that
matches**, which is load-bearing rather than tidy: it used to join product + topic + ingredients
into one haystack, so a vague word in a low-trust field outranked a precise one in a high-trust
field. `SUPPLEMENT` is checked last and `PROTOCOL` holds `sleep`, `exercise`, `training` and
`therapy` — which is how articles name their *outcome* — so "Magnesium glycinate" / "magnesium
for sleep quality", "Melatonin" / "melatonin for sleep" and "Whey protein" / "protein for muscle
after exercise" all drew a towel and a timer. Since topics routinely name an outcome that was
the common path for supplements, not an edge case. Within-list order cannot fix it; only the
field tiering can. **`negative_prompt` is close to a no-op on FLUX.1-schnell** — it is
guidance-distilled at `guidance_scale=0.0`, so the exclusions ride in the *positive* prompt and
must stay there; the tests fail if they are "tidied" into the negative channel. And the
exclusions are deliberately narrower than they look — *branded* packaging, and no ban on
numbers — because several motifs name a jar, a tube or a timer, and a blanket ban contradicted
them on every render.
