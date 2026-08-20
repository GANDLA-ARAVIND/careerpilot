"""Coach: the missing-skills aggregate and RAG-backed market questions."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from agents.coach import missing_skills_below
from api.deps import get_session
from api.schemas.coach import AskRequest, AskResponse, RetrievedChunk, SkillGap, SkillGapsResponse
from config import GEMINI_MODEL_STAGE1, GEMINI_RATE_LIMITS

router = APIRouter(prefix="/api/coach", tags=["coach"])

DEFAULT_THRESHOLD = 40
MAX_SKILLS = 50


@router.get("/skill-gaps", response_model=SkillGapsResponse)
def skill_gaps(
    threshold: int = Query(default=DEFAULT_THRESHOLD, ge=0, le=100),
    limit: int = Query(default=20, ge=1, le=MAX_SKILLS),
    session: Session = Depends(get_session),
) -> SkillGapsResponse:
    report = missing_skills_below(session, threshold)
    return SkillGapsResponse(
        threshold=threshold,
        job_count=report.job_count,
        skills=[SkillGap(skill=skill, count=count) for skill, count in report.skill_counts[:limit]],
    )


@router.post("/ask", response_model=AskResponse)
def ask(payload: AskRequest, session: Session = Depends(get_session)) -> AskResponse:
    """A market-gap question, answered over retrieved JDs.

    Spends one LLM call. Imports agents.coach's market_gap and the LLM
    client lazily so that merely importing the API doesn't pull the RAG
    stack (which reaches ranking.py, and through it sentence-transformers)
    into every process that serves a job list."""
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="question cannot be empty")

    from agents.coach import load_market_population, market_gap
    from llm import GeminiClient

    rpm = GEMINI_RATE_LIMITS.get(GEMINI_MODEL_STAGE1, {}).get("rpm")
    client = GeminiClient(model=GEMINI_MODEL_STAGE1, requests_per_minute=rpm)

    population = load_market_population(session)
    if not population:
        raise HTTPException(
            status_code=409,
            detail="No jobs in the India-fresher population to retrieve from - run the pipeline first.",
        )

    report = market_gap(question, session, client, k=payload.k)
    retrieval = report.retrieval
    return AskResponse(
        question=report.question,
        answer=report.answer,
        k=payload.k,
        retrieved=[RetrievedChunk(text=text, score=float(score)) for text, score in retrieval.chunks],
        pool_size=retrieval.pool_size,
        retrieved_count=retrieval.retrieved_count,
        retrieval_was_noop=retrieval.is_noop,
        population_size=len(population),
    )
