# CareerPilot — Multi-agent AI job discovery pipeline

Three agents (Scout finds company job boards, Analyst scores postings against my resume, Coach
identifies recurring skill gaps), LangGraph orchestration, RAG over the job archive, and a
hand-labeled evaluation set.

One user: a fresher software engineer in Hyderabad, India, looking for entry-level roles. The
system finds and ranks jobs; **the human applies**. It does not auto-apply, and that is a design
decision, not a missing feature.

## What it does

Runs nightly. Pulls postings from ATS APIs across a configured company list, normalizes and
dedupes them, filters to fresher-level roles matching my target titles, has an LLM score every
survivor with reasoning, re-checks the top 15 with a stronger model, and writes results to
the database. In the morning I open the React dashboard and apply to what's there.

## Success criterion

I open the dashboard at 8am and see 15-25 genuinely relevant fresher roles I hadn't already
found. Everything else is secondary to that.

Current reality: ~8,300 postings stored, ~43 survive the rule filters, all of them analyzed.
The count is in range. Whether the *ordering* is trustworthy is a separate question the
evaluation answers, currently: barely.

## Hard constraints

- **Zero cost.** Free-tier LLM APIs only. Gemini is the only provider implemented; Groq stays
  unbuilt until actually needed, behind the same `LLMClient` interface. Local embeddings via
  sentence-transformers. SQLite locally, Neon Postgres free tier when deployed. No paid
  hosting, no paid vector DB, no paid APIs.
- **Python 3.12**, standard venv, no Docker. Node is only for the frontend dev server and build.
- **Two storage backends, one codebase.** `DATABASE_URL` set means Postgres; unset means
  local SQLite, and local development plus the entire test suite run unchanged on SQLite.
  A test that passes an explicit path or `:memory:` always gets SQLite even when
  `DATABASE_URL` is set, so running pytest can never touch the deployed database.
- **No auto-applying.** The system finds and ranks. I decide and submit. Never build anything
  that submits an application, and never generate resume content claiming skills I don't have.

## Architecture

```
ATS adapters (Greenhouse, Lever, Ashby, Workday)
  -> normalize to JobPosting
  -> dedupe on content_hash
  -> rule filters (no LLM: title, seniority, location)
  -> Analyst stage 1: cheap model scores every survivor
  -> Analyst stage 2: stronger model re-checks the top STAGE2_TOP_N by stage-1 score
  -> SQLite / Postgres -> FastAPI -> React dashboard
```

The cheap-filters-first ordering is load-bearing. LLM calls happen on ~40 jobs per night, not
8000. This is what keeps the project inside free-tier quotas. Do not move LLM calls earlier in
the pipeline.

**Experience is no longer a filter.** `parse_max_experience_years` still runs and its result is
stored and displayed, but it never rejects — a hard cutoff encoded a per-job judgment the system
can't make. See docs/decisions.md; it cost roughly half the good matches before it was found.

**Embeddings are no longer in the live path.** `ranking.py` measured at or below random on the
label set, so it isn't used to pre-select jobs for the LLM. It's kept as the evaluation baseline
and behind `pipeline.py --ranked`. Don't wire it back in as a pre-filter without first showing a
replacement that beats random.

### Discovery via ATS, not scraping

Most companies use an applicant tracking system with a public JSON board endpoint. Detect the
ATS, use its API. Scraping is the fallback for the long tail only, and must respect robots.txt.

### Known cross-source asymmetries

- **`remote_type` is not evenly reliable across sources.** Greenhouse never gives a structured
  remote/hybrid/onsite flag — only a free-text location string — so `ONSITE` is never inferred
  from it (a bare city name doesn't distinguish onsite from hybrid); those postings land in
  `UNKNOWN`. Lever exposes `workplaceType` directly (`remote`/`hybrid`/`onsite`/`unspecified`),
  so Lever postings do get a confident `ONSITE`. A location filter written and tested only
  against one source's data can silently behave differently on the other — check both.

### The three agents

Only these three. Each owns a decision, not just a prompt.

- **Scout** — given a company name, determines which ATS it uses, finds the board token,
  validates that jobs come back, writes the config entry. Loops and retries different
  hypotheses. This is the genuinely agentic one.
- **Analyst** — given a JD and my resume, returns structured judgment: fit score, matched
  skills, missing skills, one honest sentence on fit. Pydantic-validated output.
- **Coach** — runs weekly over the archive via RAG. Answers questions like "across jobs I
  scored below 60 on, which skills appear most often?"

### RAG scope

Retrieval runs over accumulated JDs and my application history. It exists to serve Coach. Not a
chatbot.

### Orchestration

LangGraph for the nightly pipeline. The reason is concrete: retries with backoff on rate limits,
conditional branches (skip LLM stage if fewer than N new jobs), and state that survives a crash
at job 22 of 30 without refetching everything.

Graph state carries `content_hash` strings only — Pydantic models don't survive the checkpointer.
Each run is keyed to a date-based thread (`nightly-YYYY-MM-DD`) and resumed, so a second run on
the same day correctly executes nothing. `POST /api/run` takes an optional `thread_id` to force a
genuinely fresh run when that idempotency is not what you want.

### API and frontend

FastAPI (`api/`) wraps the existing modules — it never reimplements them. Routers call straight
into `filters.py`, `agents/`, `orchestrator.py`, `db.py`, `rag.py`, `config.py`. If an endpoint
needs logic that doesn't exist yet, that logic belongs in the module it's about, not in a router.

The React app (`frontend/`) talks to that API and nothing else. Its TypeScript types are
**generated** from the API's own OpenAPI document (`python gen_openapi.py`, then
`npm run gen:api`) — never hand-written, so a renamed Pydantic field breaks the frontend build
instead of surfacing as `undefined` in a browser.

Progress during a run reaches the UI over SSE (`GET /api/run/stream`). The orchestrator's nodes
emit structured `ProgressEvent`s through an optional callback; `print()` remains the CLI's only
output. A subscriber that connects mid-run gets the full replay buffer first.

## What's built

Streamlit dashboard (`app.py`), FastAPI layer (`api/`), React frontend (`frontend/`), all three
agents, LangGraph orchestration, per-run metrics, and the evaluation harness.

`app.py` is superseded by the React app but still works. Delete it once the React app has been
used for a while in its place — not before.

## Explicitly out of scope for the MVP

Cover letter generation, resume optimization, interview prep, company intelligence summaries,
notifications, preference learning. Do not build these. If I ask for one, remind me it's v2.

Also declined, with reasons recorded in docs/decisions.md — don't re-litigate without new
evidence:

- **A fourth ATS adapter *for the Indian-startup long tail*.** 20 companies surveyed across 11
  platforms; the best-represented covered 2 of 20. That conclusion still stands for Kula,
  Darwinbox and the rest. Workday was built later on different evidence — 9 verified tenants
  with real India engineering postings — and is not a counterexample to it. Anything else needs
  the same standard: real tenants first, adapter second.
- **A SmartRecruiters adapter specifically.** The API was good — better than one already
  integrated. Declined on `robots.txt`, not on technical merit.
- **Scraping the long tail.** 30% of surveyed careers pages are client-rendered SPAs, so this
  would mean driving a real browser, not fetch-and-parse.

## Repo layout

```
adapters/        base.py, greenhouse.py, lever.py, ashby.py, workday.py
agents/          scout.py, analyst.py, coach.py
api/             FastAPI layer - wraps the modules below, never reimplements them
  main.py        app, CORS, lifespan (registers the SSE event loop)
  deps.py        engine singleton + get_session (the seam tests override)
  routers/       jobs, run, resume, preferences, companies, coach, meta
  schemas/       Pydantic request/response models (separate from models.py)
  services/      run_manager (SSE fan-out), metrics, evaluation, runtime, dashboard shim
frontend/        Vite + React + TS + Tailwind + shadcn/ui
  src/api/       client.ts (hand-written) + schema.d.ts (GENERATED - do not edit)
  src/lib/       mode (User/Recruiter), useRunStream (SSE), runTimeline, format, useApi
  src/pages/     Home, Jobs, MissionControl, Resume, Applications, Coach, Settings,
                 Architecture, Evaluation, AgentMetrics
models.py        Pydantic domain schemas
db.py            SQLAlchemy over SQLite or Postgres (DATABASE_URL decides; WAL is
                 SQLite-only, pooling + pool_pre_ping are Postgres-only)
filters.py       rule-based filtering
ranking.py       local embeddings - evaluation baseline only, not in the live path
extraction.py    JD/resume section extraction
rag.py           retrieval for Coach
pipeline.py      the stages + CLI
orchestrator.py  LangGraph nightly run
evaluate.py      label loading + metrics
evaluate_stage1.py  compute_evaluation() -> the numbers /api/meta/evaluation serves
gen_openapi.py   writes frontend/openapi.json for type generation
migrate_to_postgres.py  one-off SQLite -> Postgres copy (idempotent, verifies counts)
app.py           Streamlit dashboard (superseded by frontend/, still working)
.github/workflows/nightly.yml  the 2am IST scheduled run (+ manual trigger)
companies.yaml   target company list
data/            resume.txt, labels_todo.csv, careerpilot.db, preferences.json,
                 evaluation_results.json  (gitignored)
```

Do not create files for phases not yet reached. Empty scaffolding is noise.

`frontend/src/components/ui/` is shadcn-generated. Don't hand-edit those beyond what the strict
tsconfig forces.

## Code conventions

- Small, testable functions. No class hierarchy until there's a second implementation.
- Pydantic for anything crossing a boundary (API responses, LLM outputs).
- All LLM calls go through one `LLMClient` interface so providers can be swapped by config.
- Exponential backoff on every external call, written from the start, not patched in after the
  first 429.
- Never commit: `.env`, `data/`, `*.db`, my resume.

## How to work with me

I am building this to learn RAG, agents, pipelines and orchestration — not just to have a
working tool. So:

- Explain design tradeoffs, don't just produce code.
- Before writing anything non-trivial, tell me the plan and what you'd name things. Wait for
  approval.
- When an approach has a known failure mode, say what it is up front.
- Push back if I ask for something that adds surface area without adding capability.
- Prefer the boring solution and tell me when the fancy one isn't earning its place.

## Evaluation

122 jobs hand-labeled good / weak / no in `data/labels_todo.csv` (37 good, 7 weak, 78 no).
`evaluate_stage1.py` compares three rankings — embedding cosine, stage-1 `fit_score`, and a
random baseline — on the subset with a cached stage-1 result. Every ranking change gets measured
against that set. "It seems to work" is not an acceptable answer.

**Current result: stage-1 MRR 0.132 against a random expectation of 0.121, on n=34.** That is
barely above chance. It ships as-is on the Evaluation page, stated in those words. Don't round
it, re-scale it, or bury it under a chart — the point of measuring was to find out.

Note the overlap caveat: only 34 of 122 labeled jobs have a stage-1 score, because stage 1 only
ever ran over rule-filter survivors. The numbers describe the ordering of jobs both rankers
actually saw, not the full labeled pool.

`data/labels.csv` (referred to in earlier versions of this file) never existed usefully — see
docs/decisions.md. `evaluate.py` reads `labels_todo.csv`.
