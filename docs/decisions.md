# Decisions and corrections

Non-obvious calls, and places where a stated assumption turned out to be wrong. This
isn't a changelog of features — it's for reasoning that isn't visible from reading the
code, and for mistakes worth not repeating.

## Job embedding cache was keyed on a derived value, not the value it cached

**Context:** `ranking.py` embeds *extracted* JD text (see `extraction.py`), not the raw
`JobPosting.description`, so the LLM re-ranker later inherits a coarse signal that
isn't drowned in company boilerplate. The embedding itself is expensive enough to be
worth caching in the database.

**What went wrong:** `JobEmbeddingRow` was keyed on `description_hash` — a hash of the
*raw* description, not of the text that was actually embedded. That was fine while
extraction didn't exist (the embedded text and the description were the same thing),
but the moment `extraction.py` became a real transformation, `description_hash` stopped
identifying what was cached and started only identifying *what it was derived from*.
Extraction logic (the header lists in `config.py`, `extraction.py`'s own matching
rules, and what `adapters/base.py`'s `strip_html` hands it) is expected to keep
changing — but none of those changes touch `description_hash`, so none of them would
ever invalidate a cache entry.

**The concrete failure:** after building `extraction.py`, one full pipeline run had:
correctly recomputed resume embedding (its cache already keyed on a hash of the resume
text itself, so it invalidated properly), scored against **stale JD embeddings** left
over from before extraction existed at all. `get_job_embeddings` found existing rows
under the unchanged `description_hash` keys and returned them without re-embedding. The
diagnostics printed alongside each score (`extracted: True`, token count) were computed
fresh every call, so they looked correct and consistent with the score next to them —
but the score itself came from a different, older piece of text than the diagnostics
described. No error, no warning: a plausible-looking number that was quietly wrong for
one session, until a later, unrelated `strip_html` fix prompted a check that caught it.

**Fix:** `JobEmbeddingRow`'s primary key is now `text_hash` — `sha256` of the exact
string passed to `model.encode()`, computed by `ranking.get_job_embeddings` itself, not
supplied by the caller. Any change to extraction logic changes the text, which changes
the hash, which is automatically a cache miss. No manual invalidation step to remember
or forget.

**Lesson:** a cache key has to identify the value being cached, not something merely
correlated with it. `description_hash` identified *which JD*; it never identified *what
text got embedded*. Those were the same thing only by coincidence, for as long as
extraction was an identity function. Once extraction became real, the two quietly
diverged and the cache started lying — safely and confidently.

## Correction: the `strip_html` fix does not change `description_hash`

**Context:** `adapters/base.py`'s `strip_html` was changed to turn block-level tags
(`p`, `div`, `h1`–`h6`, `li`, `br`, `tr`) into newlines instead of collapsing all
whitespace — including line breaks — into single spaces, so `extraction.py`'s
standalone-line header detection has real line breaks to match against.

**Stated premise (mine, restating the user's):** "description text changes, so
`description_hash` changes for every Greenhouse job, and they'll all show as `edited`
on the next run."

**This was wrong.** `compute_description_hash` (`models.py`) hashes
`_normalize(description)`, and `_normalize`'s own whitespace collapse
(`re.sub(r"\s+", " ", text)`) already treats space, tab, and newline identically before
hashing ever happens. The `strip_html` fix only changes *which* whitespace character
sits at a given position — never the words, their order, or their count — so the
normalized text, and therefore the hash, is byte-identical either way.

**Verified, not assumed:** compared `description_hash` computed from the old
(space-collapsing) and new (newline-preserving) `strip_html` output, across every real
job in `companies.yaml` at the time — 71 Greenhouse postings (PhonePe) and 957 Lever
postings (Paytm, Paytm Payments Services — Lever's `_assemble_description` also calls
`strip_html` for its `lists` content). **0 mismatches out of 1028.**

**Actual consequence:** none. No row was reclassified as `edited`; `application_status`
was never at risk, because `db.py`'s edit-detection path (comparing stored vs. incoming
`description_hash` in `upsert_job`) never triggers when the hash doesn't change.

**Lesson:** "the text changed" does not imply "the hash changed" once a lossy
normalization step sits in between — and this project has one everywhere an identity
hash is computed (`_normalize` for both `content_hash` and `description_hash`). Trace a
stated premise through the actual normalization before repeating it, rather than
inheriting it as given.

## The first label set (`data/labels.csv`) was unusable

**Context:** `evaluate.py` was built to score `ranking.py`'s embedding ranking against
96 hand-labeled jobs, keyed by `content_hash`.

**What went wrong:** every one of the 96 labels came back `missing` — 0/96 joined
against the database. Two separate, compounding problems:

1. **Company roster mismatch.** The 96 labels span 44 distinct companies (Mercor,
   Databricks, Baseten, OpenAI, GitLab, Palantir, and 40 more) — an entirely different,
   much larger set than the 4-company roster configured in `companies.yaml` at the time
   the labels were checked. Neither PhonePe nor Paytm appears anywhere in the 96 labels.
2. **Hash scheme mismatch, even where the same job plausibly still exists.**
   `Sarvam | Backend Engineer, Chanakya` (labeled) has the exact same title as
   `Sarvam AI | Backend Engineer, Chanakya` (in the database) — plausibly the same
   long-open requisition. Recomputing `content_hash` for it every way this codebase
   knows how — current 3-argument formula with `"Sarvam"` or `"Sarvam AI"` as the
   company, and the pre-location-fix 2-argument formula — matched none of them against
   the label's stored prefix. The label set's `content_hash` doesn't appear to have been
   generated by this codebase's `compute_content_hash` at all.

**Fix:** stopped trying to reconcile an external label export with this database.
`export_labels.py` now generates the labeling sheet (`data/labels_todo.csv`) by reading
`content_hash` directly out of `job_postings` — the full 64-character hash, not a
16-character prefix from an unknown source — so there is nothing to drift out of sync
with. It always matches, because it's the same table `evaluate.py` will query later.

**Lesson:** ground truth has to be generated *by* the system being evaluated, not
supplied by an external process and joined after the fact. A label file with no
verifiable link to the identity scheme it's meant to score against isn't ground truth —
it's a list that happens to share some company names.

## Rule-based filter yield: 20 / 7456 survive

**Context:** after expanding `companies.yaml` from 4 companies to 46 (mostly US-based)
and adding the India location filter (`filters.location_matches_india`), ran
`pipeline.py --filtered` against the live roster.

**Result:**

```
Total jobs fetched:  7456
Survived filters:      20  (0.27%)

Rejected by rule:
  seniority:          3632
  not_allowlisted:     2138
  non_engineering:     1143
  not_india:            491
  experience_too_high:   32
```

**Reading it:** `seniority` and `not_allowlisted` together account for over 77% of all
rejections — expected, since a roster of mostly-US, mostly-experienced-hire companies
produces mostly senior-shaped titles before location or experience ever get checked.
`not_india` (491) is smaller than any of the three title-based rules, which makes sense
given the cheap-filters-first ordering: most non-fits are already gone by the time
location is evaluated. `experience_too_high` (32) is tiny relative to the others — most
of what would otherwise need experience-text parsing has already been filtered out by
title alone, and it's also the rule with a documented false-reject failure mode (see
`filters.py`'s `parse_max_experience_years` docstring), so its small size doesn't mean
it's trustworthy, just infrequent.

## Filter miss: `"QA"` in `TITLE_ALLOWLIST` accepts non-engineering QA roles too

**Found while reviewing what passed:** `Weloglobal | Circinus - QA Audio Contributor
English (India)` survived every filter. `TITLE_ALLOWLIST` contains the bare token
`"qa"`, matched with a word-boundary regex — which correctly avoids substring
false-positives like matching inside "squad", but has no way to distinguish "QA" as in
*software quality assurance* from "QA" as in *quality assurance for an audio data
annotation project*. The actual role is scripted audio data collection for an AI
training dataset — not an engineering job by any reading, but it passed because the
literal string "QA" is present and nothing currently excludes it.

**Not fixed here** — recorded for when `TITLE_ALLOWLIST`/`NON_ENGINEERING_KEYWORDS` next
get tuned. Two candidate directions: replace the bare `"qa"` entry with more specific
phrases (`"qa engineer"`, `"sdet"`, `"quality assurance engineer"`), or add an exclusion
list for the non-engineering senses of overloaded abbreviations (`"audio contributor"`,
`"data annotation"`, `"content rater"`, `"data collection"`). This exact job is included
in `data/labels_todo.csv`'s passed-jobs set, so labeling it `no` gives a concrete
regression case to check either fix against.

## Ranking baseline: embeddings perform at random on the first real label set

**Context:** all 122 rows in `data/labels_todo.csv` hand-labeled (37 good, 7 weak, 78
no) and scored via `evaluate.py` against `ranking.py`'s cosine-similarity ranking.

**Result:**

```
n = 122, 37 positives

Metric              Embedding rank   Random (expected)
MRR (good jobs)              0.042             0.044
Recall@10                    0.081             0.082
Recall@20                    0.162             0.164
```

All three are at or slightly below the random baseline. On this label set, ranking by
cosine similarity between extracted resume/JD text is not providing measurable
prioritization value over shuffling the pile.

**Not a surprise in hindsight** — this is consistent with, and now quantifies, the
truncation and topical-vs-skill-matching failure modes documented when `ranking.py` was
first built (`all-MiniLM-L6-v2`'s 256-token window against resumes/JDs several times that
length; cosine similarity capturing topical overlap, not requirement-matching).

**Why this number matters going forward:** this is the baseline the LLM re-ranker has to
beat. "Better than embeddings" is a very low bar here — recorded so that bar is explicit
and can't quietly become "better than nothing," which is a different and much easier
claim.

## `experience_too_high` removed: this was a config error, not a parser bug

**Context:** `experience_too_high` was rejecting 19 of the 37 hand-labeled `good` jobs -
by far the largest source of false rejects (see the ranking-baseline evaluation above).
The working theory going in was that `parse_max_experience_years` was misreading company
boilerplate ("we've been building for 10 years") as a candidate requirement - the
already-documented xfail case.

**Investigated before changing anything:** pulled the exact matched text and surrounding
context for all 19 cases directly from the stored descriptions. The boilerplate theory
was wrong. 18 of 19 were genuine, correctly-anchored requirements sitting directly inside
a `Requirements`/`Qualifications`/`What You'll Need` section - `"5+ years"`, `"at least 4
years of experience"`, `"7+ years in Software QA"`, and so on. The parser read every one
of them correctly. Only one (a founder's personal bio, not company-history phrasing)
touched the documented xfail pattern at all, and it didn't change that job's outcome
since a second, genuine requirement was present in the same posting.

**The actual bug:** `MAX_EXPERIENCE_YEARS = 2` was a hard cutoff, but the ground-truth
labels showed the real tolerance was much higher - multiple 7-year asks labeled `good`,
a 10-year ask labeled `weak` (not `no`). The config encoded a judgment - "reject anything
above 2 years" - that this system has no way to make correctly, because whether a stated
requirement is worth applying anyway is a per-job, per-person call. A fixed number can't
capture that, and this one didn't even reflect the actual person it was supposedly
protecting.

**Fix (Option 3 of three considered - see the false-reject investigation above for why
the other two were ruled out):** `experience_too_high` removed from
`filters.reject_reason` entirely - stated experience is no longer a filter criterion.
`parse_max_experience_years` still runs on every job; the result is stored on
`JobPostingRow.experience_years_required` (nullable - `NULL` means "no requirement
found," never coerced to 0) and displayed alongside every listing in `--filtered` and
`--ranked` as `"(N yrs)"` or `"(not stated)"`, so the person doing the actual judging has
the information without the system pre-deciding for them.

**Measured effect**, before vs. after, on the same 37 `good`-labeled jobs:

```
good jobs passing filters:   17 / 37  ->  31 / 37
false rejects (good/weak):   25       ->  6
false accepts (no):           1       ->  14
```

Not a free win - going advisory traded false rejects for false accepts. The 6 remaining
false rejects are `not_allowlisted`/`not_india` misses, unrelated to experience. The rise
in false accepts (mostly GoHighLevel `"...III"` titles and HighRadius `"...IV"`/`"SDE-III"`
titles) shows the hard experience cutoff had been doing accidental double duty as a rough
seniority filter for numbered-level titles that `SENIORITY_KEYWORDS` doesn't catch (no
literal "Senior"/"Staff"/etc.). That's a real, separate gap now visible on its own merits
instead of hidden behind a cutoff that was catching it for the wrong reason.

**Lesson:** the ground-truth labels didn't just find a bug, they found a policy encoded
as if it were a fact. "The parser is right and my config was wrong" - the fix wasn't in
the regex, it was in stopping the system from making a decision it wasn't equipped to
make and handing the number to the person who is.

## Gemini free-tier rate limits: check aistudio, not the docs

**Context:** built in exponential backoff on 429s from the start (`llm.py`), and measured
real token cost per job before depending on a full nightly run fitting inside the free
tier (see "Measured token cost" work in agents/analyst.py's build).

Google's published docs no longer list current free-tier numbers - check
**aistudio.google.com/rate-limit** directly for the actual current RPM/TPM/RPD figures on
the project being used, not a docs page or a number quoted from memory (mine included -
verified live against the API's own `models.list` during this same session that my own
training knowledge of available models was already multiple generations stale).

A few things worth knowing about how the caps work, independent of their exact values:

- **Limits are per-project, not per-key.** Multiple keys under the same Google Cloud
  project share one quota; generating a second key doesn't get a second budget.
- **RPM, TPM, and RPD are independent caps**, not one combined budget - a run can be well
  under the daily token budget and still get 429'd for a burst that's too fast, or vice
  versa. Backoff-and-retry handles this by slowing down, not by needing the exact numbers
  known in advance.
- **The daily quota resets at midnight Pacific time** - roughly 12:30pm IST, not midnight
  IST. A run scheduled for 2am IST is using the *previous* Pacific day's quota, not a
  fresh one; the reset lands mid-run-cycle from an IST perspective, not at a natural
  pipeline boundary.

## Model landscape moved faster than expected - verify, don't assume, applies to model choice too

**Context:** `GEMINI_MODEL` was set to `gemini-2.5-flash` when `llm.py`/`agents/analyst.py`
were built. Asked to check what models were actually available to the API key via a live
call to `models.list` rather than assume the training-data-era model lineup was current.

**Result:** the account has access through Gemini 3.6 (`gemini-3.6-flash`) and 3.1 Pro
preview models - several generations past 2.5. This wasn't a guess that turned out
slightly off; it was a full model generation gap between what got hardcoded and what was
actually available, caught only because the check was live against the real API instead
of trusted from memory.

**Follow-up finding, comparing `gemini-2.5-flash` against `gemini-3.5-flash-lite` (the
newest non-preview Flash-Lite available) on the same 3 real jobs:** Flash-Lite returned
`thoughtsTokenCount: 0` on all three calls, versus ~1000+ thinking tokens/job on 2.5-Flash
- roughly halving total cost. But it also consistently under-matched skills that were
genuinely present in the resume (e.g. missed "Java" as a matched skill on an Android
posting where 2.5-Flash correctly caught it), and read one job's stated experience
requirement as 7 years where 2.5-Flash read the same text as 5. Not switched - n=3 is too
small to act on, and the failure direction (under-matching real overlaps) works against
the one property most explicitly asked for (accurate skill correspondence). Recorded as a
finding to revisit with a larger comparison sample, not a decision.

**Lesson:** "verify against the real system, don't trust what you know" - the theme
running through most of the entries in this file - applies to model selection exactly the
same way it applied to hash formats and cache keys. A model name is a config value with
its own expiry date, not a fact.

## Actual Gemini free-tier rate limits, and why the cascade needed pacing, not just backoff

**Context:** the previous entry said to check aistudio.google.com/rate-limit directly
rather than trust a remembered or documented number. Did that for both models in the
two-stage design:

```
gemini-3.5-flash-lite (stage 1):  15 RPM  /  500 RPD
gemini-2.5-flash      (stage 2):   5 RPM  /   20 RPD
```

**This is the number that makes the two-stage design work at all, not just cheaper.**
20 RPD on `gemini-2.5-flash` is smaller than the 52-job survivor count by itself - a
single-stage run against it alone cannot finish one night's batch even in principle, no
amount of backoff changes that a 21st request that day gets rejected. Confirmed live: a
single-stage run against `gemini-2.5-flash` hit 22/20 and was fully exhausted mid-run.
`gemini-3.5-flash-lite`'s 500 RPD comfortably covers a full stage-1 pass over every
survivor with room to spare; `STAGE2_TOP_N = 15` then keeps stage 2 under its 20 RPD
ceiling with 5 requests of same-day headroom. Backoff (already built, `llm.py`'s
`_post_with_backoff`) handles bursts and transient failures; it does nothing for a hard
daily cap that's smaller than the job count - only routing most of the volume to the
model with the much larger budget does that.

**Added deliberate pacing on top of backoff, not instead of it.** Backoff only reacts
after a 429 already happened, which on a tight RPM (5/min for stage 2) means the first
real request routinely eats a rate-limit response before backing off into rhythm.
`GeminiClient` now sleeps before sending, just enough to keep its own call rate under
`GEMINI_RATE_LIMITS[model]["rpm"]` (config.py), so the client doesn't rely on getting
rate-limited to learn how fast it's allowed to go. The two are complementary: pacing
avoids most 429s pre-emptively, backoff still catches whatever pacing doesn't (bursts,
real transient 5xx failures).

**Lesson:** independent RPM/TPM/RPD caps (recorded two entries above) means a two-stage
design has to route by the *tightest* cap of the model it's trying to protect, not by
cost alone. Flash-Lite being cheaper was the original reason for the cascade; Flash-Lite's
500-vs-20 RPD gap turned out to be the reason the cascade is *necessary*, not just an
optimization.

## Headline evaluation result: stage-1 LLM analysis beats random, embedding cosine similarity doesn't

**Context:** `evaluate_stage1.py` compared three rankings - embedding cosine similarity,
stage-1 `fit_score`, and random - on the 52-job overlap between the 122 hand labels and
jobs that have a cached stage-1 result (43% of the label set; stage 1 only ever ran over
rule-filter survivors, so a `good`-labeled job the filters rejected has no stage-1 score
to compare).

**Result** (all three rankings restricted to the same 52-job overlap):

```
Metric          Embedding   Stage-1   Random (expected)
MRR (good)          0.054     0.100                0.087
Recall@10            0.065     0.258                0.192
Recall@20            0.323     0.355                0.385
```

8 of stage-1's top 10 by `fit_score` are labeled `good`, versus 2 of embedding's top 10.
The single `strong` verdict this run produced - GoHighLevel "Full Stack Builder (Team of
One)", `fit_score` 85 - matches a `good` label.

**Reading it:**

- **Embedding cosine similarity performs at or below random on both metrics measured** -
  consistent with, and now reconfirmed on a larger, post-`experience_too_high`-fix label
  set than, the original ranking-baseline entry above (MRR 0.042 vs. 0.044 random there;
  0.054 vs. 0.087 random here).
- **Stage-1 `fit_score` is the first ranking signal in this project to clear random on
  anything.**
- **The advantage is front-loaded, not uniform.** Strong at recall@10 (0.258 vs. 0.192
  random), back down near random by recall@20 (0.355 vs. 0.385 random). Stage-1 is good
  at clustering genuine matches at the very top of the list, not at correctly ordering the
  whole pool.

**n=31 `good` labels in a 52-job overlap - indicative, not conclusive.** A handful of jobs
moving position would swing this visibly. Worth re-checking as more nights of data
accumulate, and once stage 2's re-ranking of the top 15 has its own evaluation against the
same labels.

**Design consequence for the dashboard (not yet built):** because the advantage is
concentrated in the top 10 and gone by 20, the dashboard should show a short top-N list,
not a full ranked table - past roughly rank 10-15, stage-1's ordering carries little more
signal than random on this evaluation, so presenting the whole survivor list as if it were
meaningfully ordered would overstate what was actually found.

**Open question, flagged not decided: are embeddings still worth keeping?** Since the
two-stage design, `ranking.py`'s output isn't even in the Analyst's input path any more -
`pipeline.py`'s `_analyze_stage` calls `filter_jobs` directly, and stage 1 runs over every
survivor rather than an embedding-selected top-30. Embeddings currently exist only behind
`--ranked` (a manual diagnostic view) and inside `evaluate.py`/`evaluate_stage1.py` (the
comparison baseline itself). Given they measure at or below random, wiring them back in as
a live pre-filter would be actively worse than no pre-filter, not neutral. Two things argue
for keeping the code anyway rather than deleting it: (1) it's the only baseline this
evaluation has to compare the LLM against - remove it and there's nothing to say "stage-1
is more than a fancy random shuffle" against; (2) if the survivor count grows enough that
even stage-1's 500 RPD budget stops covering a full night's pass (currently 52 vs. 500 -
not close, but not permanently safe as `companies.yaml` keeps growing), *some* cheap
pre-filter becomes necessary again - though given the demonstrated random-level
performance here, that would likely mean finding a better pre-filter, not reviving this one
unchanged. Recommendation: keep `ranking.py` as the evaluation baseline, drop it from any
future "select top N for the LLM" role unless a cheaper alternative is shown to beat random
first.

## LangGraph orchestration: three things verified live before designing `orchestrator.py`

**Context:** `langgraph`/`langgraph-checkpoint-sqlite` weren't installed yet (1.2.10 /
3.1.1 once added - newer than anything likely in training data, same generation-gap
pattern as the Gemini model check). Rather than design the graph from remembered API
shape, three small experiments were run against the real, installed library first, and
each one changed the design:

**1. Pydantic models don't survive the checkpointer.** Putting a `JobPosting` in graph
state throws `TypeError: Type is not msgpack serializable` the moment a node tries to
write it - confirmed by reproducing it directly, not inferred from docs. This settled the
state design: graph state carries `content_hash` strings and small plain dicts only, never
job/result objects. Every node re-derives real objects from the database via the existing
`job_posting_from_row`. This turned out to be the right shape independent of the bug too -
the database was already the real state store (every posting and every Analyst verdict is
already persisted there); graph state only needed to be coordination metadata layered on
top, not a second copy of the data.

**2. `RetryPolicy`'s default `retry_on` only retries specific failure classes.** Verified
by forcing a node to raise different exception types: `ValueError`/`TypeError`/
`RuntimeError` and several other built-ins are explicitly excluded (presumed-deterministic
bugs - retrying won't fix them), while `ConnectionError`, retryable 5xx `HTTPError`, and -
importantly - this project's own custom exceptions (`ATSAdapterError`, `LLMError`, since
neither is in the exclusion list) fall through to "retryable". A plain
`RetryPolicy(max_attempts=N)` with no custom `retry_on` does the right thing here with zero
extra configuration.

**3. The one that would have caused a silent, real bug: `invoke()` with real input vs.
`None` on a thread with existing history.** Verified by crashing a node deliberately and
re-invoking both ways: passing a fresh input dict on a thread that already has *any*
checkpoint history - whether it crashed partway or already completed successfully -
**replays the entry node from scratch**. Only `invoke(None, config)` continues from the
last checkpoint, and `invoke(None, ...)` on an *already-completed* thread is a true no-op
(the entry node was confirmed not to re-run). This is why `run_nightly()` calls
`app.get_state(config)` before deciding what to pass, rather than always passing fresh
input - getting this backwards would mean "just run the script again" after a crash
silently re-fetches everything, exactly the bug crash-resume exists to prevent.

**Cross-midnight resume:** a purely date-based `thread_id` (`nightly-2026-08-05`) means a
run that crashes at 2am and isn't re-run until the next morning is a *different* thread by
the time anyone notices - refetching everything, the exact scenario checkpointing exists
to avoid. Fixed by scanning a bounded lookback window (`LOOKBACK_DAYS = 7`) at startup for
the most recent thread with history but no completed run (`state.next` non-empty). A
thread that reached `END` - whether via the full cascade or an early conditional skip -
always shows `next == ()`, indistinguishable by design from "nothing left to do" (verified:
a normally-completed thread is correctly never picked up as resumable).

**End-to-end verification:** the graph was run for real (46 companies, real fetch/persist -
28 new, 225 edited that night), a hard `os._exit()` was injected 3 jobs into stage-1
(bypassing Python's exception handling entirely, the closest reproducible approximation of
a real process kill), and a second, separate process was launched against the same
thread_id with `fetch_all` wired to raise if called at all. Result: fetch was never called
again (zero-length grep for both the assertion and the "Fetching jobs for" log line),
stage-1 resumed and came back 52/52 cache hits - including the 3 jobs completed
immediately before the crash, confirming their writes were durable before the kill - and
the run proceeded into stage-2, which happened to hit the real daily quota wall mid-run and
degraded exactly as designed (`STOPPED: ... re-running will skip them and resume here`), an
unplanned but welcome confirmation that the existing graceful-stop behavior and the new
node-level machinery don't interfere with each other.

**Lesson:** the same "verify against the real system" discipline that already applied to
hash formats, model availability, and rate limits applies to library internals too,
especially a library on its 1.x major version with a fast release cadence - a checkpointing
feature is exactly the kind of thing where "should work" and "does work" are worth keeping
separate until proven.

## Numbered seniority titles were an unfiltered gap, not a MAX_EXPERIENCE_YEARS side effect

**Context:** the dashboard surfaced "SDE III", "Java Developer IV", "Software Development
Engineer - II" among ranked results. `SENIORITY_KEYWORDS` is a fixed word list
(`"senior"`, `"staff"`, `"lead"`, ...) with no concept of numbered or Roman-numeral levels
at all, so these passed every existing filter untouched. This is exactly the gap the old
`MAX_EXPERIENCE_YEARS` hard cutoff had been closing by accident (see the entry above on its
removal) - once that cutoff went away, nothing else was catching numbered-level titles.

**Fix:** `filters.has_numbered_seniority_level()` - Roman numerals II-V, `SDE-2`..`SDE-5`,
`L3`..`L7`, and a bare digit 2-5 immediately after a recognized role noun ("Engineer 2").
Deliberately excludes "I" (`Engineer I`, `SDE-1` are entry level) and deliberately does NOT
recognize "Tier"/"Level" as level-prefixes - `"Tier 2 Support"` is a support-tier label, not
an engineering seniority level, and it's excluded by simply not being in the prefix list,
not a special case. A digit-range strip (`\d\s*-\s*\d`) runs first so `"3-5 Years"` (a real
title, PhonePe's React Native posting) and `"Backend Engineer Q2-02"` (a real title, Reo
Dev) never get mistaken for a level marker - verified against both as explicit regression
tests, not just imagined edge cases.

**Measured effect:** re-running stage 1 after this fix, the survivor population dropped
from 52 to 37 - 15 jobs excluded, including all three HighRadius numbered titles and all
three SpotDraft numbered titles, previously showing mid-range "possible" scores despite
being senior roles.

## Experience-gap system-instruction change, and the Broccoli case it didn't fully fix

**Context:** HighRadius "Java Developer IV" scored 45 ("possible") with reasoning stating
"lacks the required 7+ years" - a job requiring 7 years, evaluated against a fresher
resume, should not land mid-range on skill overlap alone. `agents/analyst.py`'s
`SYSTEM_INSTRUCTION` rule 4 was updated: an unmet experience requirement now caps
`fit_score` - a large shortfall (resume showing roughly half or less of the years
required) must land below 40 regardless of skill overlap; a small shortfall (within 1-2
years) may still land in the 40-74 range if skills are otherwise strong.

**Measured effect:** the "possible" band (40-74) shrank from ~20 jobs to 6 across the
37-job re-run; concrete before/after cases (Sarvam AI Backend Engineer, Reo Dev, Twilio
L2, GoHighLevel Email Builders - all `resume_meets_it: False` with real gaps) correctly
dropped into `weak` (<40) as instructed.

**One case the prompt change didn't fix, investigated rather than assumed:** Broccoli
"Software Engineer" scored 60 ("possible") with `3 yrs required, resume meets it: False`
- by the instruction's own wording this shortfall should have capped below 40. Pulled the
raw description and the extractor's output directly (same method as the original
Acceldata investigation) to check whether this was a repeat of that bug (extraction
anchoring on the wrong header). **It was not.** The extractor correctly found and included
both `"WHAT YOU'LL DO"` and `"WHAT YOU'LL BRING"` - the posting's entire requirements
section - and that section genuinely states no concrete technology, tool, or framework
anywhere: only generic ownership/curiosity language ("high ownership", "curiosity for how
real businesses operate") plus a bare `"2-4 years of experience"` figure. `matched_skills`
and `missing_skills` both came back empty because there was nothing concrete to list either
way. The model appears to have scored a general impression of the resume with nothing to
compare it against, rather than refusing to produce a number.

**Fix - a fourth outcome, not a better prompt:** a prompt instruction can steer behavior but
can't guarantee it, and this case shows a fit_score can still come out of a comparison that
never actually happened. `agents/analyst.py` now has `is_unscored(result)` (true iff both
skill lists are empty) and `derive_outcome(result)` (returns `"unscored"` regardless of
what fit_score says, before falling back to `derive_verdict`). `_store()` uses
`derive_outcome`, so the stored `verdict` column is the authoritative signal going forward.
Every consumer was updated to treat `"unscored"` as its own outcome, not a low or a mid
score:
- `pipeline.py`'s `_print_results_detail` lists unscored jobs separately, never interleaved
  into the fit_score-sorted ranking.
- `pipeline.py`'s stage-1-to-stage-2 selection and `orchestrator.py`'s `stage1_analyze` node
  both exclude unscored jobs before ranking for stage 2 - a fabricated number must not
  compete for one of stage 2's 15 scarce slots either direction.
- `app.py` splits `load_dashboard_jobs` three ways (scored / unscored / not-yet-analyzed)
  and renders unscored postings in their own "Could not evaluate" section - checked against
  the parsed skill lists directly, not the stored verdict column, so it's self-verifying
  regardless of whether a row predates the backfill.
- `agents/coach.py`'s `missing_skills_below` and `evaluate_stage1.py`'s `stage1_overlap`
  both exclude `verdict == "unscored"` rows - an aggregate or a ranking metric is exactly
  where a fabricated fit_score would quietly do the most damage.

**Backfill:** 6 of 91 existing `AnalystResultRow`s had this exact shape (empty/empty) -
2 at fit_score 60 (both Broccoli), 4 at fit_score 0 (Databricks "Full Stack Developer (AI
Agents)", Weloglobal "Circinus - QA Audio Contributor" - each x2, across postings). The 0s
happened to look "correct" by coincidence (they are genuinely bad fits) but were equally
unscored, not equally evaluated - fabricated low is not more trustworthy than fabricated
mid. Verdict updated to `"unscored"` in place for all 6; no re-analysis needed, since
`is_unscored` only needs the already-stored skill lists.

**Lesson:** "not what I expected" is worth investigating before it's worth fixing - the
instinct to treat Broccoli as a second prompt-tuning problem would have led to over-fitting
the prompt against a single anecdote. Pulling the raw data first (as the user asked: "show
me the raw description and exactly what the extractor pulled") found a different, more
fundamental issue: some structured outputs describe a comparison that never had two things
to compare. That's not a scoring calibration problem a better-worded rule can fix - it
needed a new category in the output space.

## SmartRecruiters adapter: declined on robots.txt, not on technical merit

**Context:** investigated adding a fourth ATS adapter (Freshworks, plus the biggest
remaining coverage gap after Greenhouse/Lever/Ashby). Same discipline as the other three:
verified live against real boards before writing any code, no adapter written.

**The API itself was clean - better than one of the three already integrated:**

- Public, documented, no auth (SmartRecruiters' own docs call it "the public Posting API",
  with a bare `curl` example).
- `location.remote` / `location.hybrid` are separate booleans, consistently present across
  every posting checked (verified on two unrelated companies, not just one) - a genuinely
  better structured remote-type signal than Greenhouse gives, comparable to or better than
  Lever's single enum.
- Pagination (`offset`/`limit`) is well-behaved: checked the actual boundary
  (offset=0 -> 100 of 153, offset=100 -> the remaining 53, offset past total -> empty, not
  a wraparound) - no Workday-style bug.
- Exactly one timestamp field (`releasedDate`) in the whole schema - no posted-vs-updated
  ambiguity, because there's nothing to be ambiguous with.
- An apparent encoding bug in the raw JSON (`â€œ` where a curly quote should
  be) was chased down, not assumed - fetching the raw wire bytes directly showed valid,
  correctly-encoded UTF-8. The corruption was in my own curl-to-python-stdin pipe on
  Windows, the same class of bug as this project's earlier en-dash investigation, not a
  SmartRecruiters data problem.

**Real, manageable technical costs, not fatal ones:**

- The list endpoint is deliberately partial (SmartRecruiters' own docs: "some fields might
  not be set - to get the full object use the `ref` property") - full descriptions need a
  second request per job (`GET /postings/{id}`), unlike Greenhouse/Lever/Ashby's single-call
  descriptions. N+1 requests per company, not free, but not disqualifying on its own.
- An invalid company slug returns **HTTP 200** with `{"totalFound":0,"content":[]}` -
  identical in shape to a real company with zero current openings. No 404, nothing to
  distinguish "not on SmartRecruiters" from "on SmartRecruiters, no jobs right now." This
  breaks `BoardNotFoundError`'s whole mechanism (and Scout's validation loop, which depends
  on it) - `empty_board` would become the *only* signal available here, permanently, not
  an edge case the way it is for the other three.

**The actual reason it wasn't built: `robots.txt`.**

```
User-agent: LinkedInBot
Allow: /v1/companies/
User-agent: *
Disallow: /
```

Compare this against the three adapters actually in use: Greenhouse disallows only
`/embed/`; Lever explicitly allows `/` with a crawl-delay. Both read as an ATS that doesn't
mind automated access to its board API. SmartRecruiters reads the opposite way: everything
disallowed except one named bot, on exactly the path this project would use. LinkedIn
itself runs on SmartRecruiters (found live while sourcing candidate companies - slug
`LinkedIn3`), which is almost certainly why that carve-out exists: a specific business
integration, not a general "we don't mind bots" policy. A named exception is evidence of
deliberate restriction, not an oversight to route around.

This is what actually decided it, not the technical costs above - the API being publicly
documented for third-party consumption doesn't override the site's own stated policy on
automated access, and CLAUDE.md's rule on scraping ("must respect robots.txt") doesn't carve
out an exception for "but it's a nice API." **This was a policy decision, not a technical
one** - worth being explicit about that distinction, since the technical writeup above could
easily read as "found a reason to say no" when the actual reason was singular and clear.

**Coverage finding, which would have mattered even setting policy aside:** of the India
postings found while sourcing 5 candidate companies for this investigation (LinkedIn,
Eurofins, MicroStrategy, Sutherland, NECSWS), the roles were overwhelmingly senior/lead/
manager/director level - "Senior Software Engineer", "Staff Software Engineer", "Software
Engineering Manager", "Director, Engineering", "GCP Data Engineer Lead". Not a fresher-role
market the way Ashby/Greenhouse startups on the existing roster are. Even if the robots.txt
question had gone the other way, this source doesn't look like it would have served
CLAUDE.md's actual target user well.

**Lesson:** a clean technical investigation and a "should we build this" decision are two
different questions, and this is the first of the four ATS investigations where they came
apart - Greenhouse, Lever, and Ashby were all yes on both counts. Worth keeping the two
answers visibly separate in the record, not collapsed into one verdict.

## Dashboard startup: two separate fixes, measured separately

**Context:** the dashboard felt slow to open. Turned out to be two unrelated problems
layered on top of each other - fixed and measured independently rather than as one lump.

**Fix 1 - a heavy, unnecessary import.** `app.py` only ever calls `agents.coach.
missing_skills_below` (pure SQL), never `coach.market_gap` (the one that needs
embeddings) - but Python imports execute a module's entire top level regardless of which
function is actually called, and `agents.coach` imports `rag`, which imports `ranking`,
which had `from sentence_transformers import SentenceTransformer` at module level.
`ranking.py`'s own `_get_model()` already instantiated the model lazily; the class import
itself wasn't. Moved the import inside `_get_model()` (via `from __future__ import
annotations` + `TYPE_CHECKING` for the two type-hint sites it's still needed for).
Verified the deferred import still works when a real embedding path runs it (a direct
`_get_model()` call correctly pulled in torch and produced a real 384-dim embedding, 16s -
the cost didn't disappear, it just moved to only where it's needed).

Measured (`AppTest.run()`, the real script-execution signal - confirmed the `streamlit
run` server banner appears before any client session connects and the script actually
executes, so timing that would have measured the wrong thing):

| | Before | After |
|---|---|---|
| `import app` alone | 11.40s | 1.02s |
| Full dashboard script execution | 25.42s | 14.40s |

Got a genuine controlled comparison, not a synthetic benchmark, by temporarily reverting
`ranking.py` to the pre-fix state, measuring, then restoring the fix and re-measuring
against the identical real database.

**Fix 2 - an N+1 query and full-table materialization, on the ~13s fix 1 didn't touch.**
Broken down before touching anything: loading and reconstructing all ~7500 rows into full
`JobPosting` objects (Pydantic validation, content_hash/description_hash recomputation)
cost 3.9s, and `load_dashboard_jobs`'s per-survivor `AnalystResultRow` lookup (up to 2
separate `session.get()` calls per job - a classic N+1) cost another 5.0s for just 37
survivors, only getting worse as the roster grows.

Two changes:
1. `filters.reject_reason_for(title, location)` extracted as the actual rule logic;
   `reject_reason(job)` is now a thin wrapper. Lets a caller filter against a lightweight
   `(content_hash, company, title, location)` query - never `description`, the heaviest
   column per row, and never a full JobPosting - instead of materializing every row just
   to discard most of them. `app.py`'s new `run_filter_pass()` does exactly that: the
   lightweight query decides who survives, and only survivors ever get a full row fetch.
2. `load_dashboard_jobs`'s per-job `AnalystResultRow` lookup replaced with one batched
   query: every stage-1/stage-2 candidate hash computed up front, then a single
   `text_hash IN (...)` fetch, matched in memory. 2N queries for N survivors -> 1.

**Not pushed into SQL, and why:** all four rule-filters (`seniority`, `non_engineering`,
`not_allowlisted`, `not_india`) are word-boundary regex against fixed keyword lists,
specifically *because* naive substring matching has real false-positive risk (that's the
documented reason `\b{keyword}\b` exists at all, not `LIKE '%keyword%'`) -
`has_numbered_seniority_level`'s roman-numeral/digit-range logic has no reasonable SQL
equivalent without a custom `REGEXP` function, which would just be running the same Python
regex from inside SQL for no real benefit. Porting the *keyword matching itself* to SQL
would have been a correctness regression, not an optimization - what actually mattered was
never materializing full objects for the ~7460 rows that get discarded, which SQL *is*
good at (column selection, `IN (...)` filtering on a predetermined id set) without needing
the regex logic to move anywhere.

**Caching, and the one thing that must never be cached.** `run_filter_pass`'s result
depends only on `title`/`location`/`company` - never `application_status`, never resume
text - so it's genuinely safe to cache: neither a status change nor a resume upload can
make it stale, because neither ever enters that computation. Wrapped in `st.cache_data`
(TTL 300s, `_session` argument underscore-prefixed to exclude it from the cache key -
verified this convention actually works in the installed streamlit version before relying
on it, empirically, not from memory) at the `__main__` level, kept as a plain
Streamlit-free function underneath for testability.

`load_dashboard_jobs` was deliberately NOT wrapped the same way, even though it's the
slower of the two - it reads `application_status` (the status selectbox's own write
target) and depends on resume text (the Resume tab's write target) on every call. Caching
either would reintroduce exactly the "status change doesn't show up" bug class this
project already named as a reason to avoid caching in the first version of this file.
Verified directly, not assumed: on an isolated test DB, changed a job's status through the
real widget, confirmed the DB updated, and confirmed the job correctly disappeared from
the default (new/interviewing) filter on the very next rerun - proving the cached
`run_filter_pass` result and the uncached `load_dashboard_jobs` result compose correctly,
not just that each looks right in isolation.

Measured, same database, no revert needed this time (the fix-1 "after" state from the
previous entry is the legitimate fix-2 "before" baseline - nothing about DB access
patterns had changed in between):

| | Before (fix 1 only) | After (fix 1 + fix 2) |
|---|---|---|
| Full dashboard script execution | 14.40s | 5.45s |
| Rerun after a widget interaction (cache hit) | - | 0.56s |

The 0.56s number is the one that matters most for actual usage - every status change,
every filter tweak, every tab click triggers a full script rerun, and that's now the
common case, not the 5.45s cold-start number.

**Lesson:** "the remaining time is DB work, not imports" turned out to be two genuinely
different problems (N+1 query pattern, unnecessary full materialization) that happened to
show up as one aggregate number. Breaking the measurement down before proposing a fix -
same discipline as the sentence-transformers investigation - is what found the second,
compounding issue instead of stopping after the first plausible-looking win.

## CSS bugs with no real browser to check them in

Two bugs were reported from the running dashboard: the "CareerPilot" title and the
"Rejected" tab label were invisible until hovered, and the run-stats strip appeared to
render above the page title.

**The invisible-text bugs had a real, confirmable cause: three places in `PAGE_CSS` set
colour on native Streamlit elements this project doesn't fully control.**

- `[data-testid="stTabs"] button[role="tab"] { color: var(--cp-text-muted); }` set text
  colour on Streamlit's own tab buttons. Nothing in that rule controlled what background
  those buttons actually sit on - that's owned entirely by Streamlit's own light/dark
  theme, which is a separate setting from the OS-level `prefers-color-scheme` this
  project's `@media` block reads. If the two ever disagreed (dashboard's CSS variables
  said "light", Streamlit's own theme was rendering dark chrome, or vice versa), text
  colour and background colour would come from two unrelated sources with no guarantee
  they'd contrast - exactly what "text only visible on hover" looks like (hover states
  often get a background nudge from Streamlit's own CSS, coincidentally restoring
  contrast). This is the confirmed cause of the Rejected-tab-label bug.
- `[data-testid="stAppViewContainer"] { background: var(--cp-bg-page); }` forced this
  project's background variable onto a native container without forcing a matching text
  colour anywhere - the same asymmetry, on the container the page title's `<h1>` sits
  inside. Flagged as the leading suspect for the invisible-title bug, on the same
  reasoning, though it could not be independently confirmed without a real browser.
- The dead `[data-baseweb="tab-highlight"]` selector (see the previous entry below, or
  rather - this one predates it) was a leftover from styling the active-tab indicator by
  guessing at an internal sub-element name, never verified. Grepping the installed
  `streamlit==1.61.0` static JS bundle directly for the literal string `tab-highlight`
  and for the pattern `data-baseweb="tab*"` found zero matches - dead CSS, matching
  nothing, but worth removing anyway since guessed-and-unverified selectors are exactly
  the failure mode this project has hit before (see the `data-testid` verification
  discipline elsewhere in this file).

**Fix, and the principle behind it:** removed every place `PAGE_CSS` set `color` (or
`background` without a paired, equally-forced `color`) on a `[data-testid="st*"]`
selector. Native Streamlit elements are now left alone entirely - their colour comes only
from Streamlit's own theme, whatever it is. Colour is only ever applied to elements this
project builds itself (`.cp-card`, `.cp-chip`, `.cp-stats-strip`, etc.), and every one of
those always sets background and text together from the same synced `:root` /
`@media (prefers-color-scheme: dark)` variable pair, so a custom element can never drift
out of sync with itself the way a native/custom mismatch could. The accent-coloured
active-tab indicator was kept, but re-implemented as a `border-bottom` on the
already-confirmed-working `button[role="tab"][aria-selected="true"]` selector - a border
either shows or it doesn't; it has no "blends into an unexpected background" failure mode
the way text colour does.

Audited every remaining `color:` line in `PAGE_CSS` against this rule directly (grep, not
memory) - all of them are on `.cp-*` classes, all self-contained. There is no remaining
place in the current file where colour is set on a native element.

**The layout-order bug turned out not to be a bug in this file at all.** `st.title()`
appears before the stats-strip `st.markdown()` call in the source (line 739 vs. 797), and
`AppTest` against a seeded temp database confirmed the same order in the actual rendered
widget tree: `markdown(PAGE_CSS)` -> `title` -> `caption` -> `markdown(stats-strip div)`.
No CSS rule in this file sets `position`, `order`, `float`, or anything else capable of
visually reordering block-level content (checked by grep, not assumption). The most likely
explanation is `run_dashboard.py`'s `--server.fileWatcherType none` flag (added
deliberately, to quiet the torchvision file-watcher noise - see the startup-time entry
above): with the watcher off, a Streamlit process started before this file's Task B
redesign (when the stats strip genuinely did come first) keeps serving that old page
indefinitely across edits, until the process is stopped and restarted by hand. If the
dashboard has been running continuously since before the redesign, this fully explains the
symptom without any code change. Not independently confirmed - flagged to the user as the
leading explanation, with a plain "stop and restart the process" as the test.

**The harder problem: none of the above could be confirmed by rendering anything.** This
project has no real browser available - `AppTest` executes `app.py`'s script body and
inspects the resulting widget tree and HTML strings, which is enough to catch exceptions,
confirm which CSS rules are present/absent, and confirm element order, but it never
renders a pixel and cannot see colour, contrast, or a hover state. Every fix above is the
strongest defensible hypothesis from code review, not a verified fix, and is reported to
the user as exactly that distinction.

**`style_preview.py`** was added as the answer to "I can't see this, and you can't check
it" going forward: a standalone page (`streamlit run style_preview.py`) rendering one of
every element (job card per verdict, unscored card, missing-skills overflow chips, stats
strip, empty state) twice, forced into light and dark via explicit `.cp-force-light` /
`.cp-force-dark` wrapper classes that redeclare every `--cp-*` variable locally rather than
relying on `prefers-color-scheme` - a real browser only ever reports one OS theme at a
time, so side-by-side comparison has no other honest implementation. It intentionally does
NOT attempt to force native Streamlit chrome (tabs, title) into both themes at once, since
`PAGE_CSS` no longer touches native colour at all - there is nothing left in native
elements that this project's own CSS could break, regardless of which theme Streamlit
itself happens to be in.

**Lesson:** the same "verify against the real system, don't trust what a selector or a
memory says it should do" discipline that caught the dead `tab-highlight` selector applies
just as hard in the other direction - when there is no way to verify at all (no browser),
the honest move is to say so explicitly, fix what code review can actually support, and
build the tool that closes the verification gap, rather than asserting a visual fix is
correct when it hasn't been seen.

## ATS platform survey: the 89 not-on-Greenhouse/Lever/Ashby companies are a long tail, not a fourth platform

After a batch scout run left 89 companies unclassified as `not_supported`, the obvious next
question was whether they clustered on one platform worth a fourth adapter, or were scattered.
Investigated 20 of them by hand (careers page + job-link URL patterns + page source), picked
across sectors: quick-commerce, fintech, SaaS, logistics, AI, healthtech, mobility.

**Result: 14 identified companies, 11 different platforms.** Best case was 2/20 each for
**Kula** (Cashfree, Rocketlane) and **Darwinbox** (Delhivery, Ather Energy) - every other
identified platform (SmartRecruiters, SAP SuccessFactors, Workday, Recruiterbox, Trakstar
Hire, Workable, TalentRecruit, Param.ai, plus one company - Zoho - running a fully
custom in-house candidate portal) had exactly one company each. 6/20 (30%) could not be
determined at all, even with real effort - Swiggy, Juspay, BharatPe, Krutrim, Yellow.ai, and
LeadSquared all either returned no ATS branding in their static HTML or are client-rendered
SPAs whose careers page ships an empty template shell with job data populated by JavaScript
after load, invisible to a static fetch.

**Conclusion: no fourth adapter is worth building.** Even the best-represented platforms
(Kula, Darwinbox) only cover 2 of 20 sampled companies - extrapolated generously to the full
89, that's maybe 9-10 companies each, not the "one adapter unlocks dozens" case that would
justify the build-and-maintain cost the way Greenhouse/Lever/Ashby did. The Indian company
market for this company list is a long tail, not a platform to consolidate around.

**The 30% undetermined rate is itself a finding, not just a gap in this survey.** A third of
these careers pages don't expose their ATS in static HTML at all - the job data only exists
after client-side JavaScript runs. A scraping-based adapter (the documented fallback for the
"long tail" per CLAUDE.md, for companies without a public board API) would have to run a real
browser to work against pages like these, not fetch-and-parse HTML - a categorically heavier,
more fragile dependency than anything the three existing adapters need, and exactly the kind
of target most likely to break silently on a layout change. That fragility risk applies before
even weighing whether any single platform's company count would justify it.

One unrelated but concrete finding fell out of this survey: Razorpay, one of the 89, turned out
to already be on Greenhouse (`job-boards.greenhouse.io/razorpaysoftwareprivatelimited`) - not a
missing platform at all, but a Scout token-guessing miss, because the real token is Razorpay's
full registered legal name, not its brand name. See the next entry.

## Scout mechanical candidates: Indian legal-entity suffixes, and what raising the guess space cost

Follow-up to the platform survey above. Razorpay's real Greenhouse token,
`razorpaysoftwareprivatelimited`, is the company's full registered legal name concatenated with
no separator - a shape `generate_mechanical_candidates`'s existing `_SUFFIXES` list
(`inc`/`hq`/`io`/`co`/`labs`, each tried both concatenated and hyphenated) never covered, and a
shape the one LLM fallback round also never happened to propose for Razorpay specifically before
giving up. This wasn't a detection-logic bug - Scout finds tokens by testing candidates directly
against the adapter APIs, it never scrapes a careers page for ATS links - it was a gap in what
candidates got generated at all.

**Fix:** added `_INDIAN_LEGAL_SUFFIXES` - `softwareprivatelimited`, `technologiesprivatelimited`,
`privatelimited`, `pvtltd`, `technologies`, `software`, `labs`, `solutions` - concatenated only,
deliberately with no hyphenated variant the way `_SUFFIXES` gets, because the real pattern is one
continuous legal-name string; a hyphenated form doesn't occur in practice and would just be a
wasted request. Also added the Razorpay token as a worked example in `SCOUT_SYSTEM_INSTRUCTION`,
so the LLM fallback round is more likely to propose this shape unprompted for a similarly-shaped
company Indian the mechanical list's fixed suffix set doesn't happen to match exactly (a
three-word legal name, "LLP" instead of "Private Limited", etc).

**This changed the worst-case request volume enough to require a second change.** The new
mechanical list is up to 19 unique candidates for a two-word company name (was 12), and each
untested candidate costs up to 3 requests (one per source, `SOURCE_ORDER`) before moving on -
worst case 57 requests on mechanical alone, up from 36. `MAX_TOTAL_ATTEMPTS` was still 50: a
genuinely-unsupported company could exhaust the cap *before the mechanical queue ever emptied*,
meaning the LLM fallback - carrying the very Razorpay example just added to make it more useful
- would often never run at all. Raised `MAX_TOTAL_ATTEMPTS` to 75, comfortably covering
worst-case mechanical (57) plus one full LLM round (15). Net effect: per-unsupported-company
request ceiling goes from ~45 to ~72, roughly 1.6x. Still zero dollar cost - these are free,
unauthenticated public board APIs - but a real increase in wall-clock requests for a batch run
across dozens of companies, worth stating plainly rather than raising a cap silently as a side
effect of an unrelated-looking suffix-list change.

**Re-ran batch scout over the reconstructed 92-name set (89 not-supported + a small,
expected gap - see below) to measure the actual effect.** 6233 requests, matching the
~1.6x-per-company estimate above. Result: 3 found, 2 empty board, 87 still not supported.

- **Razorpay - the motivating case - found, exactly as intended**: `token=
  razorpaysoftwareprivatelimited on greenhouse, via a mechanical candidate`. This is the
  one hit directly attributable to this fix - `_INDIAN_LEGAL_SUFFIXES` generated the
  correct token mechanically, no LLM round needed.
- **Fi Money** (`token=fi` on lever) and **Shiprocket** (`token=rocketship` on lever) were
  also found, both via the LLM fallback round, not the new mechanical suffixes - worth
  being precise about, since neither token shape resembles an Indian legal-entity suffix
  at all. Checked whether the raised `MAX_TOTAL_ATTEMPTS` (75, up from 50) was what let
  these two get an LLM round they wouldn't have gotten before: no - both names are short
  enough (11 and 2 words) that even the *old*, smaller mechanical list would have stayed
  under 50 attempts and still reached the LLM round. These 2 are ordinary LLM-fallback
  finds that most likely would have surfaced on a plain re-run regardless of this
  particular fix, not a second confirmed win for the legal-suffix change specifically.
- **87 remain genuinely not-supported** - consistent with, and now backed by real request
  data rather than just the 20-company manual survey above, the "long tail, not a
  platform to consolidate around" conclusion already recorded in this file.
- **Freshworks and Plivo** confirmed `empty_board` (real Lever boards, 0 jobs right now) -
  unrelated to this fix, unchanged from whatever the original run found.

**On the 92-vs-89 gap**: the original run's exact output was never saved to disk (only
described in a later conversation, not preserved as a file) and could not be recovered
byte-for-byte. Reconstructed the re-scout list as everything in `companies_to_try.txt`
not already present in `companies.yaml` (92 names) rather than the reported 89 - the
3-name gap is most likely explained by the `empty_board` entries (which, like
`not_supported`, are never written to `companies.yaml`, so they'd resurface in this
reconstruction too) - and this run's own 2 empty-board results are consistent with that
explanation. Results are reported here split by outcome type specifically so this
reconstruction gap can't quietly inflate the "how many were legal-name misses" count -
the answer to that specific question is 1 (Razorpay), stated precisely rather than folded
into the aggregate "3 found."

## Roles tab: live-reloadable filter preferences, and why filters.py had to stop importing names from config

Piece 1 of the daily-flow dashboard build: `TITLE_ALLOWLIST`, `SENIORITY_KEYWORDS`,
`NON_ENGINEERING_KEYWORDS`, and the India location keywords moved from hardcoded
`config.py` literals into `data/preferences.json`, editable from a new Roles tab
(add/remove entries, preview survivor-count impact before saving, save).

**The load-bearing design problem: `filters.py` used to do
`from config import TITLE_ALLOWLIST, ...`.** That binds a private snapshot in
`filters.py`'s own namespace at *its own* import time. A Roles-tab save that rebinds
`config.TITLE_ALLOWLIST` afterward would never be seen by `filters.py`'s already-bound
name - the dashboard would need a full process restart to pick up an edit, defeating the
entire point of an in-app editor. Fixed by changing `filters.py` to `import config` and
read `config.TITLE_ALLOWLIST` etc. as module-attribute lookups *inside* each function
body, not at import time - now a rebind on the `config` module object is visible on the
very next call, in the same running process. `reject_reason_for`, `reject_reason`,
`filter_jobs`, `rejected_jobs`, and `location_matches_india` all gained one new optional
parameter each (`preferences: Optional[Preferences] = None` / `india_location_keywords:
Optional[list[str]] = None`) rather than a second code path - passing an explicit override
evaluates against a candidate ruleset (the Roles tab's "preview before saving" step)
without ever mutating the live `config.*` values every other session or call reads; every
existing call site keeps working unchanged since the parameter defaults to "use whatever's
currently loaded."

**`DEFAULT_PREFERENCES` is not just the seed for the generated file - it's the permanent
fallback target.** `config.load_preferences()` never raises: a missing file falls back
entirely (and re-creates the file from defaults); an unparseable file or a file with an
unrecognized field (`Preferences` uses `extra="forbid"`, same as `CompanyConfig`) falls
back entirely; a file with one or more individual lists emptied falls back *only those
specific fields*, preserving whatever real customization exists in the other three - and
every fallback is recorded as a warning string, surfaced on every dashboard page load
(not buried in the Roles tab, since a fallback changes every tab's filtering) and printed
by `pipeline.py` before a real run starts. The specific danger this defends against: an
emptied `title_allowlist` matches every title (nothing to reject against), silently
converting a targeted nightly run into scoring all ~7500+ jobs with real LLM quota - the
one failure mode expensive enough to justify never running with whatever's on disk
without saying so first.

**`st.data_editor` verified live before relying on it, same discipline as everywhere else
in this file.** Two things that looked plausible from memory turned out to need checking:
(1) its return value on every rerun is the full current row list matching the input shape,
*not* an edit-delta - the edit-delta shape lives separately in
`st.session_state[key]["edited_rows"/"added_rows"/"deleted_rows"]` and isn't what this
tab reads; (2) deleting a data_editor's own keyed session-state entry to force it to
re-seed from new data does not work reliably in this Streamlit version (`del
st.session_state[key]` raised `KeyError` even immediately after that same key was
written by the widget itself, in AppTest). The documented, working way to force a reset
is changing the widget's `key` itself - implemented here as a `roles_editor_generation`
counter in session state, bumped by both the Save and Reset actions, appended to each of
the 4 editors' keys (`roles_editor_<field>_<generation>`), forcing Streamlit to treat the
next render as a brand-new widget seeded from the just-saved/just-reset data rather than
replaying accumulated edits on top of whatever it first saw.

**Reset to defaults writes immediately, no separate confirm step** - a deliberate choice
to match the resume-save flow's existing pattern (a warning shown, then one button, no
nested confirmation dance) rather than inventing a new interaction style for one button.

**Verification limits, stated plainly:** `AppTest` confirmed the Roles tab renders without
exceptions (empty DB and structural smoke checks) and that the Preview/Save/Reset button
click flows run end-to-end without exceptions against an isolated temp DB and temp
`preferences.json` path (never the real files - same isolation discipline as the SQLite
tests). It could not verify the actual add/remove-row editing interaction itself -
`AppTest` has no interaction API for `st.data_editor` cell edits (checked directly: the
`dataframe`-typed element it exposes has no edit/set-cell method), only for simpler
widgets like buttons and text inputs. That gap is real: whether adding or removing a row
in the browser actually produces the row list this code expects has not been seen
rendered, only reasoned through from the verified return-value shape above.

## Run tab: a progress callback threaded through closures, not through LangGraph's node signature

Piece 2 of the daily-flow dashboard build: a Run tab that executes `orchestrator.run_nightly()`
directly and shows live progress via `st.status()`, without changing what running
`orchestrator.py`/`pipeline.py` from the CLI prints.

**`pipeline.py` gained a `ProgressEvent`/`ProgressCallback` pair and one call site
(`_emit`) every progress-reporting function routes through.** `on_progress` is an
optional parameter, default `None`, added to `fetch_all`, `_run_analyst_over_jobs`,
`_analyze_stage`, `print_analyst_stage1`, `print_analyst_stage2` - every existing call
site (the CLI dispatch, `orchestrator.py`'s own nodes before this change, any future
caller that doesn't pass it) is unaffected, since `_emit(None, ...)` is a no-op. `print()`
stays the only output for every caller that doesn't opt in - this was the explicit
instruction ("keep the CLI working unchanged"), not just a side effect. `_emit` also
catches and drops any exception the callback itself raises - a bug in the Streamlit
rendering code driving the Run tab must never take down a real nightly run.

**A real bug this design caught before it shipped:** the first version of `fetch_all`'s
per-company loop only emitted a progress event on the *success* path (`continue`
statements on a fetch failure skipped straight past the `_emit` call). A test deliberately
built around an all-failing `FETCHERS` fixture caught this immediately - without the fix,
a UI's live "company N of 62" counter would silently stall short of the real total the
moment any single company failed to fetch, looking exactly like a hang even though the
loop was working fine and moving on to the next company. Fixed by moving the emit before
the branch on success/failure, unconditional either way.

**LangGraph calls a node with just `state` - it has no way to know to supply a second
`on_progress` argument.** Registering `fetch_persist_filter`/`stage1_analyze`/
`stage2_analyze` directly with `graph.add_node` (their new signatures all take an optional
second `on_progress` parameter) would mean LangGraph only ever calls them with `state`,
silently leaving `on_progress=None` regardless of what was asked for. Fixed with closures:
`build_graph(on_progress=None)` defines three tiny wrapper functions (`_fetch_persist_
filter(state)`, etc.) that close over `on_progress` and forward it to the real function,
and registers *those* with LangGraph instead. `build_graph()` with no argument - every
existing call site, the CLI, every test - binds `on_progress=None` into each closure,
which is exactly the original no-callback behavior.

**`print_analyst_stage2` internally re-runs stage 1 as a ranking input, and that's a
real, pre-existing behavior this change surfaces rather than hides.** By the time
`orchestrator.py`'s `stage2_analyze` node runs, `stage1_analyze` already ran this exact
model over these exact jobs moments earlier - so `print_analyst_stage2`'s own internal
stage-1-as-ranking-input pass is almost always 100% cache hits, cheap, but still a real
pass that emits real `stage="stage1"` progress events. A UI subscribed via `on_progress`
will see a brief second "Analyst: scoring N jobs" flash immediately before the real
`stage="stage2"` deep-pass events begin. Restructuring `stage2_analyze` to reuse
`state["stage1_ranked_hashes"]` instead and skip the redundant pass was considered and
declined - out of scope for a progress-callback task, and not something asked for; noted
here so the flash is recognized as accurate-but-cosmetic if it's ever seen, not mistaken
for a bug.

**`orchestrator.py` was deliberately kept out of computing the final "Done - N jobs, M
strong matches" summary.** Graph state can only carry plain-typed content_hashes (see the
LangGraph state-serialization entry higher in this file), not the full `AnalystResult`
verdicts - getting from a content_hash to a verdict requires the same
resume/requirements-hash recomputation `app.py`'s `load_dashboard_jobs` already does.
Rather than duplicating that logic inside `orchestrator.py`, the Run tab computes its own
final summary after `run_nightly()` returns, by re-deriving `JobPosting` objects from
`result["stage2_hashes"]` (or `stage1_ranked_hashes` if stage 2 never ran) and calling
`load_dashboard_jobs` on exactly that subset - the same tested machinery every other tab
already uses, not a second implementation of it.

**`orchestrator.py` (LangGraph) is not imported at `app.py`'s module level.** Measured
before wiring it in, same discipline as the sentence-transformers fix: `import
orchestrator` costs ~0.75s on top of this file's own import time, every single page load,
for a feature used by clicking one button. Deferred to inside the Run button's click
handler instead.

**`st.status()`'s actual API verified live, not assumed**, since a wrong assumption here
would only surface the first time someone actually clicked the button in a browser:
`status.update(label=..., state=...)` and `status.write(...)`, called from a plain helper
function holding `status` as a passed-in parameter - not lexically inside the `with
st.status(...) as status:` block - still correctly target that status container.
Streamlit's "current container" tracking turned out to be dynamic-scope (thread-local),
not lexical-scope, which is what makes `make_run_progress_handler` possible as a
standalone function at all rather than something that has to be defined inline inside the
`with` block.

**Verification limits, stated plainly:** `AppTest` confirmed the Run tab renders, and that
both the success and failure paths run end-to-end without exceptions and produce the
right message, against a mocked `orchestrator.run_nightly` (never the real pipeline - the
batch Scout run was still active in the background for part of this work) and an isolated
temp DB. It could not verify what the live `st.status()` box actually looks like mid-run
in a real browser - the label updates, the quota caption, the expand/collapse behavior are
all reasoned from the verified API behavior above, not seen rendered.

## Applied tracking: a real ALTER TABLE migration, and why applied_at is never cleared

Piece 4 of the daily-flow dashboard build: `applied_at` (nullable datetime) on
`JobPostingRow`, set by `set_application_status` the first time a job is marked
"applied", plus an Applied tab listing everything with it set, most recent first.

**`Base.metadata.create_all()` only creates missing tables, not missing columns on a
table that already exists.** `data/careerpilot.db` already had ~8000 real rows in it
before this change - simply adding `applied_at` to the `JobPostingRow` model would have
left every process hitting the real database with a schema mismatch the moment any code
touched the new column, and `create_all()` alone would never fix it. Added
`_ensure_applied_at_column`, called unconditionally from `get_engine()` right after
`create_all()`: checks via `inspect(engine).get_columns(...)` (one PRAGMA query) whether
`applied_at` exists on `job_postings`, and runs a plain `ALTER TABLE ... ADD COLUMN`
if not - one of the few ALTER forms SQLite supports natively (a single nullable column,
no default), no "rebuild the whole table" workaround needed. Idempotent and cheap on
every subsequent call, so this never needs a separate, rememberable migration step.

**Verified against a real copy of the actual database, not just a fresh `:memory:` one.**
A fresh in-memory test DB never exercises the `ALTER TABLE` code path at all - `create_all`
already includes the column from the model definition on a brand-new table, so a test
that only ever creates fresh databases could pass with a broken migration function and
never notice. Copied the real `data/careerpilot.db` (8049 rows, confirmed no `applied_at`
column beforehand via a direct `PRAGMA table_info` check) to a temp path and ran
`get_engine()` against the copy: column added, all 8049 rows intact, every existing row's
`applied_at` correctly `NULL` (never fabricated a date for historical rows this system has
no real record of), and a second `get_engine()` call on the now-migrated copy didn't raise.
The real database itself was never touched directly - it picks up the same migration
automatically, the next time anything calls `get_engine()` on it for real.

**`applied_at` is set once and never cleared by a later status change - a deliberate
design choice, not explicitly specified.** The brief said "set when I mark something
applied"; it didn't say what happens if the status later moves away from "applied" (to
"rejected", say, recording an outcome). Chose to treat `applied_at` as a historical fact,
not a mirror of current status: once set, a later status change never clears it, and a
second "applied" click never overwrites the original date. The Applied tab lists by
`applied_at IS NOT NULL`, not by `application_status == "applied"` - a job applied to and
later marked rejected still shows up, with its current status noted alongside the date.
The alternative (clear `applied_at` the moment status moves off "applied") would silently
erase the record of having applied at all the first time an outcome gets logged, which
seemed like the wrong trade for a tool whose whole purpose is not losing track of what
happened. Flagged here specifically because it's a judgment call, not something the
spec dictated - worth revisiting if it doesn't match how this actually gets used.

## block-container padding: hiding the header doesn't reclaim the space it reserved

Two bugs reported: a ~150px dead gap above the "CareerPilot" title, and excess space at
the bottom of the page. Both were the same root cause.

**Diagnosis, this time actually verified in a real browser, not reasoned from source.**
Earlier CSS work this session (see the "CSS bugs with no real browser to check them in"
entry above) was explicit about not having real-browser access - every fix up to now was
the strongest defensible hypothesis from code review plus a static grep of the installed
streamlit package's JS bundle, never seen rendered. That static-grep approach hit a real
limit this time: Streamlit's block-container padding isn't in any static `.css` file in
the package at all (the one shipped `index.*.css` is 1069 bytes, clearly not the real
app stylesheet) - modern Streamlit generates its styles at runtime via emotion
(CSS-in-JS), so there was nothing to grep. Installed Playwright + Chromium into the venv
(not added to `requirements.txt` - a temporary verification tool, not a project
dependency, uninstalled again after) and drove the actual running dashboard
(`streamlit run app.py` for real, against the live `data/careerpilot.db` - read-only
navigation and screenshots only, no clicks on anything that writes) to read the real,
live computed styles.

**Measured:** `[data-testid="stMainBlockContainer"]` (the same element `.block-container`
aliases to) carries `padding-top: 96px` and `padding-bottom: 160px` by default - reserved
so content clears the header bar and never sits flush against the bottom edge. The
existing chrome-hiding rule (`visibility: hidden; height: 0` on `stHeader`/`stToolbar`/
etc.) removes the header itself but was never going to touch this - it's a separate
property, on a different element, that Streamlit sets unconditionally regardless of
whether the header it's making room for is visible. With the title only, the real
"CareerPilot" `<h1>` sat 112px down from the true top of the viewport.

**Fix:** `[data-testid="stMainBlockContainer"] { padding-top: 1.5rem; padding-bottom:
2rem; }` - a small deliberate value on both sides, not `0` (the title should sit "near
the top", not pinned to the literal edge). Re-verified live after the change: 24px/32px,
`<h1>` now at 40px from the top - confirmed via the browser's actual computed styles, not
just re-read from the CSS source. Used the same `data-testid` selector convention as the
existing chrome-hiding rule, not the co-existing `.block-container` class alias, for the
reason already documented there: `data-testid` is the one Streamlit's own testing
conventions treat as stable across versions.

**Checked what was asked, not just the happy path:** re-measured the computed padding at
five viewport widths (1920/1400/1024/800/640px) - identical 24px/32px at every one, so
nothing about this fix is width-conditional or fighting a responsive breakpoint
Streamlit defines elsewhere. This app never calls `st.sidebar` anywhere in `app.py`, so
"holds when the sidebar is absent" is this app's only real state, not a second
configuration to separately verify - confirmed there's no sidebar-conditional CSS at
play by grepping for `sidebar`/`stSidebar` in `app.py` and finding nothing.

**Side effect worth stating plainly:** running the dashboard for real (even read-only)
against `data/careerpilot.db` triggered `get_engine()`'s `_ensure_applied_at_column`
migration (see the Applied-tracking entry above) for real, for the first time, since that
was built and only tested against a copy up to now. Confirmed afterward: row count
unchanged (8049), `applied_at` column now present - exactly the designed, harmless
behavior, not a surprise, but worth naming since it's a real write to the real database
that happened as a side effect of a CSS verification task, not something done on purpose
for its own sake this session.

## FastAPI layer: three bugs the tests didn't catch, and what did catch them

Built `api/` as an HTTP wrapper over the existing modules for the React migration.
`orchestrator.py` needed **zero** changes: the `ProgressEvent`/`ProgressCallback` seam built
for the Streamlit Run tab was already the structure the SSE stream needed. `pipeline.py`
gained one optional field (`agent`) plus token capture; everything else new lives under
`api/`.

**The composition trick that kept the blast radius small.** `MetricsCollector` is itself a
`ProgressCallback`. The run endpoint composes it with the SSE publisher and passes the pair
into the callback parameter that already existed, so neither `orchestrator.py` nor
`pipeline.py` knows metrics or SSE exist. Nothing in the nightly path had to change to make
per-run metrics possible.

Three real bugs, none caught by the unit tests, each found by a different kind of check:

**1. Live SSE was silently dead in production - found by running a real server.** FastAPI
dispatches `def` (non-async) endpoints to a threadpool. `POST /api/run` is one, so
`asyncio.get_running_loop()` raises inside `RunManager.start()`. The first version assigned
`self._loop = None` there, wiping the loop and making every live dispatch a no-op. Replay
from the buffer still worked perfectly, so `/api/run/status` and a post-run stream both
looked correct - only a browser watching a *live* run would have seen nothing, and only
after the frontend existed. In-process `TestClient` never exercised it because it never runs
a real threadpool dispatch against a real loop. Driving uvicorn with httpx did, immediately.
Fixed by registering the loop from the app's lifespan (which does run on the loop) and never
letting `start()` clear it.

**2. A broken subscriber could still escape the worker thread - found by the test the user
asked for.** `publish()` was guarded, but `_finish()` called `_dispatch` unguarded, so an
exception there propagated out of the worker's `finally` block as an unhandled thread
exception. The run outcome survived only because the state write happened to come first.
Fixed by guarding `_finish` and broadening `_dispatch`'s catch.

Worth recording separately: the **first regression test for this was useless**. It asserted
on `recwarn`, but pytest raises `PytestUnhandledThreadExceptionWarning` at *teardown*, which
`recwarn` inside the test body never sees - so it passed with the bug deliberately
reintroduced. Rewritten to hook `threading.excepthook` directly, then verified the way any
regression test should be: confirmed it fails with the bug present and passes with the fix.
A test that passes either way is worse than no test, because it advertises coverage it
doesn't have.

**3. A test overwrote the user's real `data/preferences.json`.** `config.save_preferences`'s
signature is `save_preferences(preferences, path=PREFERENCES_PATH)` - a default argument,
bound once at import. Monkeypatching `config.PREFERENCES_PATH` therefore did nothing, and the
write went to the real file. Exactly the same early-binding trap as `db.get_engine`'s
`db_path` default, hit again in a new place. No data was lost (the file was already at
defaults and a later test's reset rewrote the identical content - verified by comparing to
`DEFAULT_PREFERENCES` rather than assuming), but that was luck, not design. Fixed two ways:
the router now passes the path explicitly, and `conftest.py` wraps `save_preferences` so a
write outside `tmp_path` raises instead of succeeding quietly. The same class of bug also hit
`from app import RESUME_PATH` in the resume router - import-time binding again, caught by a
test asserting the file contents rather than just the status code.

**Honesty constraints encoded in the API surface**, because `/api/meta/*` is recruiter-facing
and this is exactly where rounding a null up to a plausible number is most tempting and most
damaging:

- `retries` is `null`, never `0`. LangGraph retries nodes silently; nothing measures it. Null
  means unmeasured. A 0 would be a claim nobody verified.
- `/api/meta/evaluation` reports `available: false` when no snapshot exists, rather than
  zeros - 0.000 across the board reads as "measured, and terrible" when the truth is "not yet
  run". The real numbers it now serves are unflattering and shipped as-is: stage-1 MRR 0.132
  against a random baseline of 0.121. The caveats (stage 1 only ever saw filter survivors;
  the 'good' count is small; random is expected-value not sampled) ship *with* the numbers,
  in the same response.
- `agent` is null for fetch/filter. They're pipeline stages; only the Analyst runs nightly.
  Labelling them with an agent name would misrepresent what the system does.
- Quota and token figures are a floor, stated as such - CLI runs spend the same quota and
  write no metrics row.
- `test_count` is functions defined, not a pass result. Running pytest per request would let
  an HTTP endpoint report green while the suite is red.

**`_ADDED_COLUMNS`.** `create_all()` only creates missing tables, never missing columns on an
existing one - so `total_tokens` was absent from the real database's `run_agent_metrics`
(created minutes earlier, before the column existed) and `/api/meta/runtime` failed with "no
such column". Generalized the one-off `applied_at` migration into a table-driven list.
Appending to it is now the whole procedure for adding a nullable column.

**WAL enabled** (`PRAGMA journal_mode=WAL`) so the API can serve reads while a background run
writes. SQLite's default rollback journal takes a database-wide exclusive lock for the
duration of a write; "watch the run progress while browsing jobs" is the normal case for
Mission Control and would otherwise intermittently 500.

**The `app.py` coupling is deliberate and contained.** `api/services/dashboard.py` re-exports
`load_dashboard_jobs`, `run_filter_pass` and friends from `app.py` - tested code that is
importable without Streamlit (its `import streamlit` sits inside `__main__`). Routers import
only from that shim, never from `app` directly, so deleting `app.py` after the React frontend
lands is a one-file edit rather than a hunt through seven routers.

## Frontend types are generated from the API's own OpenAPI document, not hand-written

First slice of the React app: shell, routing, mode toggle, Jobs page.

**The decision worth recording is the type boundary.** `frontend/src/api/schema.d.ts` is
generated by `openapi-typescript` from `frontend/openapi.json`, which `gen_openapi.py`
writes straight out of `api.main.app.openapi()`. Nothing about the response shapes is
retyped by hand on the TypeScript side. A renamed or retyped Pydantic field breaks
`tsc -b` immediately, rather than surfacing as `undefined` in a browser at 8am.

This paid for itself during the very first typecheck, which caught three real problems a
hand-written interface would have asserted away:

- `matched_skills` / `missing_skills` are **optional** in the generated schema. Pydantic
  fields with `default_factory=list` are non-required in OpenAPI, so the honest type is
  `string[] | undefined` - and `JobCard` was calling `.length` and `.map()` on them
  unguarded. Fixed with a default parameter rather than a non-null assertion.
- `verdict` is a plain `string` at the boundary, not a union. The card now falls back to
  the "no comparison" treatment for any verdict it doesn't recognise, so an unexpected
  value renders as uncertain rather than being displayed raw as if it were authoritative.
- Constructor parameter properties (`constructor(readonly status: number)`) are illegal
  under this project's `erasableSyntaxOnly` tsconfig, since they emit runtime code.

**Verified against both servers running, in a real browser** (Playwright, installed
temporarily and uninstalled afterwards - it is not in `requirements.txt`): Home and Jobs
render live data from the real database (8,174 fetched, 41 survived, 41 analyzed), zero
console errors, Recruiter Mode persists across reload, and `/evaluation` redirects to
Home when the mode is off but renders when it's on. The mode is a route guard, not just
a hidden nav item - a bookmarked recruiter URL shouldn't bypass the toggle.

**The status-write path was tested end-to-end and then reverted.** Deliberately using
`interviewing` rather than `applied`: `applied_at` is set once and never cleared by
design (see the Applied-tracking entry above), so testing with `applied` would have
written a permanent, false application record into the user's own history. The one job
touched (GitLab, Intermediate Fullstack Engineer) was returned to `new` and confirmed
back at `new` in the database afterwards.

**A measurement error worth recording, because the first result was wrong in a way that
looked right.** The initial write test read `jobs[0]` from the API and checked that row's
status in SQLite - but the Jobs page filters to new+interviewing by default, so the first
*visible card* was a different job than the first *API result* (which was an already-
`applied` GoHighLevel row). The status genuinely had changed; I was reading the wrong
row and would have reported the write path broken. Fixed by selecting the target the same
way the UI does, then re-verifying. The lesson generalises: when a UI applies a filter,
"first item in the response" and "first item on screen" are different things, and a test
that conflates them can report either a false failure or a false pass.

**Placeholders state what isn't built.** The six unbuilt screens render a short note
naming the endpoint that already backs them, rather than mock cards or sample charts.
On the recruiter-facing pages especially, a convincing mock is worse than an empty state -
those pages exist to show measured reality.

## Mission Control, and the silent no-op that a real run exposed

Built the SSE run experience: agent timeline, live progress, quota, and per-stage metrics
under Recruiter Mode. Verified against a real orchestrator run, not a mocked stream.

**The verification method is the point.** A dead SSE dispatch path is invisible to a test
that only checks the final timeline, because the replay buffer reconstructs a perfectly
correct-looking result at the end regardless. The only thing that separates live streaming
from replay-on-completion is whether the client's event count grows *while the run is still
running*. So the check samples both sides every 4 seconds and compares:

```
server_state  server_events  ui_events   stage
running                  13         13   fetch
running                  30         30   fetch
running                  63         63   fetch
running                 108        108   stage1
running                 167        167   stage2
completed               169        169   stage2
```

169 events, tracked in lockstep, all four stages executed for real (62 companies fetched,
43 survivors, 15 deep-passed). A mid-run browser reload recovered 9 events instantly from
the replay buffer, confirming replay-on-connect against a live run rather than a fixture.
Cost: 3 LLM calls total (2 stage-1, 1 stage-2) - the cache absorbed the other 98.

**What the real run exposed: "Start run" did nothing, silently.** The first attempt
completed with **zero events** while the API log showed a full final state. Nothing was
broken in the SSE layer. `orchestrator.run_nightly` keys each run to a date-based thread
(`nightly-YYYY-MM-DD`) and resumes it by passing `None` to `invoke()`. Today's thread had
already reached END during the 04:02 cron run, so LangGraph correctly returned the
checkpointed final state without executing a single node. Zero events was the *correct*
consequence - confirmed by the absence of any `Fetching jobs for 62 companies...` line in
the log, and by both `nightly-2026-08-05` and `nightly-2026-08-06` already existing in the
checkpoint database.

That idempotency is deliberate and worth keeping: it's what stops a 2am cron doing the
work twice if it fires twice. But from a UI it is indistinguishable from a broken button -
empty timeline, no error, status "completed". Two additive changes:

- `POST /api/run` now accepts an optional `thread_id`, forcing a genuinely fresh graph
  thread. Omitted on the normal path, so date-based crash-resume is untouched.
- Mission Control detects the no-op (completed with zero events), explains *why* nothing
  ran in the user's own terms, and offers "Run again on a fresh thread". The four stages
  render as **Skipped**, not "Waiting" - waiting would imply work still to come.

**Stage state is derived from the highest-sequence event, never from "have I seen this
stage before".** `print_analyst_stage2` re-runs stage 1 as a ranking input before the deep
pass, so `stage1` events arrive in two separate bursts with `stage2` after them. Anything
keyed on first-appearance would show the screening pass flickering back to "running" after
it had completed.

**Honesty in the metrics panel held up under real data.** `retries` renders as *not
measured* on every stage - never 0 - because LangGraph retries nodes silently and nothing
observes them. Same treatment for `cache_hit_rate` on fetch/filter (no LLM involved) and
`companies_checked` on the Analyst stages. The real numbers appear where they were
genuinely measured: fetch 101.64s across 62 companies, stage-1 98% cache hit rate on
gemini-3.5-flash-lite, stage-2 93% on gemini-2.5-flash.

**A test-harness mistake worth recording.** The first script clicked `nth(0)`, `nth(1)`,
`nth(2)` of "Show stage metrics" - but clicking flips the label to "Hide stage metrics",
so the matching set shrinks as you go and `nth(2)` times out. Fixed by re-querying and
always clicking the first remaining match. The same shape of bug as the earlier
first-API-result-vs-first-visible-card error: a selector evaluated once against a list
that the act of using it changes.

## The remaining seven screens, and the two bugs the build surfaced

Resume, Applications, Career Coach, Settings (User Mode); Architecture, Evaluation, Agent
Metrics (Recruiter Mode). Every one verified against the live API in a browser, with
console errors and the ErrorState component both treated as failures.

**One additive backend endpoint was needed: `GET /api/meta/runs`.** `/api/meta/agents`
reports the single most recent run, which can't back a per-run history view. The new
endpoint returns runs newest-first with their per-stage metrics, sharing one
`_to_agent_metric` mapper with `/agents` so the two views can't drift into subtly
different metric shapes. `duration_seconds` is null - not zero - for a run with no
`finished_at`, since "crashed or still running" and "took no time" are different facts.

**A real timezone bug, caught by reading the screenshot rather than the code.** Agent
Metrics displayed run timestamps 5.5 hours early. Backend datetimes are naive UTC
(`db.py`'s `_to_naive_utc` strips tzinfo on every write), so they serialize without a `Z`,
and `new Date("2026-08-06T15:05:59")` is interpreted as **local** time by every browser.
`formatAge`/`formatDate` already appended the `Z`; the two new pages called
`new Date(...).toLocaleString()` directly and inherited the shift. Fixed by extracting
`parseUtc` and a `formatDateTime` helper, and confirming no raw `new Date(` on an API
datetime remains anywhere in `src/`. The fix was verified by re-reading the rendered
timestamps: `15:05:59` UTC now displays as `20:35:59` IST.

**A non-bug worth recording, because it looked exactly like one.** Alongside those
timestamps, "Quota today" read `0 / 500` even though the run visibly made 3 LLM calls.
That is correct: the run happened at `2026-08-06T15:05` UTC and the check ran at
`2026-08-07T03:59` UTC, so the run was on the *previous* UTC day and today's spend
genuinely is zero. Checked before "fixing" it - the endpoint was right and the instinct to
patch it would have introduced a bug rather than removed one.

**Optional-array types, again.** Twenty more `TS18048` errors, all the same root cause
already recorded above: Pydantic fields with `default_factory=list` are non-required in
OpenAPI, so every one arrives as `T[] | undefined`. Fixed with `?? []` at each use rather
than by loosening the generated types, which is the whole point of generating them.

**Honesty rules, as actually implemented:**

- Agent Metrics renders `retries` as *not measured* in italics on every stage, never `0` -
  LangGraph retries silently and nothing observes them. Same treatment for `model` and
  `companies_checked` on stages where they don't apply, and for `cache_hit_rate` when
  nothing was looked up (no lookups ≠ every lookup missed).
- Evaluation leads with a plain-language verdict rather than a table a reader has to
  interpret: "The LLM ranking is only marginally above chance", stage-1 MRR **0.132**
  against a random expectation of **0.121**, a margin of 0.011 on **n = 34**, with the
  overlap caveat (only 34 of 122 labeled jobs have a stage-1 score) shown beside the
  numbers rather than beneath them. The embedding row is badged *at chance* by the same
  rule. Nothing is rounded, hidden, or re-scaled to look better.
- Applications lists by `applied_at`, not by current status, so a job applied to and later
  rejected still appears. The two rows marked applied before the column existed are shown
  in a separate "date not recorded" group rather than being dropped or given a fabricated
  date.
- Resume surfaces the backend's own extraction tripwires. A deliberately truncated paste
  during verification correctly triggered both the length-drop warning ("93 characters,
  down from 3,259 - 3% of the previous length") and the header-fallback warning, and
  nothing was saved.
- Coach shows retrieval provenance with every answer - retrieved count, pool size,
  population, k - plus an explicit warning when retrieval was a no-op, because an answer
  formed over the entire pool deserves less trust than one formed over a selective search.

**Verification touched real services deliberately**: two live Coach questions (real
retrieval + LLM call, confirmed `retrieved 8 of 43 in scope`, not a no-op) and a real
resume preview. No resume was saved, no preferences were written, and no company was added
- every write path was exercised only up to the point before it commits.

## Workday adapter: 9 real tenants found, real request cost measured, companies.yaml left untouched on purpose

Built only after confirming real tenants existed - the brief's own condition ("if it's fewer
than 8, stop"). Checked the 89 not-on-Greenhouse/Lever/Ashby companies from the platform
survey above plus large Indian/India-hiring employers generally, for a `myworkdayjobs.com`
CSP reference or redirect, then verified each live CXS candidate for real India postings.

**Result: 9 tenants confirmed** (robots.txt-allowed, live-API-confirmed, India postings on the
first page): BrowserStack, Uniphore, Visa, HPE, Zebra, Salesforce, Harris Computer (Altera),
Thomson Reuters, Micron. Four other plausible guesses (Cisco, Boeing, Autodesk, Motorola
Solutions) resolved to real, robots-allowed tenants with zero India postings on the first
page - correctly excluded, not silently dropped.

**Two corrections to the investigation brief, both found by testing the live API before
coding against it, not by trusting the brief's description:**

1. The offset-wrap point is not a fixed 2000. Verified on a ~1,461-job tenant (Salesforce):
   `total` reads correctly at offset 0, reports `0` through the entire middle of pagination
   while still returning genuinely distinct postings, then reads correctly again only once
   offset has wrapped back near 0 - the wrap happens near the tenant's *own* total, not a
   universal constant. `OFFSET_HARD_CAP` in `adapters/workday.py` is a circuit breaker for
   totals this code hasn't seen yet, not the primary stop condition; the real stop is
   `offset >= total` (trusted from page 1 only) or a detected first-job repeat, whichever
   comes first.
2. "500, non-JSON, `response.json()` throws on both" is real, but on Workday's human-facing
   HTML site, not the `/wday/cxs/.../jobs` JSON API this adapter actually calls. Tested both
   surfaces directly: the CXS API returns HTTP 422 for a nonexistent tenant and HTTP 404 for
   a wrong `site`, both with valid, parseable JSON bodies. Both are still treated as
   `BoardNotFoundError` (either one means the companies.yaml entry is wrong), and
   `response.json()` is still never called unguarded - a WAF page or network-level
   interception could still hand back HTML on either status.

**A third finding the brief didn't anticipate: `remote_type` inference must run on
location text, because Workday's own `remote` field is unreliable.** Checked across two
tenants - `remote` read `null` on every real posting, including ones with a clearly
non-remote city location. Same conclusion this project already reached for Ashby's
`isRemote` (see the location-filter asymmetry note in CLAUDE.md). `_to_job_posting` never
reads that field at all; `infer_remote_type(location)` does the same word-boundary
inference the other three adapters fall back to.

**A fourth: Workday's `location` string format is set per tenant, not fixed Workday-wide.**
One tenant gives `"Bengaluru, India"` (comma-delimited, city-first - already
`models.normalize_location()`'s assumed shape); another gives `"India - Bangalore"`
(dash-delimited, country-first) for the same kind of posting. `_normalize_location` in
`adapters/workday.py` flips only a plain two-part `"X - Y"` with no existing comma to
`"Y, X"` before storage; anything already comma-shaped, or a more complex multi-part
string, passes through unchanged rather than being guessed at.

**`CompanyConfig` extended without touching the other three adapters' function
signatures.** `token` became `Optional`, three new `Optional` fields
(`workday_tenant`/`workday_wd`/`workday_site`) were added, and a `model_validator` enforces
that Workday entries set all three and never `token`, while every other source requires
`token` and forbids the three Workday fields - a companies.yaml typo (wrong ATS's fields
present, or a half-filled Workday entry) fails at load time naming the specific problem,
not as an `AttributeError` deep inside `fetch_all`. `pipeline.py`'s `FETCHERS` dispatch
changed from `Callable[[str, str], list[JobPosting]]` to `Callable[[CompanyConfig],
list[JobPosting]]`, with the three existing fetchers lambda-wrapped at the dispatch site
only - `adapters/greenhouse.py`, `lever.py`, and `ashby.py` are unmodified.

**One real regression this surfaced, caught by the test suite before it shipped:**
`tests/test_api_companies.py::test_add_company_rejects_an_unknown_ats` used `"ats":
"workday"` as its example of a value `ATSSource` doesn't recognize - written before Workday
existed. Once `ATSSource("workday")` started succeeding, that request fell through to
`CompanyConfig(...)`, which raises a `pydantic.ValidationError` for a Workday entry with
`token` set instead of the three Workday fields - and FastAPI does not auto-convert a
manually-raised `ValidationError` inside a route body to an HTTP 422 (only its own
request-parsing validation gets that treatment), so this would have been an unhandled 500
in production, not just a failing test. Fixed with an explicit `ats == ATSSource.WORKDAY`
check in `add_company` (this endpoint has no fields to collect
`workday_tenant`/`workday_wd`/`workday_site`, so Workday is rejected with a 422 explaining
why, directing the addition to companies.yaml by hand) plus a general
`ValidationError -> HTTPException(422)` wrapper as defense in depth for any future source
with its own cross-field requirement.

**Real per-company request cost, measured against a live tenant end-to-end, not
estimated.** Ran `fetch_jobs` against Harris Computer's Altera board (`harriscomputer.wd3`,
site `altera`) with `requests.request` wrapped to count calls:

| | value |
|---|---|
| jobs returned | 69 |
| listing requests | 4 |
| detail requests | 69 |
| **total requests** | **73** |
| elapsed | 97.5s (real network + the adapter's own 0.3s pacing) |
| requests per job | 1.00 (detail) + a small shared listing overhead |

The N+1 shape holds exactly as designed: essentially 1 request per job plus roughly 1
listing request per 17-20 jobs. This also caught a discovery-script artifact worth
recording: the Step 1 survey's counting probe searched with `searchText="engineer"` to
keep the discovery pass cheap, which **undercounts** total board size - Harris/Altera read
41 total in the survey, 69 for real, because `fetch_jobs` itself searches with no text
filter (rule-based title filtering happens after fetch, same as every other source, per
CLAUDE.md's architecture). The 9-tenant count from Step 1 is still correct (it only needed
"real India postings present", not an exact volume), but any board-size number from that
survey understates the true fetch cost - corrected below with one real `total`-reading
request per tenant (search-free, same query `fetch_jobs` makes) rather than a guess:

| company | real total jobs | est. requests (`total + ceil(total/20)`) |
|---|---:|---:|
| BrowserStack | 33 | 35 |
| Uniphore | 37 | 39 |
| Harris Computer (Altera) | 69 | 73 |
| Thomson Reuters | 431 | 453 |
| Zebra | 244 | 257 |
| Visa | 759 | 797 |
| HPE | 1,096 | 1,151 |
| Salesforce | 1,462 | 1,536 |
| Micron | 2,707 | 2,843 |
| **all 9** | **6,838** | **7,184** |

**This is the number that decides the cadence, and it says on demand, not nightly.** At the
measured pace (97.5s / 73 requests $\approx$ 1.3s/request, real latency plus the adapter's
own throttling), fetching all 9 tenants unconditionally would cost roughly 7,184 requests
and **2.5+ hours** - dwarfing the rest of the pipeline, which fetches ~8,300 postings from
62 Greenhouse/Lever/Ashby companies in about 100 seconds (one paginated GET per company,
descriptions included in the listing response - no N+1). Folding all 9 into the
unconditional nightly `fetch_all()` would make Workday the dominant cost of every run, not
a fresh source alongside the existing ones.

**companies.yaml was deliberately left untouched - adding these entries is a cadence
decision, not a mechanical one.** `pipeline.py`'s `fetch_all()` has no per-company cadence
knob today: every entry in companies.yaml is fetched on every invocation, nightly or
otherwise. Appending all 9 Workday tenants as-is would silently make every future run -
including the 2am cron - take 2.5+ hours longer and issue thousands of requests against
other companies' infrastructure on a fixed schedule, which is a different kind of load than
anything the three existing adapters produce and not a decision to make by editing a YAML
file quietly. The three small tenants (BrowserStack, Uniphore, Harris/Altera - 147 requests,
about 3-4 minutes combined) are cheap enough that folding just those into the nightly run
would barely register; the four large ones (Visa, HPE, Salesforce, Micron - 6,327 requests
between them) are not, and are better suited to `python -m adapters.workday <company>
<tenant> <wd> <site>` run on demand, or a separate slower-cadence job, until `CompanyConfig`
or the pipeline gains an actual cadence field. Left for a decision on which tenants (if any)
go into companies.yaml and at what cadence, rather than assumed.

### Decision: only the three small tenants added; the real survivor count came back thin

BrowserStack, Uniphore, and Harris Computer (Altera) added to companies.yaml as `ats:
workday` entries; Visa, HPE, Salesforce, and Micron deliberately left out - the request
volume for a "handful of fresher roles" was judged disproportionate load on someone else's
infrastructure for on-demand-only use, not folded into any schedule.

Ran the real pipeline (`pipeline.py --filtered`, all 65 companies, real fetch + persist +
rule filters) to get the number that was supposed to decide whether the four large tenants
are ever worth adding:

| company | jobs fetched | unique stored (content-hash dedup) | survived filters |
|---|---:|---:|---:|
| BrowserStack | 33 | 17 | 0 |
| Uniphore | 37 | 37 | 0 |
| Harris Computer (Altera) | 69 | 57 | **1** |

The one survivor: Harris Computer (Altera), "Software Engineer", Remote Pune-Baroda,
India, experience not stated. Everything else was rejected - mostly `not_allowlisted` and
`seniority`, the same two rules doing most of the rejecting pipeline-wide (4,193 and 2,374
of the run's 8,317 total rejections). BrowserStack and Harris's fetched-vs-stored gap (33
vs 17, 69 vs 57) is `compute_content_hash` dedup on (company, title, location) doing its
normal job, not a bug - multiple real requisitions sharing a title and location collapse
to one stored row, same as any other source.

**1 new survivor for 111 unique jobs pulled (139 raw fetches, 147 real requests) is a
~0.9% yield** - roughly in line with the whole pipeline's own 41/8,358 (~0.5%), not
obviously worse. Naively scaling that rate to the four large tenants' combined ~6,024 real
jobs would suggest something like 40-50 more survivors - but that's an extrapolation from
a single observed survivor across three companies, not a measurement, and shouldn't be
treated as one. The honest read: the three small tenants earned their spot in the nightly
run on cost alone (147 requests for 1 real match is still cheap), and whether the large
tenants are worth their much higher cost stays an open question this single data point
doesn't settle - the next real signal is whichever of the three added companies keeps
producing survivors over the following nights, or an actual on-demand run against one of
the four large tenants when there's a specific reason to check it.

## A second Workday batch, a facet-based India count that's actually trustworthy, and Cisco/Adobe added

Checked 11 more named companies for Workday tenants: Amazon, Microsoft, Oracle, IBM, SAP,
Walmart Global Tech, JPMorgan Chase, Cisco, Qualcomm, Intel, Adobe. Verified directly
(redirect + CSP + page source), not assumed from company size or reputation.

**7 of 11 are not on Workday at all.** Amazon (`amazon.jobs`, custom), Microsoft
(`careers.microsoft.com`, custom), Oracle (no signal - plausibly because Oracle sells a
competing recruiting product), IBM (`careers.ibm.com`, custom), SAP (`jobs.sap.com`, no
signal - plausibly because SAP owns SuccessFactors, also a competitor to Workday),
JPMorgan Chase (no signal, despite several SEO/ATS-tip sites confidently claiming
otherwise - direct inspection found nothing, so those claims were treated as unverified,
not as fact). **Qualcomm is a genuinely interesting case**: `qualcomm.wd5` and
`qualcomm.wd12.myworkdayjobs.com` are real former tenants that still resolve, but the page
itself now reads "We've moved sites. Follow the link below to view current Qualcomm jobs
careers.qualcomm.com" - a dead Workday deployment, not a live one. Caught by fetching the
actual HTML and reading it, not by trusting a 200 status code or a non-empty JSON facets
array (which the retired tenant still returns).

**A methodology upgrade, forced by a bad first result.** The first pass at "how many India
postings" reused the original survey's method (free-text `searchText="India"` + scanning
page 1). It produced garbage: a job in Mccordsville, Indiana matched via a location string
containing "IN" and a leftover substring bug in the checking script counted it as India;
Adobe's count mixed in Hamburg and generic-market roles that merely mentioned India in
running text. Replaced with Workday's own `locationMainGroup` facet - an exact per-city
count scoped by an `India` word-boundary match, applied to a response already filtered by
the `jobFamilyGroup` engineering facet - a real filtered count sourced from the ATS's own
aggregation, not a text-search guess. **This also reversed the first Workday batch's
verdict on Cisco**: that batch's page-1 substring scan reported 0 India postings for
Cisco and excluded it; the facet-based count found 205 real India engineering postings
across 12 cities (162 in Bangalore alone). The earlier exclusion was a shallow-methodology
artifact, not a real absence - worth remembering the next time a "0" comes back from a
first-page scan.

| company | tenant / wd / site | robots.txt | India engineering postings (facet-based) | est. requests |
|---|---|---|---:|---:|
| Walmart Global Tech | `walmart` / `wd504` / `WalmartExternal` | allowed | 0 | ~2,100 |
| Intel | `intel` / `wd1` / `External` | allowed | 48 | ~689 |
| Adobe | `adobe` / `wd5` / `external_experienced` | allowed | 98 | ~839 |
| Cisco | `cisco` / `wd5` / `Cisco_Careers` | allowed | 205 | ~1,169 |

**Walmart's `total` field reads exactly `2000` on every query tried - filtered or not.**
That round a number, unmoved by an applied facet that should have shrunk the count, is
consistent with a Workday-side display cap on the counter itself rather than the tenant's
real size. The India-engineering count of 0 is still trustworthy (it came from the
location facet's own per-value counts, not the capped top-level `total`), but this tenant
is exactly the shape of case the next finding is about.

**Decision: Cisco and Adobe added to companies.yaml; Intel and Walmart left out.** 205 +
98 = 303 India engineering postings for roughly 2,000 combined requests is the best ratio
either Workday batch has produced - clearly ahead of the first batch's 1-survivor result
for 147 requests. Intel's 48-for-689 is real but weaker, left for later if Cisco/Adobe
prove out over real nights. Walmart's 0 India engineering postings makes its request cost
pointless regardless of the total-cap question.

## Pagination-stop bug: a capped `total` was a silent-truncation risk, fixed before any large tenant used it

Flagged by the Walmart finding above and fixed immediately, since it would bite any large
enough tenant, not just Walmart: `fetch_jobs`'s pagination loop originally stopped on
`offset >= total`, trusting the `total` read from page 1 (module docstring, point 3
already established that read as the *only* trustworthy one - true, but "trustworthy" and
"uncapped" turned out to be different claims). A tenant whose real size exceeds whatever
cap Workday applies to that counter would have had its fetch silently end early, with no
error, no log line, nothing to notice - `fetch_jobs` would just return fewer jobs than the
tenant actually has, indistinguishable from a tenant that genuinely has that many jobs.

**Fix: `total` is no longer part of the stop decision at all.** It's still read from page
1 (kept as a local, now genuinely just for potential future diagnostics), but pagination
now stops only on a short page (fewer than `MAX_PAGE_SIZE` postings - nothing left) or a
detected wrap (a repeated first-job id), exactly the two conditions that are actually
observed from real response data rather than a number the server reports about itself.
`OFFSET_HARD_CAP` remains the circuit breaker for a tenant that triggers neither. Cost:
at most one extra request, in the edge case where a tenant's true size happens to be an
exact multiple of `MAX_PAGE_SIZE` (the last real page is full-size, so one more request is
needed to discover the following page is empty) - negligible next to the risk it closes.

**Locked in with a new regression test**, `test_fetch_jobs_ignores_total_and_stops_on_the_
short_page_instead`: page 1 reports a total that claims the board ends right there, but two
more full pages of genuinely new postings follow. The old code would have stopped after
page 1 and silently dropped both; the fixed code fetches all three pages and returns every
job. Not run against Walmart itself (Walmart wasn't added), but the exact shape of tenant
that would have triggered this is now covered structurally, for whichever future tenant
turns out to be large enough to hit it.

### Decision result: Cisco and Adobe added, and the real survivor count landed far better than the first batch

Ran the real pipeline (`pipeline.py --filtered`, all 67 companies) after adding Cisco and
Adobe. Real, DB-verified numbers (`rejection_rule IS NULL` per company, not the console's
raw per-run print count - see below for why those two differ):

| company | jobs fetched | unique stored (content-hash dedup) | survived filters |
|---|---:|---:|---:|
| Cisco | 1,113 | 1,002 | **37** |
| Adobe | 799 | 734 | **9** |

**46 genuinely new survivors for ~1,912 requests** (the two real fetches came in slightly
under the ~2,008 estimate) - a dramatically better outcome than the first batch's 1
survivor for 147 requests, and it validates the facet-based India-engineering count
(205 + 98 = 303) as a real leading signal, not just a bigger number that happened to look
good.

**One honest gap worth naming: 303 was never going to become 303 survivors, because it
measured the wrong filter.** The facet count was India *engineering* postings - it said
nothing about seniority. `seniority` was the single largest rejection reason for both
companies (310 of Cisco's 1,002, 523 of Adobe's 734 - over 70% of Adobe's rejections
alone), and eyeballing the fetched titles confirms why: Cisco's board is dense with
"4-8 Years", "8-11 Years", "9-12 Years" in the title itself. The facet count was the right
signal to justify the request cost (it's what made Cisco/Adobe obviously better than
Intel/Walmart), but it was never going to predict the fresher-survivor count directly -
that's what running the real pipeline was for.

**A second dedup finding, Cisco-specific**: 1,113 fetched collapsed to 1,002 unique stored
rows - about 10% of Cisco's board is exact title+location duplicates (multiple identical
"Software Engineer | Bangalore, India" requisitions, presumably distinct real openings
with the same title and city). Adobe's collapse was smaller (799 -> 734, ~8%). Neither is
a bug - `compute_content_hash` doing its documented job - but it means the console's
per-run "Survived filters: 110 / 10284" print (which counts every fetched job, before
dedup) overstates what actually lands as new distinct rows on the dashboard; the DB-level
per-company count above is the one that matches what the user actually sees at 8am.

## Per-company cadence: nightly by default, weekly for Cisco and Adobe

1,912 requests and roughly 40 minutes for two companies is disproportionate to run every
single night, once Cisco and Adobe's real survivor counts (37 + 9 = 46, see above) showed
they were worth keeping at all - enterprise ATS boards don't turn over daily enough to
justify paying that cost 7 nights a week for the same handful of new postings a weekly
check would also catch.

**`models.Cadence`** (`NIGHTLY` default, `WEEKLY`) is a new field on `CompanyConfig`, not a
new adapter concern - `nightly`/`weekly` describes a company's own request-cost profile,
not anything about which ATS it's on. Every existing companies.yaml entry is unaffected by
the default.

**Where "last fetched" lives**: a new `company_fetch_state` table (`company_fetch_state`,
keyed on company name), not a YAML sidecar file. This is genuinely runtime state produced
by a real successful fetch, not hand-edited config - `data/careerpilot.db` is where every
other piece of state like this already lives, and it's a brand-new table, not a new column
on an existing one, so it needed no entry in db.py's `_ADDED_COLUMNS` migration list -
`Base.metadata.create_all()` handles a missing table on its own.

**`pipeline.fetch_all` gained `session` and `force` params, and its return became a
3-tuple**: `(jobs, failures, skipped)`. A company skipped for not being due yet is neither
a success nor a failure - folding it into `failures` would have made the CLI's summary
line lie about what went wrong (nothing did), and folding it into the success count would
overstate how many companies were actually checked tonight.

**`session=None` disables `Cadence.WEEKLY` entirely - the same "None means old behavior"
convention `on_progress` already established on this exact function.** Every test that
predates this change, and any future caller that doesn't explicitly opt in, keeps getting
today's behavior: every company fetched every call, no DB touched. This is also what kept
the existing 4 `fetch_all` tests passing with zero changes to their own setup, and it's why
the two real call sites - pipeline.py's CLI and orchestrator.py's `fetch_persist_filter`
node - now explicitly open a session and pass it through; that's where `Cadence.WEEKLY`
actually takes effect. orchestrator.py's node opens that session earlier than before
(around `fetch_all` alone, not just around the later filter-query read) - a small
restructure, not just a parameter addition.

**`--force`** on pipeline.py's CLI fetches every company regardless of cadence, and still
records the fetch afterward - forcing a run resets the weekly clock rather than leaving a
company's `last_fetched_at` stuck on the pre-force timestamp forever, which would make the
*next* scheduled check think it's still not due.

**A failed fetch is never recorded.** `record_fetch` only runs after a company's fetch
actually succeeds - a company whose last attempt raised `BoardNotFoundError` (or anything
else) needs to be retried next run, not silently skipped for a week because it happened to
fail once. Locked in with `test_fetch_all_does_not_record_a_failed_fetch`.

**Cisco and Adobe set to `cadence: weekly` in companies.yaml.** One real consequence worth
knowing before the next nightly run: `company_fetch_state` is empty for every company right
now, including Cisco and Adobe, because the run that fetched them (see above) happened
before this table existed. Their weekly clock starts on whichever run is the first to pass
a session into `fetch_all` - the next nightly or forced run - not backdated to the run
that already happened. That first post-feature run will fetch them (never-fetched-yet
always counts as due), and only the run after that, 7+ days later, is where the actual
skip is first observed.

## SQLite -> Postgres: what actually broke, measured against the real archive first

Deploying meant moving off a local SQLite file to hosted Postgres (Neon). The blast radius
was surveyed against the real 10,648-row database *before* any code changed, because the
interesting failures here are not the ones a type checker finds.

**The one that mattered most was invisible in the code: `location` was `String(300)` and
three real rows are longer than that** - 404, 479 and 649 characters. SQLite treats
`VARCHAR(n)` as decoration and never enforced it, so this went unnoticed for the whole life
of the project; Postgres rejects the row outright. The three are Zscaler postings that
semicolon-join an entire US-state list into one location string, and four more rows already
sit in the 240-299 range, so this is a moving edge, not a one-off. Changed to `Text`: a
larger `VARCHAR` would only move the cliff, since the data has no natural upper bound.
Every other string column was checked the same way and had real headroom.

**What did NOT break, checked rather than assumed:** there are zero `LIKE`/`ILIKE` queries
in the codebase, so the usual case-sensitivity trap doesn't apply; zero descriptions contain
a NUL byte, which Postgres `TEXT` would reject; the one `GROUP BY` was already
Postgres-legal. Worth recording so nobody "fixes" them later.

### Dialect-specific things, and what replaced them

- **The WAL pragma is deleted, not translated.** `PRAGMA journal_mode=WAL` is a syntax error
  on Postgres, and the reader/writer lock contention it was enabled to solve does not exist
  under MVCC. There was nothing to port.
- **`_ADDED_COLUMNS` keeps its hand-rolled `ALTER TABLE`, but the type is now per-dialect.**
  `DATETIME` does not exist in Postgres (`type "datetime" does not exist`), so each entry
  carries `{"sqlite": ..., "postgresql": ...}` and a missing dialect raises rather than
  silently skipping a column. Alembic was considered and deferred: this mechanism has been
  debugged twice in production and only handles the nullable-add case, which is all it has
  ever needed. The moment a non-nullable column or a type change is required, it can't
  stretch and Alembic becomes the right answer.
- **Pooling differs by caller.** The FastAPI server gets a real `QueuePool`
  (`pool_size=5, max_overflow=2`); a one-shot CLI or GitHub Actions run gets `NullPool`,
  because pooling connections a short-lived process is about to drop is pure overhead. Both
  get `pool_pre_ping=True` and `pool_recycle=300` - Neon sleeps idle connections, and
  without pre-ping a slept connection surfaces as an operational error on the next query
  instead of transparently reconnecting.
- **The connection string is the *pooled* Neon endpoint (`-pooler`), which is PgBouncer in
  transaction mode.** psycopg 3 auto-prepares statements after a few executions, and
  server-side prepared statements can outlive the transaction that created them under
  transaction pooling. `prepare_threshold=None` is set when the hostname contains
  `-pooler.`, so this can't surface later as an intermittent
  `DuplicatePreparedStatement` under load.

### Row order stopped being free

SQLite returns rows in rowid order when no `ORDER BY` is given; Postgres guarantees nothing.
Two places quietly depended on the accident:

- `pipeline.load_jobs_from_db()` feeds `--limit N`, whose entire purpose is a repeatable
  cheap test before spending real LLM quota. Unordered, it would analyze a different
  arbitrary N every run.
- `export_labels.sample_rejected()` calls `rng.sample()` with a seeded `Random(0)`. Sampling
  picks *positions*, so its output depends on pool order as much as on the seed - unordered,
  the same seed would produce a different label sheet each export, undermining the one
  artifact the evaluation rests on.

Both now order explicitly. Neither would have been caught by the test suite, which runs on
in-memory SQLite and would have stayed green while production drifted.

### The migration script, and the bug that only appears after it succeeds

`migrate_to_postgres.py` copies through the SQLAlchemy ORM rather than a `.dump` replay,
because the engines disagree about three types this schema uses and the models already state
the correct answer for each: booleans are `0`/`1` integers in SQLite and a real boolean in
Postgres, datetimes are ISO *text*, and embeddings are `BLOB` vs `BYTEA`. A raw replay would
have to reimplement all three by hand.

**`run_agent_metrics.id` is the trap.** It's an autoincrement integer, and copying rows with
explicit ids does not advance the Postgres sequence behind that column - so the migration
"succeeds", every row count matches, and then the *first new run* dies on a duplicate key.
`_resync_sequences()` runs `setval` past the highest copied id; verified afterwards by
inserting a row and confirming it got id 9, not 1.

**Performance was a real correction mid-flight.** The first implementation used
`Session.merge()` per row, which issues a SELECT *and* an INSERT for every row - about
21,000 network round trips to us-east-2. Measured against the live database at ~1.7 rows/sec,
which is ~100 minutes. Replaced with batched `INSERT ... ON CONFLICT DO UPDATE`
(500 rows per round trip), keeping the same idempotency. The run was killed and restarted
rather than left to finish, since re-running is safe by construction.

**Result, verified on both sides:**

| table | sqlite | postgres |
|---|---:|---:|
| job_postings | 10,648 | 10,648 |
| job_embeddings | 152 | 152 |
| analyst_results | 116 | 116 |
| run_metrics | 4 | 4 |
| run_agent_metrics | 8 | 8 |
| company_fetch_state | 0 | 0 |
| **total** | **10,928** | **10,928** |

Row counts alone prove nothing about the distinctions this project actually cares about, so
the script re-checks them on the Postgres side after copying: 2 rows with
`application_status='applied'` and **0** of them with `applied_at` set (that NULL is real
history - jobs marked applied before the column existed, and filling in a plausible date
would fabricate a record), 3,294 `experience_years_required` NULLs, 8/8 `retries` NULLs, and
a longest location of 649 characters proving the `Text` change took. The SQLite file is
opened read-only and left untouched.

### The LangGraph checkpointer moved too, and why that wasn't optional

`orchestrator.py` checkpoints to its own `data/orchestrator_checkpoints.db`, separate from
the job database. On a laptop that file persists, which is what makes the nightly run's two
documented properties true: same-day idempotency (a second run on a completed thread
executes nothing) and crash recovery (resume at job 22 of 30 without refetching).

**On GitHub Actions the runner's filesystem is destroyed when the job ends**, so a SQLite
checkpointer would start empty on every run and both properties would silently stop holding.
No error, no warning - just an orchestrator whose stated justification (CLAUDE.md names
those two properties as the concrete reason LangGraph is here at all) quietly became false
in the only environment that ships.

The *cost* of that would have been small, worth stating plainly: analyst verdicts are cached
in `analyst_results` keyed on model+prompt+resume+requirements, so a re-run is cache hits
rather than new LLM spend, and the weekly cadence state lives in `company_fetch_state` - both
now on Postgres. A lost checkpoint would have meant re-fetching ~62 nightly companies
(~100 seconds), not re-spending quota. It moved anyway, because a documented architectural
property becoming false in production is a different category of problem from a wasted
minute. `PostgresSaver.setup()` creates four tables of LangGraph's own alongside the six here.

### Freshness now comes from the data, not a file mtime

`GET /api/meta/runtime` gained `last_successful_run` - the `finished_at` of the newest run
with `status='completed'`. A file mtime cannot work once the API and the process that writes
the data are different machines, and on Actions the writer's filesystem is gone by the time
anyone asks. Deliberately excludes runs still at `status='running'`: a crashed run's start
time would overstate how fresh the data is, the same rule the metrics panel already follows
for `retries`. `null` means "no completed run on record", never an epoch date.

Two related fixes to the same endpoint:

- **`db_path` was replaced with `db_backend`.** The old field was a local filesystem path,
  meaningless once deployed - and the obvious "fix" of substituting `DATABASE_URL` would
  have published the database password through an unauthenticated endpoint. There is now a
  test asserting the response body contains neither the password nor the host.
- **`db_size_bytes` is `null` on Postgres, not `0`.** There is no local file to stat. The
  frontend's `Metric` component already renders null as *not measured*, so this needed no UI
  change - but the first implementation got it wrong in an instructive way: it branched on
  the `DATABASE_URL` environment variable rather than the live connection, so with a Neon URL
  in a developer's `.env` a session genuinely connected to SQLite still reported "no file to
  measure". Caught by a test. Config describes intent; the connection is the fact.

### One unrelated bug this surfaced

Running the real API against the real `companies.yaml` returned a **500 from
`GET /api/companies`**. `CompanyStats.token` was typed `str`, but Workday entries have no
token (they use `workday_tenant`/`workday_wd`/`workday_site`). It broke when `token` became
`Optional` for the Workday adapter and went unnoticed because the shared test fixture's
`companies.yaml` only ever contained a Greenhouse company. Nothing to do with Postgres - it
was simply the first time the endpoint had been exercised against a config containing the
companies added over the last few sessions. Fixed, with a regression test that writes a
Workday entry specifically.

## Nightly run on GitHub Actions, and the resume problem

`.github/workflows/nightly.yml` runs the orchestrator at 02:00 IST (cron `30 20 * * *`, since
IST is UTC+5:30 with no DST), plus a `workflow_dispatch` manual trigger. Nothing is written
back to the repo - every artifact of a run already lives in Postgres, which is what made this
workflow possible at all (see the checkpointer section of the migration entry above).

**The sentence-transformers model cache was asked for and deliberately not added, because the
premise turned out to be false.** Verified rather than assumed: importing `orchestrator` loads
no heavy modules at all (`torch`, `transformers`, `sentence_transformers` are all absent from
`sys.modules`), because `ranking.py` defers its `SentenceTransformer` import and `rank_jobs` is
reachable only from `pipeline.print_ranked`, i.e. the `--ranked` CLI flag. The nightly path
calls `print_analyst_stage1`/`stage2`, neither of which touches ranking. **The model is never
downloaded during a nightly run**, so a cache step for it would restore and save an empty
directory forever. The pip cache *is* worth having, and for exactly the reason the model cache
isn't: `sentence-transformers` still pulls `torch` (524 MB installed) at install time even
though the run never imports it. If install time ever becomes the bottleneck, the real fix is
splitting the ML dependencies out of `requirements.txt`, not caching a model nothing fetches.

**Resume: injected from a base64 secret, not committed.** Options considered:

1. **`RESUME_TEXT_BASE64` Actions secret, decoded to `data/resume.txt` at run start.** Chosen.
   Zero code change, the resume never enters the repo or its history, and the file dies with
   the runner. base64 rather than a raw multi-line secret so newlines and non-ASCII survive
   byte-for-byte - verified by round-tripping the real file (4,552 chars, well under the 48 KB
   secret limit, `cmp` identical).
2. **Private repo with the resume committed.** Rejected: it puts the resume in git history
   permanently, which is a worse property than the one it solves, and it moves the run onto a
   billed minutes quota.
3. **Store the resume in Postgres.** Architecturally the most consistent with "all state lives
   in Postgres now", and it would also fix a latent problem - the deployed API's Resume page
   writes to a local file that will not persist on ephemeral infrastructure. Deferred, not
   dismissed: it needs a table and a change to `prepare_resume_text`, and it is the right move
   *when the API itself is deployed*, which it isn't yet.
4. **Private gist / object storage fetched at runtime.** Rejected: another moving part and
   another secret, with no advantage over option 1 at this size.

**Preferences were a near-silent trap worth recording.** `data/preferences.json` is gitignored
too, and `config.load_preferences` falls back to `DEFAULT_PREFERENCES` for a missing file
without failing. Checked the real file: it is currently byte-for-byte identical to the
built-in defaults across all four lists, so the runner filters identically today. That stops
being true the first time the Roles page tunes a list, and the divergence would be invisible -
the nightly run would quietly filter differently from local. The workflow takes an optional
`PREFERENCES_JSON` secret and logs which path it took, so the answer is always in the run log
rather than assumed.

**Timeouts, three layers, deliberately not one.** The platform ceiling is 6 hours. The job sets
`timeout-minutes: 90`, which for a run measured at well under an hour means "something is badly
wrong" and fails fast rather than burning six hours of quota first. Below that, a soft
`::warning::` fires at 60 minutes: a run that has grown that long is a trend worth seeing but
not itself a failure, so it warns without failing.

**Concurrency queues rather than cancels.** `cancel-in-progress: false` because a half-finished
run holds a resumable Postgres checkpoint, and cancelling mid-flight would throw away ATS
requests already spent.

**Cost, from measured inputs.** Per-run time is built from real numbers (fetch stage 101.6s for
62 companies, filter 3.3s, stage1+stage2 16s warm, 1.3 s/request measured against a live
Workday tenant, 147 requests for the three small tenants, 1,912 for Cisco+Adobe):

| | duration |
|---|---:|
| typical night, warm caches | ~8 min |
| busy night, new postings | ~10 min |
| weekly night, +Cisco/Adobe | ~52 min |

That is roughly **494 billed minutes/month** (26 ordinary nights + 4 weekly ones, rounded up
per job as GitHub bills). Free on a public repo; about **25% of the 2,000-minute Free-plan
allowance** on a private one. Runner overhead (checkout, Python setup, cached pip install) is
the one estimated input at ~2.5 min - it cannot be measured from here, so the workflow prints
its own elapsed time and the first real run replaces the estimate with a fact.

**Secrets are passed through `env:`, never interpolated into shell.** An earlier draft wrote
`${{ secrets.X }}` directly inside a `run:` script; rewritten to bind them as step `env` first,
which is the standard mitigation for script injection through workflow expressions.

## The first real nightly run died at 44 minutes: a session held across the fetch

`psycopg.OperationalError: terminating connection due to idle-in-transaction timeout`, on a
SELECT against `company_fetch_state` for Cisco, inside `fetch_persist_filter`. Two distinct
faults, one causing the other.

**Fault 1: `fetch_all` borrowed a live `Session` and held it across 40+ minutes of HTTP.**
The caller opened a session, `_is_due` issued the first `SELECT` (opening a transaction), and
that transaction then sat idle while the loop fetched every company - including Cisco and
Adobe on the week they are due. Neon terminates idle-in-transaction connections. SQLite has
no such timeout, which is why local runs and the whole test suite were green: the same shape
of engine-specific gap as the `VARCHAR(300)` location finding, and found the same way - only
by running against the real thing.

**Fault 2: LangGraph retried the node, doubling the load.** `RetryPolicy(max_attempts=3)` was
on `fetch_persist_filter`. LangGraph's `default_retry_on` excludes `ValueError`, `OSError`,
`RuntimeError` and friends and returns `True` for everything else; `psycopg.OperationalError`
inherits `DatabaseError -> Error -> Exception`, none of which are excluded - verified
directly rather than assumed. So the timeout was retried into a **second complete fetch of
all 67 companies**, which is what put `Fetching jobs for 67 companies...` in the log twice. It
doubled the runtime and, worse, doubled the request load on other companies' ATS APIs.

### The fix: `fetch_all` takes an Engine, never a Session

Raising a timeout was rejected - it treats the symptom, and the transaction would still be
open for 40 minutes. The function now owns three explicit phases:

1. one short session reads **every** company's last-fetch time in a single query
   (`db.get_last_fetched_map`), then closes;
2. the loop runs with **no** database connection held at all;
3. one short session records the successful fetches.

Taking an `Engine` rather than a `Session` is the part that makes it stay fixed: the
long-open-transaction bug is no longer expressible in this function. For the same reason
`_is_due` now takes a plain `dict[str, datetime]` instead of a `Session` - a function that
cannot reach the database cannot reintroduce a per-company query inside the loop. There is a
test asserting that signature, because the signature *is* the guard.

`engine=None` still disables cadence entirely, preserving the "None means old behavior"
convention `on_progress` established on the same function.

### Retry policy narrowed for the fetch node only

`_retry_fetch_on` never retries a `SQLAlchemyError` or anything from `psycopg`. A database
failure is either configuration (a retry cannot fix it) or connection-level (`pool_pre_ping`
already handles that far more cheaply, without redoing the network work). Retrying is
reserved for a genuinely unexpected in-process error during the cheap persist/filter phase -
and the node already survives per-company adapter failures internally by collecting them into
`failures` rather than raising, so wholesale retry was never what protected against a flaky
board. `stage1`/`stage2` keep the default policy: they are cheap to redo and already guard
LLM quota internally.

LangGraph's default predicate is read off `RetryPolicy().retry_on` rather than imported from
`langgraph._internal._retry` - that module path has already moved between versions, and the
default of a public class is the stable way to reach it.

### Verified three ways, not just by unit test

1. **The regression test is provably not vacuous.** Simulating the old per-company read shows
   the event order `db_read, db_read, fetch:Cisco, db_read, fetch:Adobe`, and the assertion
   `max(reads) < min(fetches)` fails on it. The test asserts *ordering* rather than
   connection-pool internals, because ordering is the property that actually matters and it
   reads identically on both engines.
2. **Against the real Neon database.** Running `fetch_all` with a deliberately slow fetcher
   and sampling `pg_stat_activity` from a separate connection *during* the fetch showed **no
   application connection at all** - not merely no open transaction.
3. **With a control, so that result means something.** Deliberately reproducing the old shape
   (hold a session, issue the read, wait) showed `idle in transaction` on 3 of 3 samples. The
   observer detects the bug when it is present, so its silence when the fix is in place is
   real evidence rather than a broken probe.

## The second failure: PostgresSaver held one connection for the whole run

With `fetch_all` fixed, the next nightly run got further and died at 40 minutes in
`PostgresSaver.put_writes` with `psycopg.OperationalError: SSL connection has been closed
unexpectedly`. Same underlying cause as the first failure - a connection idle across the
long fetch - but in a component this codebase does not own.

**How PostgresSaver actually manages its connection, read from the installed source rather
than assumed.** `PostgresSaver.__init__` accepts `Conn = Connection | ConnectionPool`, and
every operation goes through `_internal.get_connection`, which does:

- a plain `Connection`: `yield conn` - the *same* connection, for the object's whole life;
- a `ConnectionPool`: `with conn.connection() as conn` - borrowed per operation, returned
  immediately after.

`from_conn_string` (what this project was using) takes the first path: it opens one
`Connection.connect(...)` and holds it for the entire `run_nightly`, including the 40+
minutes during which the checkpointer does nothing at all. Passing a pool instead turns "one
connection held for an hour" into "a connection checked out for the milliseconds each
checkpoint write takes".

**A pool alone is not sufficient - that was measured, not reasoned about.** Killing the
backend server-side with `pg_terminate_backend` to stand in for Neon's idle timeout, against
the real database:

| configuration | outcome |
|---|---|
| single `Connection` (`from_conn_string`) | **failed** - `SSL connection has been closed unexpectedly`, the exact production error |
| `ConnectionPool`, default settings | **failed** - same error |
| `ConnectionPool` with `check=ConnectionPool.check_connection` | **survived** - dead connection discarded, fresh one issued |

So the answer to "can it survive a 40-minute idle gap" is **yes, but only with the check**.
psycopg_pool's `check` callback pre-pings each connection as it is handed out; without it the
pool cheerfully hands back the connection it already had, which is dead. The pool's own
`max_idle` (default 600s) does not save this either, because it only shrinks the pool down to
`min_size` - the last connection stays and goes stale.

**A detour worth recording, because the first attempt at this experiment proved nothing.**
Run against the `-pooler` endpoint, `pg_terminate_backend` reported terminating **0**
backends and both configurations "passed". That is not a fix, it is a broken probe: Neon's
pooled endpoint is PgBouncer in transaction mode, which releases the *server* backend between
transactions, so there is no idle backend to kill and `pg_stat_activity` shows nothing to
target. What Neon dropped after 40 minutes was the client-to-pooler TLS connection, not a
server backend. Re-running against the **direct** endpoint (same host without `-pooler`) gave
each client a real backend and produced the table above. The recovery fix was then verified
end-to-end through the real `-pooler` `DATABASE_URL` by force-closing the pool's connection
mid-use and confirming the next write transparently recovered.

**The settings, and why each one:**

- `check=ConnectionPool.check_connection` - the whole point, per the table above.
- `kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0}` - mirrors
  what `from_conn_string` sets on its own connection. `row_factory=dict_row` is required, not
  cosmetic: the saver's SQL reads rows as dicts and fails confusingly deep inside the library
  without it.
- `min_size=1` rather than psycopg_pool's default of 4 - one sequential writer, and idle
  connections are not free on a Neon free tier.

All of them are hoisted to module constants so a test can assert them without opening a real
connection, which is also what stops `check` from being quietly dropped later as a
mysterious-looking keyword argument.

**Alternatives, had the answer been no.** Worth writing down since the question was asked
directly: (1) a keepalive - periodically touching the checkpointer during the fetch - which
is a background thread and a timer to maintain, and papers over idleness rather than removing
it; (2) opening the checkpointer per graph *node* instead of per run, which LangGraph's API
does not invite and would fragment the checkpoint lifecycle; (3) returning to SQLite
checkpoints and accepting that on Actions the file dies with the runner, so same-day
idempotency and crash-resume silently stop holding - the option previously considered and
rejected precisely because a documented property becoming false in production is worse than
the cost it saves. None were needed: the pool plus check works, and it is the shape the
library already supports.

## Stage 2 was running a model two generations older than stage 1

Found by pairing the two stages' cached verdicts per job - not by reading the config, where
`gemini-3.5-flash-lite` (stage 1) and `gemini-2.5-flash` (stage 2) sit two lines apart and
look unremarkable. Stage 2 is described throughout as "the stronger model"; it was two
generations behind. That makes "stage 2 disagrees with stage 1" useless as a quality signal,
because the disagreement is as likely to be the older model being wrong.

**What the key can actually reach, from a live `models.list` rather than memory:** 50 models,
37 supporting `generateContent`. Non-preview text models: `gemini-3.7-flash`, `3.6-flash`,
`3.5-flash`, `3.5-flash-lite`, `2.5-flash`, `2.5-flash-lite`, `2.5-pro`. Every 3.x *Pro* is
preview-only, so the newest non-preview model available is **`gemini-3.7-flash`** - which is
what stage 2 now uses.

**Is that the same family as stage 1, making the cascade pointless?** No, and the difference
is observable rather than nominal: `gemini-3.7-flash` is a newer generation *and* a higher
tier than `gemini-3.5-flash-lite`, and on a real job it reported `thoughtsTokenCount: 192`
where flash-lite has consistently reported **0** (recorded in the earlier model-comparison
entry above). The two stages are genuinely doing different amounts of work, so the cascade
keeps a defensible basis. Had the answer been "same family", the honest move would have been
to drop stage 2 rather than keep paying 20 daily calls for a second opinion from the same
judge.

**Quota is unchanged, which is what makes the swap free.** Measured from a real 429 body
rather than assumed: `quotaId` `GenerateRequestsPerDayPerProjectPerModel-FreeTier`,
`quotaValue` **20** - the same daily ceiling `gemini-2.5-flash` had, so `STAGE2_TOP_N = 15`
remains correct and nothing else in the budget reasoning changes.

**Retries spend quota, and a 503 burst proved it expensively.** The first re-run stopped at
9 of 15 jobs when `gemini-3.7-flash` returned 503 "experiencing high demand"; the backoff
retried 5 times, and every attempt counted against the 20/day budget. The second run then hit
429 immediately. Roughly a quarter of one day's stage-2 budget went to retrying a single
temporarily-unavailable model. `llm.py` already counts attempts rather than successes for
exactly this reason; this is the first time it cost a visible amount.

**Result, stated as a like-for-like comparison.** The headline "58% -> 83%" is not one:
58% was over 15 jobs, 83% is over the 9 that completed before quota ran out. Restricting the
old model to the *same 9 jobs*:

| stage-2 model | pairwise ordering agreement with stage 1 |
|---|---|
| `gemini-2.5-flash` (old) | 17/23 = **74%** |
| `gemini-3.7-flash` (new) | 20/24 = **83%** |

A real improvement, and a much smaller one than the raw figures suggest.

**On the labelled jobs, the sample is too small to conclude anything yet.** Only 3 of the 12
human-labelled "good" jobs were re-scored before quota ran out: stage 1 means 71.7, old model
55.0, new model 62.0. The new model moves back toward the human judgment on the case that
started this (ElevenLabs, labelled *good*: stage 1 85, old model 50, new model 78, and its
stated reason is a requirement the JD actually contains - "experience working with customers"
- rather than the old model's complaint that the resume "does not literally list 'APIs
integration'"). One good example is not evidence. n=3 is not either, and this entry is not
claiming otherwise.

**Still open:** 6 of the top 15 have no stage-2 verdict from the new model, and 9 of the 12
labelled jobs have not been re-scored. Both need tomorrow's quota. Until then the ranking on
the dashboard mixes stage-2 verdicts from two different models, which is worth knowing before
trusting the order.

## Big Four India: only PwC is on a supported ATS, and it is expensive

Checked because Deloitte India produced real interview responses, so it is a genuine target
rather than a speculative one. All four were probed against every supported ATS before any
conclusion was drawn.

**Workday tenant discovery used the failure-shape discriminator** recorded in the Workday
adapter entry above: a nonexistent tenant returns HTTP 422, a real tenant with a wrong site
returns 404. Probing `{tenant}.wd{N}.myworkdayjobs.com` across 12 `wdN` values for 11 name
variants (deloitte, deloitteus, deloittein, ey, eyglobal, ernstandyoung, pwc, pwcus, kpmg,
kpmgus, kpmgllp) found exactly one: **`pwc` on `wd3`**. Greenhouse, Lever and Ashby board
APIs returned nothing for any of the 11 tokens.

**What the other three actually use** - read off their real careers pages, not assumed:

| firm | ATS | evidence |
|---|---|---|
| Deloitte India | **SAP SuccessFactors** | `southasiacareers.deloitte.com` -> `career44.sapsf.com` |
| EY | **SAP SuccessFactors** | `careers.ey.com` -> `career5.successfactors.eu` |
| KPMG India | **Oracle Recruiting Cloud** | `ejgk.fa.em2.oraclecloud.com/hcmUI/CandidateExperience` |

None is supported, and none is worth an adapter on this evidence alone - the same standard
the Workday adapter had to meet (real tenants first, adapter second). Worth noting that
Deloitte India runs a *separate* South Asia portal from the global `apply.deloitte.com`,
which is Avature - so a global-page fingerprint would have given the wrong answer for the
entity actually being targeted.

**PwC, measured properly.** Three real sites on `pwc/wd3`, robots.txt allows the CXS path:

| site | total board | India postings | est. requests |
|---|---:|---:|---:|
| `Global_Experienced_Careers` | 4,287 | **1,002** | ~4,502 |
| `Global_Campus_Careers` | 1,521 | 2 | ~1,598 |
| `US_Experienced_Careers` | 481 | 0 | ~506 |

The campus site is the one that *sounds* right for fresher hiring and is almost entirely
useless here: 2 India postings out of 1,521. India volume is all on the experienced site.

**A third location convention, and why the India count nearly came out as zero.** PwC's
location facet uses bare site names with no country - "Bengaluru Millenia", "Mumbai Shivaji
Park", "Gurugram 10 C", "Kolkata DN 57". An `\bindia\b` match against it returns nothing,
which would have wrongly concluded PwC has no India presence. Counting required matching
Indian *city* names, reusing the project's own `india_location_keywords`. That is now three
distinct per-tenant location formats seen on Workday ("City, Country", "Country - City", and
bare site names), reinforcing that the format is tenant-configured and must never be assumed.

**A real filters.py bug, found through PwC's title convention.** PwC India titles are
underscore-delimited: `IN_Senior Associate_ Python Full Stack Developer_GCC_Advisory_Gurgaon`.
`_` is a **word character** in regex, so `\bsenior\b` cannot match inside `IN_Senior` - the
seniority rule silently fails to fire. Verified directly: that exact title returns `None`
(survives), while the same words with spaces instead of underscores return `seniority`.
Normalising delimiters before filtering moves PwC's India rejections from 308 to 705 on the
seniority rule. This is not a PwC quirk to special-case; it is a latent hole in a rule the
whole pipeline depends on, and any source using underscores, en-dashes or em-dashes as title
delimiters bypasses it the same way. **Not fixed in this pass** - it was found during an ATS
survey and changing filter behaviour would silently re-rank the existing archive, which
deserves its own change with a before/after survivor count.

**Yield, and the verdict.** Of 1,000 India postings examined (via 52 facet-filtered requests,
not a full fetch), **21 unique roles survive the rule filters** once delimiters are
normalised and duplicates collapsed - mostly "Associate" and "Sr Associate" developer roles
in Bengaluru, Gurugram, Kolkata and Noida. Cost to obtain them nightly would be ~4,502
requests, because the adapter fetches the whole board and applies no server-side filter.

That is **~214 requests per surviving role**, against 32 for Cisco, 73 for Harris and 93 for
Adobe - by a wide margin the worst ratio of any tenant surveyed. At the measured ~1.3
s/request it is also roughly **98 minutes**, which on its own exceeds the 90-minute
`timeout-minutes` on the nightly workflow. PwC is therefore not added: it is a viable
on-demand target (`python -m adapters.workday "PwC India" pwc wd3
Global_Experienced_Careers`), not a nightly one, unless the adapter gains the ability to
apply a location facet server-side - which the facet probe above shows the API supports and
would cut the cost by roughly 4x.

## Title delimiter normalisation: a real bug with, as it turned out, zero current impact

Fixing the hole found during the Big Four survey. The impact was measured against the real
archive **before** the change, because "fix a filter rule" and "silently re-rank the whole
archive" are the same action here.

**Only underscore actually breaks `\b`** - checked against 17 candidate characters, not
assumed. `\b` sits between a word char and a non-word char, and Python's `\w` is
`[a-zA-Z0-9_]` plus Unicode letters/digits, so en-dash, em-dash, non-breaking hyphen, nbsp,
middot, bullet, minus and the rest are all already safe. The original framing (that dashes
break `\b` too) was wrong.

**But there is a second, unrelated failure mode that dashes DO cause**, and it is why they
are normalised anyway: any delimiter *inside* a multi-word keyword breaks the phrase match.
`"Vice-President Engineering"` does not match the `vice president` keyword - with a plain
ASCII hyphen, nothing to do with `\b`. Two bugs, one fix.

**Impact on the existing archive: exactly zero, verified both directions.** Recomputing all
12,158 stored postings under current rules and under normalised titles:

| | count |
|---|---:|
| survivors before | 121 |
| survivors after | 121 |
| currently surviving that would become rejected | **0** |
| currently rejected that would newly survive | **0** |

A zero result is as likely to mean "broken analysis" as "no impact", so it was checked
directly: only **9** titles in the whole archive contain an underscore (all Lever, all
already rejected on other rules - "Manager", "Team Leader", "Legal"), while **5,422** contain
a dash, so the normalisation is genuinely exercised on thousands of titles and simply changes
no verdict. **So no, this has not been leaking senior roles from Greenhouse, Lever or Ashby.**
The bug was real but latent - it needed a source that writes `IN_Senior`, and the only one
found is PwC, which is not a configured source.

**Scope, deliberately narrow.** Normalisation is applied only to the seniority and
non-engineering keyword checks. The allowlist and location rules keep the raw title, so this
cannot widen what counts as a matching role or a matching city - a test asserts that
`"Data-Entry Clerk"` is still `not_allowlisted` rather than becoming a "Data Entry" match.

Post-fix survivor count by source, unchanged at 121 total: workday 69, greenhouse 20,
ashby 19, lever 13.

## PwC declined as a source, and what a server-side location facet would change

Not added: ~4,502 requests for 21 surviving roles is ~214 requests per survivor against 32
for Cisco, and at the measured ~1.3 s/request the ~98 minutes exceeds the nightly workflow's
own 90-minute `timeout-minutes`.

**The facet option, costed rather than hand-waved.** The Workday CXS API accepts
`appliedFacets` server-side - already proven here, since the 1,002 India postings were
counted with exactly that mechanism in 52 requests instead of 4,502. Adding a
`workday_facets` field to `CompanyConfig` and passing it through `_fetch_listing_page` would
bring PwC to roughly **1,002 detail fetches + ~51 listing requests = ~1,053 requests, about
23 minutes** - inside the workflow timeout, and ~50 requests per survivor, which is between
Cisco (32) and Harris (73) rather than off the scale.

Deliberately NOT built yet, for two reasons worth stating. First, facet IDs are opaque
per-tenant hashes (`e57e6863118d01f411ec8989342b58c9` for one PwC "Ahmedabad" value, and
there are 29 India location values on that one board) - they would have to be discovered and
pinned in `companies.yaml`, and they can change when a tenant reconfigures its locations,
turning a silent facet mismatch into a silently smaller fetch. That is the same
fails-quietly shape as the capped-`total` bug. Second, it only pays for itself on tenants
where the India subset is a small fraction of a large board; for Cisco and Adobe, already
configured, it would save little. It is a real option with a real number attached, not a
default.

## Experience stated in the title, and unscored jobs that were buried

Two problems visible in the same 121-survivor list, fixed together.

### Title-stated experience is now a filter rule (the description-based one still is not)

Dozens of Cisco roles requiring 8-12 years were surviving, because the seniority rules match
words and these titles state numbers: "8 to 11 Years", "9 - 12 yrs", "4 to 8 yrs",
"(5-7 years)", "| 12+ Yrs |".

**The existing description parser could not be reused as-is, and finding out why mattered.**
Its range patterns require an `exp`/`experience` anchor within a short window - deliberately,
because a description is full of stray numbers (team sizes, founding years, revenue). Against
real titles it returned `None` for "8 to 11 Years", "9 - 12 yrs", "4 to 8 yrs" and
"(5-7 years)", while correctly reading "Exp: 4-8 Yrs". So `parse_title_experience_years`
reuses the same idioms without the anchor, on the grounds that a title is short and curated:
a number followed by "years" in one can only be the requirement.

**This is not a revival of the removed `MAX_EXPERIENCE_YEARS` cutoff.** That rule inferred a
judgment from buried prose and, at 2 years, rejected roles the hand-labelled set accepted -
costing roughly half the good matches. This one fires only on a figure the employer put in
the title, which is an explicit seniority marker. `MAX_TITLE_EXPERIENCE_YEARS = 7` is set to
the highest requirement the labels ever accepted, so nothing the labels endorsed can be cut.
Rejections get their own reason, `experience_in_title`, rather than being folded into
`seniority`, so the new rule's impact stays visible in the per-rule breakdown.

**Impact, measured and previewed before applying**, as the change was requested:

| threshold | rejected | remaining | labelled-good cut |
|---|---:|---:|---:|
| > 5 | 39 | 82 | 0 |
| **> 7 (chosen)** | **34** | **87** | **0** |
| > 8 | 16 | 105 | 0 |
| > 10 | 9 | 112 | 0 |

43 of the 121 survivors stated a figure in the title; all were Cisco except two PhonePe, and
none was labelled good. Of the top 12 by fit score exactly one is cut - Cisco "Data
Engineering Application Developer", fit 50, title "7 to 10 years". Archive-wide the rule
rejects 86 postings and survivors fall 121 -> 87, entirely from Workday (69 -> 35).

**Two implementation bugs worth recording, both caught by testing rather than review.**
First, the range regex was applied to the *delimiter-normalised* title, and normalisation
turns "(5-7 years)" into "(5 7 years)" - destroying exactly the hyphen the range pattern
needs. The two normalisations serve opposite purposes and must not be chained; the experience
parser reads the raw title, and a test asserts that. Second, the patterns were written into
the file through a non-raw Python string, so the trailing `\b` was encoded as a literal
backspace byte (`\x08`) and every pattern silently failed to match. The symptom was every
title parsing to `None`, which looks exactly like "no titles state experience".

### Unscored jobs that meet a stated requirement now lead the list

The strongest-looking job in the list sat dead last: Cisco "Software Engineer (Evergreen)",
0 years required, resume meets it, marked *could not evaluate*.

"Unscored" means the Analyst found no concrete technical requirements to compare the resume
against - a statement about the **skills** comparison alone. The experience judgment is
separate and was still present. Appending such a job below all 83 scored ones threw away real
evidence, so `partition_unscored_by_experience` promotes the ones with a stated requirement
the resume meets to the head of the list.

**The existing rule that unscored jobs never receive a fabricated `fit_score` is untouched.**
They still show *could not evaluate*; only their position changes. Ordering here says "worth
your attention", not "scored highest". A job with `years_required is None` is never promoted:
None means "not stated", never zero, and reading it as a met requirement would promote on
absent evidence. `include_unscored=false` still drops them all - they are unscored jobs.

**A correction to the preview given before implementing.** That preview said this would
affect 2 jobs; it affects **1**. The second, Databricks "Full Stack Developer (AI Agents)",
showed 3.0 years in the preview because that number came from `JobPostingRow.
experience_years_required` (the description parser), whereas promotion uses the Analyst's own
paired `(years_required, resume_meets_it)` - and the Analyst recorded no figure for it. Using
one source's years with the other source's "meets" would be incoherent, so the Analyst's pair
is the right input, but the preview should have read the field the rule actually uses.

Verified against the live API: `/api/jobs` returns 87 jobs with Cisco Evergreen at #1
(unscored, 0.0 years, meets), ElevenLabs 78 at #2, GoHighLevel 78 at #3.

## Two "it works in the API" bugs that were both really frontend bugs

Reported after a restart: the promoted Cisco job was still at the bottom, and the two
highest-scoring jobs had vanished. `/api/jobs` was correct in both cases - 87 jobs, Cisco
Evergreen first, ElevenLabs and GoHighLevel present at 78 each. The lesson is the same one
twice: **verifying an API response is not verifying what the user sees.**

### The promotion was undone by the frontend re-partitioning the list

`api/routers/jobs.py` returns promoted-unscored first, then scored, then the rest. But
`JobsPage.tsx` threw that ordering away:

```
const scored = visible.filter((j) => !j.is_unscored)
const unscored = visible.filter((j) => j.is_unscored)
```

and rendered `unscored` in a "Could not evaluate" section pinned to the bottom. Any unscored
job landed there regardless of its position in the response, so the backend change was
invisible by construction.

**Fixed by having the backend state the fact, not by duplicating the rule in the frontend.**
`JobSummary` gained `is_promoted_unscored`, set from the same
`partition_unscored_by_experience` call that does the ordering. The frontend groups on the
flag. Re-deriving the predicate in TypeScript would have worked today and drifted the first
time the rule changed - the same reasoning that makes the API types generated rather than
hand-written.

### The two "vanished" jobs were filtered out by their own status - and nothing said so

Both are still stored, both still pass every filter, and the new title-experience rule does
not touch them (neither title states a figure). They are simply the **only two rows in the
archive with `application_status = 'applied'`** - and `JobsPage` defaults to
`["new", "interviewing"]`:

```
const DEFAULT_STATUSES: ApplicationStatus[] = ["new", "interviewing"]
```

So the top two jobs disappeared because the user had applied to them. That default is
reasonable - a "what should I apply to" list is more useful without the ones already done -
and it has not been changed. What was wrong is that it happened **silently**: 87 jobs became
85 with nothing on screen accounting for the difference. The per-status chips did show
"applied 2", but a count on a chip is not the same as saying the list in front of you is
shorter than the data behind it.

`JobsPage` now renders, whenever a filter is hiding anything: *"2 jobs hidden by the status
filter — 2 applied. Click a status above to include it."* Same principle the metrics panel
and the evaluation page already follow: never quietly present a reduced view as the whole
picture.

### Verification, and its limit

Confirmed against the real database by replaying `JobsPage`'s exact grouping over the live
`/api/jobs` payload in Node:

| filter | visible | rendered order |
|---|---|---|
| default `['new','interviewing']` | 85 of 87, notice: *2 hidden — 2 applied* | 1 Cisco (promoted), 2 Adobe 55, 3 Sarvam 52 ... |
| no filter | 87 of 87, no notice | 1 Cisco (promoted), 2 ElevenLabs 78, 3 GoHighLevel 78 ... |

The production build passes and TypeScript is clean. **Not verified in a real browser** -
Playwright is not installed in this environment and pulling it plus browser binaries was not
worth it for a change this size. Replaying the component's grouping over the real payload
covers the logic and the data; it does not cover rendering. Stated rather than glossed,
because "the API returns the right thing" is precisely the claim that was already wrong twice
here.
