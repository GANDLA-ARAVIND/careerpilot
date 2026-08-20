from typing import Optional

from pydantic import BaseModel, Field


class ResumeExtraction(BaseModel):
    """What extraction.extract_resume_sections pulled, and whether it
    actually recognized headers or fell back.

    `extracted=False` means no "Technical Skills"/"Projects" header matched
    and the FULL text would be sent to the Analyst instead of a trimmed
    section. That's surfaced as its own boolean rather than left implicit,
    on the same visible-fallback principle the JD extractor follows: a
    silent fallback looks exactly like a successful extraction from the
    outside, and would quietly change every score."""

    skills: str
    projects: str
    extracted: bool


class ResumeResponse(BaseModel):
    text: str
    length: int
    extraction: ResumeExtraction
    exists: bool


class ResumePreview(BaseModel):
    """A candidate resume, not yet saved. `warnings` are the mangled-PDF
    tripwires (unusually long words, low space ratio, big length drop) -
    empty means nothing obvious looked wrong, never that extraction is
    proven clean.

    `invalidates_cached_analyses` is how many currently-analyzed jobs would
    need re-scoring if this were saved: the resume text is part of every
    Analyst cache key, so changing it doesn't corrupt anything, it just
    stops matching - and the next run spends real quota re-analyzing."""

    text: str
    length: int
    extraction: ResumeExtraction
    warnings: list[str] = Field(default_factory=list)
    invalidates_cached_analyses: int
    previous_length: Optional[int] = None


class ResumeConfirmRequest(BaseModel):
    text: str


class ResumeConfirmResponse(BaseModel):
    saved: bool
    length: int
    invalidated_cached_analyses: int
