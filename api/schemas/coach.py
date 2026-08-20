from pydantic import BaseModel, Field


class SkillGap(BaseModel):
    skill: str
    count: int


class SkillGapsResponse(BaseModel):
    """Missing-skills frequency across stage-1-scored jobs below a
    threshold. Stage-1 only, deliberately: mixing stage-1 and stage-2 rows
    would aggregate judgments made by two different models into one count
    that means neither. See agents/coach.py."""

    threshold: int
    job_count: int
    skills: list[SkillGap] = Field(default_factory=list)


class AskRequest(BaseModel):
    question: str
    k: int = 12


class RetrievedChunk(BaseModel):
    """One retrieved JD requirements excerpt and its cosine similarity to
    the question."""

    text: str
    score: float


class AskResponse(BaseModel):
    """A Coach answer plus what it actually retrieved. The retrieved set is
    returned alongside the answer, not hidden, so the answer can be checked
    against its own evidence rather than taken on trust - the whole point of
    RAG over a chatbot.

    `retrieval_was_noop` is surfaced, not buried: at this project's current
    corpus size, k frequently exceeds the matching pool, meaning "retrieval"
    returned everything and ranked nothing. An answer built that way is
    still useful, but calling it retrieval-augmented would overstate what
    happened. See rag.RetrievalResult.is_noop."""

    question: str
    answer: str
    k: int
    retrieved: list[RetrievedChunk] = Field(default_factory=list)
    pool_size: int
    retrieved_count: int
    retrieval_was_noop: bool
    population_size: int
