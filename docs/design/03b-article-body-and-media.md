# 3. Decisions made, with reasoning — §3.4, §3.4b, §3.4c


## 3.4 Article body as TipTap JSON, not markdown

`original_content` and `edited_content` are `JSONB` holding a TipTap document. Citations are a
first-class inline node (`{type: "citation", attrs: {sourceIds: ["S1","S3"]}}`), not a `[S1]`
string in prose. Three payoffs: the editor can render a citation as a clickable chip that
opens the sources panel; validation walks a typed tree instead of regexing prose; and
`original` vs `edited` diffs structurally.

The model still *emits* `[S1]` markers in a plain-text body — asking it for TipTap JSON would
burn tokens and invite malformed output. `services/tiptap.py::body_text_to_doc()` parses the
marker syntax into the document tree at the assembly step. Parsing failure is a validation
failure.

**Multi-source citations are accepted in both spellings — `[S1][S5]` and `[S1, S5]` — and the
marker pattern is defined once, in `domain/contracts.py`.** The comma form was originally
rejected, and the cost of that was a whole draft: the prompt's only example was a single handle,
so llama3.1:8b wrote `[S1, S5, S8]`, the body failed to render, and the article was written
`validation_failed`. That status never enters the review queue, so the run *looked* like nothing
had happened. Strictness at the parser is not free when the thing being parsed is generated:
rejecting a spelling nobody documented discards work rather than catching an error.

The single definition matters more than the widening. `body_text_to_doc()` and
`extract_handles()` both read markers, and they feed different things — the document tree and
`all_cited_handles()` respectively. If the extractor cannot read a form the parser renders,
`was_cited` is false for a source the article visibly cites and `check_beats_are_cited` reports
an uncited beat that is cited on the page. Validation would be measuring a document nobody is
looking at.

Malformedness is checked by *elimination*: strip every well-formed marker, and any bracket still
standing is a parse failure. The earlier "not a valid marker" pattern had to enumerate the ways
a marker can be wrong, and missed unterminated runs like `[S1, S5` — which matched nothing and
fell through as literal text, printing a broken marker into the finished article.

**Every nullable JSONB column uses `NullableJSONB`, not bare `JSONB`.** SQLAlchemy's JSON types
default to `none_as_null=False`, which persists a Python `None` as the JSON literal `null`
rather than as SQL `NULL`. That breaks the `COALESCE` above — a never-edited article renders as
JSON `null` instead of falling through to the draft — and it breaks `WHERE error IS NULL` for
every stage the orchestrator records as succeeded. Both were live: the first article written
after the schema reset had `jsonb_typeof(edited_content) = 'null'` and all six of its stage rows
had the same in `error`.

It stayed invisible because `json.loads('null')` is `None`, so every Python reader
(`display_content`, `ReviewService.approve`) took the correct branch through `or`. The defect
only appears in the SQL this document and `services/card.py` both name as the mechanism, which
is the worst place for it to first surface — enforcement that is documented but not actually in
force reads exactly like enforcement that works.

## 3.4b Article media: uploaded or generated, never linked

A picture reaches a draft two ways: a reviewer adds one from their own machine, or the
pipeline's `illustrate` stage draws one (§3.4c). A reviewer can also embed a YouTube video.
All are block-level TipTap nodes beside the beat paragraphs, so none disturbs the beats — those
are addressed by `attrs.beat`, and a picture above beat 1 does not change which paragraph the
feed card is derived from. The same holds for the section headings and paragraphs the pipeline
writes between beats 2 and 3 (§6): they carry no `beat` attribute, so however many of them
there are, `beat_text` still finds the three.

The two sources of a picture are not two code paths. A generated image goes through the same
`store_image`, produces the same content-addressed `src`, is built into a node by
`media.py::image_node`, and is re-checked by the same `assert_media_is_ours` on every autosave
and again at approve. Once it is in the document it is an ordinary picture: the reviewer can
resize it, re-wrap it, replace it or delete it with the controls that already exist.

Two rules, enforced in `services/media.py` and re-checked at approve:

**Images are uploaded, never linked.** `POST /api/console/media` stores the bytes and returns
the only `src` shape the server will accept back. A linked image breaks when the other site
reorganises and tracks the reader until it does, so the image node's `parseHTML` declines any
other `src` — a picture copied out of a web page is dropped, while the same picture *dropped
as a file* is uploaded and kept. What a file is is decided by its first bytes; the upload's
filename and `Content-Type` are read and discarded. **SVG is not on the allowlist**: it is a
script-bearing document, and one served from our own origin inside a published article is
stored XSS against every reader of it. PNG, JPEG, GIF and WebP cannot carry script.

**A video is an id, not a URL.** The document stores the eleven-character YouTube id and the
renderer builds the embed src from it, so no string anyone typed reaches an iframe intact. The
public page renders a still until it is pressed — an embedded player is about a megabyte of
Google's JavaScript executed on every reader of every article that has one, and mounting the
iframe on the click makes being tracked by YouTube something the reader chose.

Storage is content-addressed local disk — filename is the SHA-256 of the bytes, so uploads
dedupe, cannot collide, and nothing client-supplied reaches the filesystem. Served at
`/api/media/…` rather than a prefix of its own because the dev proxy and any deployment
already route `/api`. The S3 swap is that one module; orphan collection (an image dropped from
a draft leaves its file) is deferred with the rest of §11.

**Layout is two attributes, never CSS.** A reviewer can resize a picture or a video and wrap
prose around it — Word's "Square" — and the whole model is `width` (20–100, a percentage) and
`align` (`none` / `left` / `right`). Both are validated server-side beside the `src` check, on
autosave and again at approve.

The three things that follow from storing data rather than a stylesheet:

- **The same classes render both sides.** `mediaWrapClass()` in `lib/media.ts` produces the
  `.ew-media` classes that `styles/youth.css` defines, and the console editor and the
  published article both use them. That identity is the only way "the reviewer approves what
  publishes" is true of layout and not just of words.
- **It collapses on a phone.** The float and the width live inside one `@media (min-width:
  640px)` block, so under that every picture is a full-width block — a 40% float in a 320px
  column leaves about fifteen characters a line. An inline `style` could not express this,
  which is the concrete reason the document does not store one.
- **Nothing reviewer-authored reaches a reader as style.** The renderer turns one of three
  words into a class it already ships. There is no string a reviewer can supply that becomes
  CSS on a published page.

A percentage rather than pixels because the editor's column (~850px) and the article's measure
(76ch) differ, and a percentage is the only unit that means the same in both. No x/y
coordinates: media stay siblings of the beat paragraphs and are placed by being dragged
between them, which is what keeps `attrs.beat` — and therefore the derived card — meaningful.

In the editor the two nodes are React node views (`console/editor/MediaNodeView.tsx`). Worth knowing
before editing it: TipTap's React renderer wraps a node view in an element of *its own*, and
that element is the one in the editor's flow, so it is the one that has to float — hence the
`attrs` option on `addNodeView()` rather than styling the component's own root. Dragging the
corner writes the width straight to that element's custom property and commits a single
attribute on release, so one gesture costs one undo step and one autosave rather than sixty.

## 3.4c Generated illustration: two frames, one locked prompt builder, no claim

Every draft gets two pictures from `black-forest-labs/FLUX.1-schnell`, drawn in stage 5 by
`pipeline/steps/illustrate.py` and stored through the ordinary media store. A landscape **lead**
goes into `original_content` as a normal image node; a portrait **cover** lives only in
`articles.generated_imagery` and is what the feed tile shows. Two frames rather than one because
every shape `tileRatio()` produces is 1:1 or taller, so `object-cover` fits a landscape picture
to a tile by discarding its sides. §1's fifth rule survives through the pairing predicate
described there.

**Since 2026-09-30 the default is `IMAGE_COVER_MODE=crop`: one render, the cover cut from
it.** The landscape lead is rendered once, and the portrait cover is centre-cropped out of it to
3:4. At the default sizes that is 624×832, ample for a feed tile. That halves the cost to one
billed render per article, and it makes the two frames provably one photograph. A Regenerate
press in this mode always redraws both frames, for one render. `render` mode keeps the
two-generation behaviour described below. A local FLUX provider (mflux, 4-bit) was measured on
the target 16 GB M2 Pro and not built: 198 s and 18.4 GB of peak GPU memory for one 1216×832
image, and swap grew from 6.2 to 16.8 GB.

**In `render` mode the two frames are not the same photograph, and the docs said otherwise
until it was measured.** A seed initialises latent noise shaped like the frame, so one seed at two sizes is
two compositions — and the provider `auto` currently resolves to (`nscale`, reached through HF's
OpenAI-shaped images endpoint, which has no seed field) ignores seeding altogether: two
identical requests came back different on 2026-08-24. What survives is the useful half. Both
frames were drawn for one article under the same locked prompt builder, which forbids either
from asserting anything — so there is no composition the tile can promise and the article
withhold. The stronger guarantee is available if it is ever wanted: render portrait once and
crop the landscape with Pillow. It halves the cost, and the centred-subject-in-negative-space
style makes the crop safe. It was not taken because each frame composed for its own shape looks
better, and the weaker guarantee is sufficient for the rule it has to keep.

**Either frame can be redrawn on its own**, and the same argument is what licenses it. A
reviewer can like the picture in the prose and not the one on the tile; the two are seen in
different places and judged separately, so `POST /console/articles/{id}/illustration` takes a
`frames` list and draws only what was asked for, keeping the rest from the article's current
record. Nothing is written back until every requested frame has been drawn and stored — a cover
saved beside a lead that then failed is the one state `derive_card` cannot reason about.

The cost of that freedom is that provenance had to move off the pair and onto the frames.
`GeneratedImage` carries its own `prompt`, `negative_prompt`, `model` and `seed`; `Illustration`
is now just `lead` + `cover`. After a one-frame redraw the two genuinely come from two prompts,
and a single shared field would be a true record of one picture and a false record of the other
— which is worse than none, because it still answers when asked. Rows written before the split
keep their top-level fields and a `model_validator` pushes them down onto both frames on read,
so no migration is needed and no history is discarded; a frame's own value always wins.

**A generated picture must not become a claim.** This is the design constraint, and it drives
everything about `imagery/prompt.py`. An illustration of someone visibly healthier beside a
supplement asserts efficacy no citation backs; a clinic or a lab bench borrows authority the
article has not earned; a before-and-after pair states a result outright. None needs a caption
to be read as an argument, and an image is the register readers scrutinise least. So the prompt
is assembled here from fixed parts rather than written by a model, and the exclusions are
hard-coded.

Three consequences. **No second model call writes the prompt**: asking an LLM for a nice image
prompt reintroduces the ungrounded generation this pipeline exists to prevent, one layer down
and unvalidated. **The verdict never reaches it, and neither does the headline**, which is where
the verdict usually is — an illustration that looks hopeful for `supported` and bleak for `weak`
is a scoreboard drawn in pictures, composed before a human approved the verdict. And **the
exclusions ride in the positive prompt**: FLUX.1-schnell is guidance-distilled and runs at
`guidance_scale=0.0`, where a CFG-style `negative_prompt` has nothing to act on. It is sent
anyway, for the providers and checkpoints where it bites, but it is not what enforces any of
this — the positive prompt is.

**Locked does not mean identical, and the first version got that wrong.** It fixed the motif,
the camera angle, the arrangement and the light, leaving the subject noun as the only variable —
three words in eighty. On real renders that produced a feed where every article was white pills
on warm beige: the prompt described a space so narrow that different sampler noise still landed
in the same picture. The fix is not more randomness at the sampler, and cannot be — the provider
ignores `seed` entirely, so those renders already came from different noise.

So the prompt now separates **treatment from composition**. `TREATMENT` — palette, matte
surfaces, register — is fixed and applies to every render; framing, arrangement, light, surface
and the motif itself are selected from tuples by the seed. Exactly 5,184 combinations, all of
them the same magazine. Locking the treatment is what makes a feed read as one publication;
locking the composition as well is what made it read as one photograph.

**The motif is an axis, and it is the one that mattered most.** Each subject held a single
string, so every article the hint table called a `SUPPLEMENT` — which is most of a wellness
feed's vocabulary — rendered the literal phrase *"loose capsules, a small heap of pale powder
and a plain unlabelled glass jar"*. The four composition axes could not compensate, because
they only re-photograph whatever the motif named: five hundred angles on one still life still
read as one picture. `_MOTIFS`, `_PEOPLE_MOTIFS` and the default fallback therefore hold
`_MOTIF_RADIX` variants each, checked at import for the same reason the axis lengths are. Two
details are deliberate. It is the **slowest** digit — stride past all four composition axes — so
neighbouring seeds move the camera and distant ones change the objects, which keeps a seed
meaning "this article's composition" rather than "a reroll". And the article's noun moved to the
*head* of the sentence (`Still life of Ashwagandha KSM-66: …`) from a trailing `, suggesting
<noun>`; arriving after three concrete object nouns that had already specified the frame, it was
doing almost nothing, which is why creatine, ashwagandha and melatonin all drew the same jar.

**Two axes carry the tonal range, because the palette cannot.** `_SURFACES` varies material and
**value** — pale paper through to dark oiled wood — never hue, and `_LIGHTS` reaches from even
overcast through hard directional sunlight to near-dark. Five pale neutrals under four flavours
of soft daylight differ on paper and not at feed-tile size, especially on a four-step distilled
checkpoint; contrast and shadow depth are the only range available once hue is locked, and
locking hue is what the paragraph above is protecting.

**The strides are a mixed radix**, each the product of the axis lengths before it, so the axes
are exact digits of `seed % 5184` and every combination appears once per 5,184 consecutive
seeds. They were coprime odd numbers `(1, 7, 53, 401)` on the theory that coprimality *between
the strides* buys independence *between the axes*. It does not, and the failure is arithmetic
rather than statistical: with stride 7 against a 5-option axis, writing `seed = 35q + r` gives
`(seed % 5, seed // 7 % 5) == (r % 5, r // 7)` — 35 values of `r` landing on 25 pairs, so 10
framing×arrangement combinations came up exactly twice as often as the other 15. Measured over
400k random seeds: χ² 49073 on 16 degrees of freedom, commonest pair 23008 against 11272 for the
rarest; 511 on 499 under the radix. The quantity that matters is each stride against the product
of the *preceding axis lengths*, not the strides against each other. `_STRIDES` is therefore
derived from `_AXIS_RADIX` rather than written out, so it cannot fall out of step when someone
adds a seventh framing — as widening the axes from `(5, 5, 4, 5)` to `(6, 6, 6, 6)` proved,
since that change touched no stride at all.

That also gives the seed real work. It selects *our* prompt rather than the provider's noise, so
a retried run composes the same photograph and Regenerate composes a different one — the
property `seed_for_run` was written for and could not deliver while it depended on the provider
honouring it.

**A person may appear, on the one path where the subject is an activity.** The distinction is
depicting the subject versus depicting a result. A protocol *is* something someone does, so a
body mid-stretch on a mat or a forearm mid-lift photographs what the article is about and
asserts nothing beyond "this is the thing we looked at". A body beside a supplement jar, a bowl
of food or a tube of cream is the case the rule above describes: nothing in frame is doing
anything, so the only thing the person can be there to communicate is an outcome.

So people are gated on the motif — `_PEOPLE_MOTIFS`, currently `PROTOCOL` alone — rather than
switched on globally, and every other subject renders the still life unchanged. `DEVICE` is the
one with a real argument on both sides (a hand on a massager is use; a person glowing beside a
red-light panel is efficacy) and is deliberately left out until someone wants to make it. Two
guards come with the gate, both because being wrong here costs far more than a plain still life:

- **Only a confident signal opens it** — a reviewer's explicit `subject`, or a motif matched
  against `product` itself. A motif inferred from the *topic* does not, because topics name
  outcomes and `sleep` is a protocol word: "magnesium for sleep" would otherwise have put a
  body in a supplement article, which is precisely the refused picture. Uncertainty falls back
  to objects, which is always safe.
- **`EXCLUSIONS_WITH_PEOPLE` replaces the body ban rather than dropping it.** Read the two side
  by side: the clinic, text, branding and before-and-after clauses are identical, because none
  of them was ever about whether a person was in frame. What changes is that "no body" becomes a
  much narrower ban on the body being *displayed* — a physique, a transformation, eye contact
  with the camera. Those are the forms in which a person states a result, and they are what the
  blanket rule was actually buying.

The people path carries its own composition vocabulary (`_PEOPLE_FRAMINGS`, `_POSES`,
`_PEOPLE_SETTINGS`; `_LIGHTS` is shared) because the still-life words do not transfer — a
flat-lay of a human being is a mortuary photograph — but the same option counts, so the mixed
radix and the 5,184 combinations hold on both. `_PEOPLE_MOTIFS` carries the same number of
variants as every other motif tuple for exactly that reason. `TREATMENT` is shared unchanged, and that is what
keeps a yoga photograph in the same magazine as the pill still lifes instead of drifting into
stock fitness photography. The genre word moved out of `TREATMENT` to `_Path.genre` in the
process: `editorial still-life photograph` was a composition clause hiding in the treatment
string, and it flatly contradicted a motif with a person in it.

The motif is inferred from the article's text when no reviewer has classified the draft, which
is every pipeline call. **This is not a classification and must never become one**: it decides
which objects are photographed, is never displayed, and never writes `articles.subject`. The
asymmetry is what licenses it — a wrong subject is a confident false statement on the page, a
wrong motif is a slightly odd still life.

**Precedence between the hints is by field, not by position in the table.** `infer_motif_subject`
takes product, then topic and ingredients, and stops at the first field that matches anything.
Searching them concatenated — which is what it did — lets a vague word in a low-trust field
outrank a precise one in a high-trust field: `SUPPLEMENT` is checked last and `PROTOCOL` holds
`sleep`, `exercise` and `training`, which is how articles name their *outcome*. "Magnesium
glycinate" / "magnesium for sleep quality" and "Whey protein" / "protein for muscle after
exercise" both resolved to `PROTOCOL` and drew a towel and a timer. Topics routinely name an
outcome, so that was the ordinary path for supplements, and no reordering within the table could
have fixed it. It is also the tier the people gate above reads: only `product` is trusted enough
to put a body in frame.

**Failure is never fatal.** A picture is decorative and an article without one is publishable —
the feed already draws a typographic tile, which is the resting state of the design rather than
a degraded one. `IllustrateStage` catches everything and records a `cause` in
`pipeline_stage_runs.metrics`. This is the deliberate inverse of §4's throttle rule, and the
difference is visibility: recall lost to a 429 produces a more cautious verdict that reads as a
correct answer, whereas a missing picture is on the reviewer's next screen. Fail on the
degradations nobody can see.

**Cost is out of the ledger**, with embeddings and for a stronger reason: an image is billed per
render and `pipeline_stage_runs` prices four token columns. `record_usage` is never called here.

A reviewer who dislikes a picture has two moves, both ordinary: edit or delete it in the editor
like any other image, or redraw it with a fresh seed through
`POST /console/articles/{id}/illustration`. Three controls sit on that one route, each placed
where its effect is visible. **Regenerate picture** and **…and the tile** are in the editor
toolbar, beside Image, because that is where the article's own picture is. **Redraw tile** is in
the *Show draft* overlay, because the tile is only visible there — a button that spends money
and changes nothing on screen is a button reviewers press twice. The overlay offers it only when
the tile is actually showing a generated cover; with the pairing broken by a reviewer's own
picture, a redraw would succeed, bill a render and change nothing.

That route is the only console action that bills an external provider per press, so it sits
behind `security/spend_throttle.py` — a per-reviewer budget that rejects with `Retry-After`
rather than sleeping, checked before the render for the same reason the login throttle is
checked before the Argon2 hash. **It counts renders, not presses** (24 per ten minutes), so a
one-frame redraw costs half a two-frame one; charging the cheaper action at the expensive
action's rate is the wrong incentive on the one control that exists to stop needless spending.
The route also uses the article's `subject` when one is set, which the pipeline cannot:
`subject` is reviewer-set and the article row does not exist when `illustrate` runs.
