"""Analyst agent: given a job's requirements and the candidate's resume,
returns a Pydantic-validated fit judgment. Structured shape is enforced by
Gemini's native responseSchema (see llm.py); AnalystResult re-validates the
parsed JSON regardless, so validation behaves identically no matter which
provider answered.

Only the extracted requirements/skills sections are sent, not full
documents - extraction.py already established that full JDs and resumes are
mostly boilerplate (see ranking.py, docs/decisions.md). Results are cached
in the database keyed on a hash of everything that determines the output -
the calling client's model name, SYSTEM_INSTRUCTION, and the exact (resume
text, requirements text) sent - the same fix as the embedding cache
staleness bug: keying on content_hash/description_hash, or on the input
text alone leaving the model or prompt out, would let an extraction.py
tuning, a resume edit, a prompt change, or (new in the two-stage design) a
different model silently serve a stale verdict under an unchanged key. See
docs/decisions.md.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from db import AnalystResultRow
from extraction import extract_jd_requirements, extract_resume_sections
from llm import LLMClient
from models import JobPosting

RESUME_PATH = Path("data/resume.txt")

# Deliberately no "fresher / entry-level / Hyderabad" framing: that invites
# sympathetic scoring. This is a cold read of demonstrated fit; whether a gap
# is worth stretching for is the user's call, not something to pre-soften.
SYSTEM_INSTRUCTION = """Compare the following two texts and judge the candidate's demonstrated fit for this job, based only on what is shown.

Rules, followed exactly:

1. matched_skills and missing_skills must contain ONLY concrete technologies, tools, programming languages, frameworks, platforms, or specific domain experience (examples: "Python", "Kubernetes", "distributed systems", "3+ years backend experience"). Never include generic professional attributes or soft skills - "communication skills", "attention to detail", "ownership mindset", "collaboration", "problem-solving", "ability to work in a fast-paced environment" and similar are not skills for this comparison. Exclude them from both lists entirely, even if the job posting states them as requirements.

2. matched_skills must contain ONLY items that are BOTH explicitly asked for in the job requirements AND explicitly present in the resume text below. A skill being present in the resume is not enough by itself - it must also be something this specific job asks for. Do not list every technology the resume happens to mention. Never include a skill because it's common, because it's implied by a related skill, or because a strong candidate would probably have it. If it is not literally present in the resume text, it does not belong in matched_skills.

3. missing_skills lists concrete skills or requirements stated in the job's requirements that do NOT appear in the resume.

4. fit_score is 0-100, reflecting alignment between what the job asks for and what the resume actually demonstrates - not potential, not trainability, only demonstrated fit. Experience is a hard constraint, not just one signal among many: if the job states a required experience level and the resume does not demonstrate enough professional experience to meet it, that gap must cap fit_score - strong skill overlap cannot compensate for it. A large shortfall (the resume showing roughly half or less of the years required - for example 0-2 years of experience against a 5+ year requirement) must result in a fit_score below 40, regardless of how many individual technologies match. A candidate who does not meet the stated experience bar is not a "possible" fit no matter how many listed tools they know. A small shortfall (within about 1-2 years of the requirement) may still land in the 40-74 range if skill alignment is otherwise strong. When experience is not stated, or the resume meets or exceeds what's required, judge fit_score on skill alignment alone.

5. experience_gap.years_required is the years of experience stated in the job's requirements, as a number. If no specific figure is stated, use null. experience_gap.resume_meets_it is true if the resume demonstrates at least that much professional experience, false otherwise.

6. reasoning is exactly one sentence. Be direct and honest, not diplomatic - if the fit is weak, say so plainly and say why, don't soften it.

Return only the structured fields requested. Do not add commentary outside them."""

# Gemini's structured-output schema format (Google's OpenAPI-3.0 subset,
# UPPERCASE types) - not the same shape as Pydantic's own model_json_schema()
# output, so this is hand-written and kept in sync with AnalystResult by
# hand. Small, stable schema; not worth a generic converter for one caller.
_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "fit_score": {"type": "INTEGER"},
        "matched_skills": {"type": "ARRAY", "items": {"type": "STRING"}},
        "missing_skills": {"type": "ARRAY", "items": {"type": "STRING"}},
        "experience_gap": {
            "type": "OBJECT",
            "properties": {
                "years_required": {"type": "NUMBER", "nullable": True},
                "resume_meets_it": {"type": "BOOLEAN"},
            },
            "required": ["resume_meets_it"],
        },
        "reasoning": {"type": "STRING"},
    },
    "required": ["fit_score", "matched_skills", "missing_skills", "experience_gap", "reasoning"],
}


class ExperienceGap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    years_required: Optional[float] = None
    resume_meets_it: bool


class AnalystResult(BaseModel):
    """LLM output only - no verdict field. Keeping fit_score and verdict
    consistent is arithmetic, not a judgment call worth asking the model to
    make; see derive_verdict."""

    model_config = ConfigDict(extra="forbid")

    fit_score: int = Field(ge=0, le=100)
    matched_skills: list[str]
    missing_skills: list[str]
    experience_gap: ExperienceGap
    reasoning: str


def derive_verdict(fit_score: int) -> str:
    if fit_score >= 75:
        return "strong"
    if fit_score >= 40:
        return "possible"
    return "weak"


def is_unscored(result: AnalystResult) -> bool:
    """True when both matched_skills and missing_skills came back empty -
    the model had no concrete technical requirements to compare the resume
    against. Verified against a real case, not assumed: Broccoli's
    "Software Engineer" posting extracted cleanly (the right section, no
    header-recognition bug - see docs/decisions.md), and that section
    genuinely states no concrete technology, only generic ownership/
    curiosity language plus a bare years-of-experience figure. In this
    state fit_score is not a real comparison - it's the model's impression
    of the resume alone, with nothing to weigh it against - and must not be
    trusted or displayed as one, whichever direction it happens to land."""
    return not result.matched_skills and not result.missing_skills


def derive_outcome(result: AnalystResult) -> str:
    """"unscored" when is_unscored(result) - not one of derive_verdict's
    three score-derived buckets, a fourth, distinct outcome. Callers
    holding a full AnalystResult (not just a bare fit_score) should use
    this instead of calling derive_verdict directly, since only this
    function has both facts available."""
    if is_unscored(result):
        return "unscored"
    return derive_verdict(result.fit_score)


def prepare_resume_text(resume_path: Path = RESUME_PATH) -> str:
    """Skills + projects only, same extraction ranking.py already uses.
    Falls back to the full resume if extraction can't find the headers -
    same fallback shape as ranking.py, but the cost of a miss is much lower
    here: Gemini's context window doesn't have MiniLM's 256-token wall."""
    resume_text = resume_path.read_text(encoding="utf-8")
    skills, projects, extracted = extract_resume_sections(resume_text)
    return f"{skills}\n\n{projects}" if extracted else resume_text


def _text_hash(model: str, system_instruction: str, resume_text: str, requirements_text: str) -> str:
    """Hashes everything that determines the output, not just the job-
    specific inputs. A prompt tune (a new rule, a reworded constraint)
    changes what a fixed (resume, requirements) pair would produce; a
    different model changes it even more - the two-stage design runs a
    cheap model over everyone and a stronger model over the top candidates,
    and those two results for the same job must never collide under one
    cache entry. See this module's docstring and docs/decisions.md."""
    combined = f"{model}\n***\n{system_instruction}\n===\n{resume_text}\n---\n{requirements_text}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def _user_message(resume_text: str, requirements_text: str) -> str:
    return (
        f"RESUME (skills and projects):\n{resume_text}\n\n"
        f"JOB REQUIREMENTS:\n{requirements_text}\n\n"
        "Evaluate this candidate's fit for this role."
    )


def _row_to_result(row: AnalystResultRow) -> AnalystResult:
    return AnalystResult(
        fit_score=row.fit_score,
        matched_skills=json.loads(row.matched_skills),
        missing_skills=json.loads(row.missing_skills),
        experience_gap=ExperienceGap(
            years_required=row.experience_years_required,
            resume_meets_it=row.resume_meets_experience,
        ),
        reasoning=row.reasoning,
    )


def _store(session: Session, text_hash: str, model: str, result: AnalystResult) -> None:
    session.add(
        AnalystResultRow(
            text_hash=text_hash,
            model=model,
            fit_score=result.fit_score,
            matched_skills=json.dumps(result.matched_skills),
            missing_skills=json.dumps(result.missing_skills),
            experience_years_required=result.experience_gap.years_required,
            resume_meets_experience=result.experience_gap.resume_meets_it,
            verdict=derive_outcome(result),
            reasoning=result.reasoning,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    )
    session.commit()


def analyze(job: JobPosting, resume_text: str, client: LLMClient, session: Session) -> tuple[AnalystResult, bool]:
    """Returns (result, from_cache). Only calls the LLM on a cache miss.
    Cache key includes client.model_name, so calling this with two different
    clients on the same job is exactly how the two Analyst stages coexist
    without one overwriting the other."""
    requirements_text, _ = extract_jd_requirements(job.description)
    text_hash = _text_hash(client.model_name, SYSTEM_INSTRUCTION, resume_text, requirements_text)

    existing = session.get(AnalystResultRow, text_hash)
    if existing is not None:
        return _row_to_result(existing), True

    raw = client.complete(SYSTEM_INSTRUCTION, _user_message(resume_text, requirements_text), _RESPONSE_SCHEMA)
    result = AnalystResult.model_validate_json(raw)

    _store(session, text_hash, client.model_name, result)
    return result, False
