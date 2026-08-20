"""FastAPI application.

Run it with:  uvicorn api.main:app --reload --port 8000

CORS is open to the usual Vite/CRA dev-server origins only. This is a
single-user tool that runs on localhost (see CLAUDE.md) - there is no
deployment where a third-party origin should be calling it, so
allow_origins is an explicit list rather than "*". Using "*" would also
silently break the moment credentials are ever sent, since the two are
incompatible in the CORS spec.
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routers import coach, companies, jobs, meta, preferences, resume, run
from api.services.run_manager import run_manager


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Registers the event loop with the run manager.

    This runs on the loop, which is the point: POST /api/run is a sync
    `def` endpoint, so FastAPI executes it in a threadpool where
    asyncio.get_running_loop() raises. Without capturing the loop here,
    the manager would have no loop to marshal progress events onto and
    every live SSE dispatch would silently do nothing - replay would still
    work, so the failure looks like "the stream connects but never
    updates". Found by driving a real uvicorn server; in-process test
    clients never exercised it."""
    run_manager.set_loop(asyncio.get_running_loop())
    yield
    run_manager.set_loop(None)

DEV_ORIGINS = [
    "http://localhost:5173",  # Vite default
    "http://127.0.0.1:5173",
    "http://localhost:3000",  # CRA / Next default
    "http://127.0.0.1:3000",
]

app = FastAPI(
    title="CareerPilot — Multi-agent AI job discovery pipeline",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "Three agents (Scout finds company job boards, Analyst scores postings against my "
        "resume, Coach identifies recurring skill gaps), LangGraph orchestration, RAG over the "
        "job archive, and a hand-labeled evaluation set.\n\n"
        "This is the HTTP layer over that pipeline. It wraps adapters/, filters.py, agents/, "
        "orchestrator.py, db.py, extraction.py and rag.py - it does not reimplement any of "
        "them.\n\n"
        "**The system finds and ranks; the human applies.** Nothing here submits an application "
        "to an employer, and no endpoint exists that could - that is a design decision, not a "
        "missing feature.\n\n"
        "Ranking quality is measured against the hand-labeled set rather than assumed. The "
        "current result is marginal - stage-1 MRR sits only slightly above the random baseline "
        "on a small overlap. See GET /api/meta/evaluation for the numbers and their caveats."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=DEV_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in (jobs, run, resume, preferences, companies, coach, meta):
    app.include_router(module.router)


@app.get("/api/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}
