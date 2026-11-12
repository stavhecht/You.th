# Social listening, products, intel

The third and fourth proposers (`app/social`, `app/products`, `app/intel`). Notes for all three live here.

Loaded when you work under this directory; the root [CLAUDE.md](../../../CLAUDE.md) (commands, invariants, map) still applies.

## Rules

**Social listening and product discovery (`app/social`, `app/products`, `app/intel`) are two
more proposers, beside the trend search and the MeSH scan, not inside them** (2026-11-04,
migration `0002_social_products.sql`). Nothing in `app/research` or `app/discovery` imports
them (`test_social_scope.py`), the trend search's output fields and `trend-v1` weights are
pinned by that test, and neither module ever enqueues an article. What to know:

- **YouTube Data API only; no scraping, by decision.** Instagram is reported `NOT_CONFIGURED`
  (needs a Business account and Meta app review for Public Content Access; 30 hashtags a
  week) and TikTok `UNAVAILABLE` (its Research API is academic-only) on every run, with the
  reason. A new platform is a client beside `social/youtube.py` plus an entry in
  `collect.py::platform_logs`; scraping vendors were considered and refused.
- **Collection runs are a queue the worker claims** (`collection_runs`, kinds
  `social_daily | social_weekly | product_discovery`, one active per kind by a partial unique
  index; since 2026-11-11 also `retraction_sweep`, which is not social: the weekly re-check of
  cited papers rides this queue because the worker already claims, heartbeats and sweeps it,
  see [retrieval/CLAUDE.md](../retrieval/CLAUDE.md)), after research runs and before article runs, with the research run's heartbeat and
  stale sweep. In the worker because the reader's model calls must never share the GPU with a
  pipeline run. `social/schedule.py` runs in the API lifespan and only *enqueues*: due-ness
  from the last completed run of each kind (23 h, ~7 days), and a failed kind is not retried
  for 2 h. `POST /console/collection/runs` and `POST /automation/collection/runs` (n8n token)
  queue the same rows.
- **Statuses: `SUCCESS | EMPTY | UNAVAILABLE | RATE_LIMITED | ERROR | NOT_CONFIGURED`**
  (`social/contracts.py::SourceLog`), per platform or source, on `collection_runs.collection_status`.
  Only SUCCESS and EMPTY count as a day of coverage. `detail` says why a source was never asked
  and is dropped once it was; a product run's web and openFDA read EMPTY, "not asked", when no
  product needed them, since NOT_CONFIGURED would suggest a missing key. A spent quota or rejected key stops the
  source for the run (`log.stopped`).
- **The daily run makes no model calls**: refresh counters of posts published within
  `social_track_days` (`videos.list`, 1 unit/50; a video the API no longer returns is deleted),
  search a rotating watch list (research seeds + product categories + recent social topics) at
  `social_youtube_max_searches_per_day` (100 units each), tag new posts with known topics by
  their words, prune. The rotation is stateless (the day picks the
  slice) and every search covers the whole `social_track_days` window, newest first: a look-back
  sized to the rotation left gaps whenever the watch list changed length and shifted the slices,
  while those days still counted as covered. **That quota is
  the trend search's too**: if social spends it, the trend search's YouTube source fails and its
  data coverage drops — a trend-score change caused from outside. Keep the sum well under 10,000.
- **Measured live 2026-11-04/05:** broad seeds fill the 50-result page, so their upload counts
  are floors; the run records `truncated_queries` and the result says so. On 2026-11-05 **all 15
  searches filled it** (738 new posts, most from the last 36 h): counts are a sample of each
  word's newest uploads, not totals. Daily run 31 s, ~1,515 quota units; weekly and product
  discovery ~27 min each, all of it the reader (10 batches at ~2.8 min).
  **The reader's budget sets the run's length, not the search count**, and it reads ~200 of
  ~5,000 posts a week. That is sampling by design: the word tagger still counts unread posts
  for known topics, so the reader's job is finding new topics and products. To shorten a run,
  lower `SOCIAL_MAX_READING_BATCHES`.
- **Every reading of a post's counters is a new `social_post_observations` row**; NULL is "the
  platform did not say" (hidden likes, comments switched off), never 0. **YouTube allows 30 days
  of storage**: posts not refreshed for `social_retention_days` (28; validator: 21–28, since the
  prune runs only with the daily collection and at 30 a post could reach almost 31) are deleted
  and cascade to observations, topic links and product mentions; `text_readings` go too.
  Topics, products and candidates' computed numbers stay. The prune runs **first** and commits alone, so a
  crash later in the run cannot skip it day after day and turn a bug into a storage-policy breach.
- **One model call reads texts for both modules** (`intel/reader.py`): the wellness topic and
  any named products, in batches of 20, at most `social_max_reading_batches` per run, cached
  in `text_readings` by sha256(`READER_VERSION` + text) so an unchanged text is never read
  twice, and **committed after each batch**, so a restart loses at most the batch in flight
  rather than up to ~27 minutes of model time. Posts are offered **newest first**: at default volume (hundreds of new posts a day,
  ~200 read a week) an unordered query spent the budget on three-week-old posts. Checked, never
  trusted: an id must be one sent; a topic's subject must share a word
  with the text; a product's quote must be copied from the text and contain the brand. A word
  filter (`worth_reading`) drops texts with no wellness or product words before the model.
  `ProductCategory` and `MentionKind` have no docstrings (grammar). Measured 2026-11-04 on
  Bonsai, 14 labelled texts: 14/14 on every metric, 130 s — a small set written by the
  implementer, so it shows the reader works, not how accurate it is.
- **Analytics are pure** (`social/analytics.py`, `social-v1`): three equal 7-day windows ending
  now; a window counts only with ≥5 days of SUCCESS/EMPTY daily collection, else every count
  in it is None. Popularity (median views a day) and growth (uploads and *distinct creators*,
  `(c+5)/(p+5)`) are separate fields; velocity needs two readings ≥12 h apart; engagement
  leaves out posts with a hidden count and reports how many it rests on. Momentum
  (`rising | steady | declining | insufficient_history`) is an assessment stored apart from the
  metrics, and there is **no composite social score**. The first two weeks of a deployment
  are `insufficient_history` by design; nothing is backfilled.
- **The weekly run** reads new posts, links topics (`social_topics`, keyed by the trend search's
  own `topic_key`, kept separate from `research_topics` so social activity cannot touch the
  trend search's history), records product mentions, **re-tags every post in the window by the
  word rule** (`tag_known_topics`), measures, and proposes topics with
  ≥`social_min_posts` this week. The re-tag is what makes weeks comparable: daily tagging only
  knows topics that already exist, so without it a topic found this week counted every matching
  post from then on but only the model's sampled reads before, and read `rising` in its second
  week (`test_a_topic_found_this_week_counts_last_week_by_the_same_rule`). **A category we search for is never proposed** (`weekly.py::too_broad`, listed in the run's
  notes): measured 2026-11-05, 6 of the top 10 were our own search words ("Protein", "Stress",
  "Sleep for sleep"), because the tagger hands such a topic every post that search found, so the
  ranking measured our search budget. A subject inside a seed or `CATEGORY_INFO` phrase, or all
  `GENERIC_WORDS`, needs an outcome that adds a word beyond the subject and `_EMPTY_OUTCOME_WORDS`.
  That list stays tiny: `GENERIC_WORDS` has "muscle" and "heart", and "protein for muscle" is a
  real angle. It is word-exact, so "Sleep tracking for sleep" still passes. **Suppression is novelty only**: promoted never returns
  unless its draft failed or was cancelled (`weekly.py::_draft_failed`: no article exists to duplicate, and this is
  the only way such a topic is tried again, as for the MeSH scan; queued and running still hold it),
  dismissed not within `social_dismiss_cooloff_days` unless independent creators doubled, and back after the cooloff either way; held
  topics are listed in the run's notes. The top `social_evidence_top` get PubMed counts and the
  research agent's `evidence_status` as `social_candidates.evidence`, beside — never inside —
  the metrics. Promote creates a pipeline run with origin `social`.
- **A proposed topic is shown and generated as a question** (`social/questions.py`,
  `social_topics.question`, 2026-11-05): one call per weekly run over each new topic's post
  titles (57 s for 10), written once per topic, kept only if it is a short yes/no question naming
  the subject (`usable_question`); otherwise NULL and the label stands in. A failure costs the
  question, never the run. **Promote sends the question to the pipeline, which is why this is
  more than display**: measured once on Bonsai, the label "Smart ring for sleep" came back from
  extraction as `product='Smart ring for sleep'`, a quoted phrase PubMed will not match, while
  "Can smart rings detect sleep apnea?" gave `product='smart rings'`. First full article from a
  question, 2026-11-05 ("Do smart rings accurately detect sleep apnea?", PubMed down): 21.6 min,
  validation clean, 11 of 12 sources cited, a `mixed` verdict that answers the question. But
  APPRAISE labelled 11 of those 12 `off_topic`, and the draft gave some a section of their own
  ("What the other sources measured": VO2max, pregnancy): the coverage re-prompt asks for every
  source, and a narrow question leaves more of them off the point. The reviewer trims those.
- **The literature counts ask what the question's article would search**
  (`weekly.py::assess_evidence`, as the research agent's `_deep`): extraction on the question,
  `TemplateQueryStrategy`, counted per claim, the best claim sizing the topic; `evidence.query` is
  the search counted. A question with no searchable subject says so before anyone promotes it.
  **Europe PMC answers when PubMed is down** (`source: europe_pmc`, measured 2026-11-05 during an
  NCBI `esearch` outage), in titles and abstracts only (`profile_terms`), since unscoped it
  searches full text and over-counts ~40x. Its review tag is coarser, so `reviews_or_meta` and the
  status can read higher than PubMed's. 0 papers is real information: "Does a home gym replace a
  commercial gym?" counted 0 and would likely draft `no_evidence`.
- **A search that found 0 papers goes to the bottom of the weekly list, with a bold line on the
  desk** (`weekly.py::found_no_papers`, the reviewer's request, 2026-11-05). It is the only way a
  count touches the order, and only a search that ran: `not_checked` is unknown, not zero, and
  keeps its place. Only the top `social_evidence_top` are counted, so an unchecked topic can sit
  above a counted zero. **Topics asking one question are proposed once**
  (`without_repeated_questions`, `questions.py::same_question`, Jaccard at the trend search's 0.8
  over the words without the wording); the better-ranked stays and the run's notes name the other.
  **That covers decided topics too** (`weekly.py::_decided_topics`, 2026-11-06): every promoted one
  and those held back this run. Suppression works per topic, so holding back "Sleep tracking for
  sleep" would otherwise have freed its twin, "Smart ring for sleep", to ask the same question.
  Questions are written for twice `social_max_candidates`, and the list is cut after the repeats
  go, so the next-best topic takes a repeat's slot (a run with 33 eligible had proposed 8 of 10).
- **The other two proposers show questions on the desk, and still generate from their labels**,
  because the label is what their evaluations measured. The MeSH scan's is a template at read
  time (`discovery/topics.py::topic_question`, "affect" because the scan does not know a direction).
  The trend search already wrote one: triage's `signals.reader_question`, now the row's headline;
  nothing in `app/research` changed. It is open-ended ("What are the health benefits of Pilates?"),
  not the yes/no form social topics get.
- **Products** (`products/`): identity is normalised `brand|name` (case, ™/®, brand repeated,
  "Gen 4"/"Gen4"/"4th generation" → "4"); **a brand alone is no product** (`product_key` → None), which
  also drops single-name products like "Pulsetto" — precision over recall, revisit if it bites.
  Status climbs and never falls: `uncertain` → `corroborated` (2+ independent publishers or
  creators) → `confirmed` (a web result on the brand's own domain, `is_official_url`, strict on
  purpose, whose title or snippet names the product, since a rumoured model also finds the brand's homepage;
  or an openFDA 510(k) whose applicant is the brand, asked only for `FDA_CATEGORIES`).
  **Product discovery offers the reader news before feed items** (`discover.py`): the feed backlog alone
  filled the budget, so in dev no news text had ever been read. Two dev runs now; on 2026-11-07, 12 of the run's 16 mentions came from news. **An official page must also hold, in its URL path, every word of the name that says which product it is** (`resolve.is_product_page`, `_KIND_WORDS`, 2026-11-07): that run confirmed "Samsung Galaxy watches", "Samsung Galaxy wearables" and "Meta smart glasses" on category pages whose titles named them, 3 of its 5 new confirmations. The cost: a name of only brand and kind ("Aina Ring") or an opaque path (`/p/123`) stays unconfirmed. Replayed on the 8 confirmed rows, not yet on a live run. Those three were reset by hand in dev, since status never falls; Aina Ring keeps its status.
  **The reader's category stands only if the source's own text uses one of the category's
  `CATEGORY_NAMES`** (`resolve.checked_category`, applied in `record_mentions` because readings
  are cached), else `other`: Bonsai filed watches and earbuds as smart rings, milk as a massage
  gun and a home gym as a glucose monitor, which would ask openFDA. It reads the whole text, since
  a quote is often just the name ("Dexcom Stelo"); a news item's text is only its headline, so a
  real device can fall to `other` and miss the FDA lookup. A category is set once, at insert.
  **A mention is not a launch**: one source saying it launched is `claimed`, two `corroborated`,
  only a *first* FDA clearance within a year `confirmed`, since updates get clearances of their
  own (Dexcom G7: 2022, and again 2026-06-08; `lookup_510k` sorts oldest first); "announced" or
  "coming soon" never counts (`LAUNCH_KINDS`). **Lookups go to the unconfirmed products checked
  longest ago, whichever run found them** (`discover.py::_confirm`,
  `products.confirmation_checked_at`, 2026-11-06). They used to go only to products that run
  touched, so anything named only on YouTube, 30 of 34 in dev, was never looked up, and with a
  cap of 10 and nothing recording who was tried, the same ones came back. A lookup that could not
  run keeps the product's turn. Product discovery reads the feed items the trend search
  already cached (`research_feed_state.items`, read only) plus one news search per category.
  The openFDA query and field names were checked against a live response on 2026-11-06.
- **Relations are computed at read time, never stored** (`intel/related.py`, which the scope
  test keeps free of writes): same `topic_key` or subject key across `research_topics` and
  `social_topics`, a MeSH substance whose normalised name is the subject, products named in a
  topic's posts, and product categories whose `CATEGORY_INFO` words appear in a topic. Each
  related item carries *its own system's* numbers (trend score, MeSH paper count, product
  status), untouched. No link table, so nothing can go stale or outlive what it links.
  Neither `/console/products` nor `/console/social/result` carries relations: per row they cost
  ~24 queries, and the desk loads the result on every visit, so the panel asks `/intel/related`
  for one item when the reviewer presses Related.
- **`GET /console/social/result` is snake_case** (`SocialListeningResult`), like the trend
  search's result; the desk's "Social & products" panel (`SocialPanel.tsx`) renders momentum,
  popularity, literature counts and limitations as separate lines. **By default it serves the
  latest *completed* weekly run** (`latest_weekly_run`). A failed one proposed nothing, and
  serving it told the reviewer "nothing proposed this week" while hiding last week's undecided
  topics. The panel says so instead, read from the runs list.
