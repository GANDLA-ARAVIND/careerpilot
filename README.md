# CareerPilot — Multi-agent AI job discovery pipeline

Three agents (Scout finds company job boards, Analyst scores postings against my resume,
Coach identifies recurring skill gaps), LangGraph orchestration, RAG over the job archive,
and a hand-labeled evaluation set.

One user: a fresher software engineer in Hyderabad looking for entry-level roles. It fetches
postings from ATS APIs nightly, filters them down with cheap rules, has an LLM score what
survives, and presents the result in a dashboard. **It never applies to anything** — that's
the human's job, by design, and no code path exists that could.

It runs entirely on free tiers: the Gemini free-tier API, local embeddings, SQLite, no
hosting. Around 8,300 stored postings currently reduce to ~43 survivors per night, and
every survivor gets analyzed.

---

## What it actually does

```
ATS adapters (Greenhouse, Lever, Ashby)
  → normalize to JobPosting, dedupe on content_hash
  → rule filters: title allowlist, seniority, non-engineering, India location   [no LLM]
  → Analyst stage 1: cheap model scores every survivor                          [LLM]
  → Analyst stage 2: stronger model re-checks the top 15 by stage-1 score        [LLM]
  → SQLite → FastAPI → React dashboard
```

The cheap-filters-first ordering is the load-bearing design decision. LLM calls happen on
~43 jobs a night, not 8,200. That single ordering is what makes the whole thing fit inside
a free tier.

**Three agents**, each owning a decision rather than just wrapping a prompt:

| Agent | Owns | Runs |
|---|---|---|
| **Scout** | Which ATS does this company use, and what's its board token? | On demand |
| **Analyst** | How well does this candidate fit this role, and what's missing? | Nightly |
| **Coach** | What patterns across the archive should change what I learn next? | Weekly |

Scout is the genuinely agentic one: it generates candidate tokens mechanically, tests each
against real board APIs, falls back to an LLM for non-obvious shapes, and loops on the
result. Its stopping signal is a hard fact — a 404 from the real API — never a judgment
call it asked a model to make.

---

## Running it

Requires Python 3.12 and a `GEMINI_API_KEY` in `.env`. Node is only needed for the frontend.

```bash
python -m venv .venv && .venv/Scripts/activate     # or bin/activate
pip install -r requirements.txt

python -m uvicorn api.main:app --port 8000          # terminal 1
cd frontend && npm install && npm run dev           # terminal 2 → localhost:5173
```

The CLI still works independently of the API:

```bash
python orchestrator.py                 # the full nightly run
python pipeline.py --filtered          # what survives the rules, and why the rest didn't
python pipeline.py --analyze --stage 1 # screening pass only
python -m agents.scout "Company Name"  # find a company's ATS + board token
python evaluate_stage1.py              # the ranking evaluation
python -m pytest -q                    # 398 test functions
```

After changing anything in `api/schemas/`, regenerate the frontend's types:

```bash
python gen_openapi.py && cd frontend && npm run gen:api
```

Those types are generated from the API's own OpenAPI document, never hand-written — so a
renamed field breaks the frontend build instead of showing up as `undefined` in a browser
at 8am. That has already caught real bugs: `matched_skills` is optional in the schema
(Pydantic's `default_factory=list` makes it non-required), and the UI was calling `.map()`
on it unguarded.

---

## Findings worth reading

The full record is in [`docs/decisions.md`](docs/decisions.md). These are the ones that
changed how the system works.

### The config error that cost half the good matches

`experience_too_high` was rejecting 19 of the 37 jobs hand-labeled `good` — by far the
largest source of false rejects. The working theory was a parser bug: that
`parse_max_experience_years` was misreading company boilerplate ("we've been building for
10 years") as a candidate requirement.

The theory was wrong. Pulling the matched text and surrounding context for all 19 cases
showed **18 were genuine, correctly-anchored requirements** sitting inside a
`Requirements`/`Qualifications` section — "5+ years", "at least 4 years of experience".
The parser read every one correctly.

The actual bug was `MAX_EXPERIENCE_YEARS = 2`, a hard cutoff encoding a judgment the system
has no way to make. The ground-truth labels showed the real tolerance was far higher:
multiple 7-year asks labeled `good`, a 10-year ask labeled `weak` rather than `no`.

Removing it as a filter criterion:

```
good jobs passing filters:   17 / 37  →  31 / 37
false rejects (good/weak):   25       →  6
false accepts (no):           1       →  14
```

Not a free win — it traded false rejects for false accepts. The parsed figure is still
computed and shown next to every listing, so the person doing the judging has the number
without the system pre-deciding for them. And the rise in false accepts exposed a real,
separate gap: the experience cutoff had been doing accidental double duty as a seniority
filter for numbered-level titles ("SDE-III", "Java Developer IV") that the keyword list
never caught.

The lesson generalizes: the labels didn't find a bug, they found **a policy encoded as if
it were a fact**.

### The truncation experiment

`all-MiniLM-L6-v2` has a hard 256-token window. Measured against real data, the resume was
787 tokens and a typical filtered JD 800+ — so embedding either one raw meant silently
discarding most of it.

Worse, the truncation was landing in the wrong place. Header, contact details and summary
text lead the resume file, so the skills list — the part that actually matters for matching
— was getting cut off mid-list before it was ever reached.

The fix was to extract the skills and projects sections first, then trim *projects* line by
line to fit the budget while guaranteeing skills survives intact. That puts the tradeoff on
project detail instead of on the signal. Every embedding is paired with diagnostics
(extracted vs. fell back to full text, token count, still-truncated-after-extraction)
rather than a silent best guess, because the extractor is pattern-based and can miss a
header it doesn't recognize.

This is also why embeddings are no longer in the live path — see the evaluation result
below.

### The `isRemote` conflation

Ashby's payload exposes both `workplaceType` and `isRemote`. Using both would have been the
obvious move. `isRemote` is deliberately ignored, for two reasons found by looking at real
payloads: it **conflates hybrid into remote**, and it was observed `null` even on postings
where `workplaceType` was set. It adds nothing over `workplaceType` and would actively
corrupt the distinction between remote and hybrid.

This sits inside a broader asymmetry that a filter written against one source will get
wrong on another: Greenhouse gives no structured remote flag at all, only a free-text
location string, so `ONSITE` is never inferred from it — a bare city name doesn't
distinguish onsite from hybrid, and those postings land in `UNKNOWN`. Lever exposes
`workplaceType` directly and does get a confident `ONSITE`. Same field, three different
reliability profiles.

### Two ATS investigations, declined for different reasons

**SmartRecruiters — declined on `robots.txt`, not on technical merit.** The API was
genuinely good, better in places than one already integrated: public and documented, no
auth, `location.remote`/`location.hybrid` as separate booleans (a better remote signal than
Greenhouse gives), well-behaved pagination verified at the actual boundary, exactly one
timestamp field so there's no posted-vs-updated ambiguity. It had real costs — descriptions
need a second request per job, and an invalid company slug returns HTTP 200 with an empty
list rather than a 404, which would permanently break the `BoardNotFoundError` signal Scout's
validation loop depends on. Neither was fatal. `robots.txt` was.

An apparent encoding bug in their JSON was chased down rather than assumed: fetching the raw
wire bytes showed valid UTF-8, and the corruption was in a local curl-to-python pipe.

**A fourth adapter in general — declined on distribution.** 20 companies surveyed by hand
across sectors, spanning **11 different platforms**. The best-represented (Kula, Darwinbox)
covered 2 of 20 each. Extrapolated generously, that's maybe 9–10 companies per adapter — not
the "one adapter unlocks dozens" case that justified Greenhouse, Lever and Ashby.

The 30% that couldn't be identified at all is itself the finding: those careers pages are
client-rendered SPAs that ship an empty shell and populate jobs with JavaScript. A scraping
fallback would need to drive a real browser, not fetch and parse — categorically heavier and
more fragile than anything the three existing adapters require.

One concrete thing fell out of that survey: Razorpay wasn't on a missing platform at all. It
was already on Greenhouse under `razorpaysoftwareprivatelimited` — its full registered legal
name rather than its brand name. Scout now generates Indian legal-entity suffixes
mechanically, and re-running found it on the first try.

### The evaluation result, including that it's currently marginal

122 jobs hand-labeled good / weak / no. Three rankings compared on the subset with a cached
stage-1 score:

```
Metric        Embedding   Stage-1   Random (expected)
MRR (good)        0.126     0.132               0.121
Recall@10         0.310     0.310               0.294
Recall@20        0.655     0.621               0.588
```

**Stage-1 MRR is 0.132 against a random expectation of 0.121, on n = 34.** That margin —
0.011 — is not a result worth claiming as evidence the ranking works. Embedding cosine
similarity is at chance, consistent with the truncation and topical-vs-skill-matching
failure modes above; that's why it was pulled from the live path and kept only as the
baseline this comparison needs.

Two caveats that belong beside the numbers, not beneath them:

- Only 34 of 122 labeled jobs have a stage-1 score, because stage 1 only ever ran over
  rule-filter survivors. These numbers describe the ordering of jobs both rankers actually
  saw, not the full labeled pool.
- The `good` count in that overlap is small, so MRR and recall@k are correspondingly noisy.

The dashboard's Evaluation page ships this as-is, headed "The LLM ranking is only marginally
above chance." An earlier measurement on a larger overlap looked better (recall@10 of 0.258
vs. 0.192 random); it moved when the label set and filters changed. Both are reported. The
point of building the harness was to find out, and this is what it says.

---

## Design commitments

A few rules the code holds to, visible throughout:

- **Null is not zero.** `retries` renders as "not measured" because LangGraph retries nodes
  silently and nothing observes them. `experience_years_required` is `NULL` for "no
  requirement found", never coerced to `0`. A job the Analyst couldn't compare shows `?`
  rather than a fabricated low score.
- **Caches key on what they cache.** An Analyst verdict is keyed on a hash of model +
  prompt + resume + requirements, so changing any of them invalidates exactly the affected
  entries. This was learned the hard way: an earlier embedding cache keyed on a *derived*
  value served stale vectors while the diagnostics printed beside them looked correct.
- **Visible fallbacks.** When the resume extractor misses its headers, the UI says so rather
  than silently sending the whole file. Mangled-PDF tripwires (unusual word length, space
  ratio, big length drop) fire before a save, not three weeks later when every score is off.
- **The system finds and ranks; the human applies.** `applied_at` is recorded when you mark
  something applied and never cleared, because a job applied to and later rejected is still
  a job you applied to.

---

## Status

Built: the three agents, LangGraph orchestration, the FastAPI layer, the React frontend
(User Mode plus a Recruiter Mode that surfaces architecture, evaluation and per-run agent
metrics), per-run metrics, and the evaluation harness. 398 test functions.

The original Streamlit dashboard (`app.py`) is superseded but still works, and is kept until
the React app has had real use in its place.

Deliberately not built: cover letters, resume optimization, interview prep, notifications,
preference learning — and anything that submits an application.
