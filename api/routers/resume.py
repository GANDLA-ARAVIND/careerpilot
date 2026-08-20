"""Resume view, preview, and save.

Upload and save are deliberately two steps. Extraction from a PDF fails in
ways a byte count can't show - multi-column layouts interleave, tables
scramble, ligatures drop characters - and the resume text is baked into
every Analyst cache key, so saving a mangled one silently changes every
future score. POST /api/resume returns a preview and the invalidation
count; POST /api/resume/confirm is the only thing that writes.
"""

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from api.deps import get_session
from api.schemas.resume import (
    ResumeConfirmRequest,
    ResumeConfirmResponse,
    ResumeExtraction,
    ResumePreview,
    ResumeResponse,
)
import app as app_module  # module, not `from app import RESUME_PATH` - see _resume_path
from api.services.dashboard import (
    check_resume_extraction_quality,
    extract_pdf_text,
    load_dashboard_jobs,
    run_filter_pass,
)
from app import read_last_viewed
from extraction import extract_resume_sections

router = APIRouter(prefix="/api/resume", tags=["resume"])


def _extraction_of(text: str) -> ResumeExtraction:
    skills, projects, extracted = extract_resume_sections(text)
    return ResumeExtraction(skills=skills, projects=projects, extracted=extracted)


def _resume_path():
    """Read app.RESUME_PATH through the module at call time rather than
    binding it with `from app import RESUME_PATH` at import time.

    Import-time binding takes a copy of the value: a later reassignment of
    app.RESUME_PATH (which is exactly how the tests redirect writes to a
    temp directory) would leave this module still pointing at the original
    path. That isn't a test-only concern - it means the redirection people
    reasonably expect to work silently doesn't, and here the consequence
    would be overwriting the user's real resume during a test run. Same
    live-attribute pattern filters.py uses for config.TITLE_ALLOWLIST."""
    return app_module.RESUME_PATH


def _current_text() -> Optional[str]:
    path = _resume_path()
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _analyzed_count(session: Session) -> int:
    """How many jobs currently hold an Analyst verdict - i.e. how many
    would need re-scoring if the resume changed. Uses the same code path
    the dashboard does rather than counting analyst_results rows, because
    that table also holds verdicts for jobs no longer surviving filters."""
    filter_result = run_filter_pass(session)
    scored, unscored, _pending = load_dashboard_jobs(session, filter_result.kept, read_last_viewed())
    return len(scored) + len(unscored)


@router.get("", response_model=ResumeResponse)
def get_resume() -> ResumeResponse:
    text = _current_text()
    if text is None:
        return ResumeResponse(
            text="",
            length=0,
            extraction=ResumeExtraction(skills="", projects="", extracted=False),
            exists=False,
        )
    return ResumeResponse(text=text, length=len(text), extraction=_extraction_of(text), exists=True)


@router.post("", response_model=ResumePreview)
async def preview_resume(
    file: Optional[UploadFile] = File(default=None),
    text: Optional[str] = Form(default=None),
    session: Session = Depends(get_session),
) -> ResumePreview:
    """Accepts a PDF upload or pasted text. Saves nothing."""
    if file is None and not (text or "").strip():
        raise HTTPException(status_code=422, detail="Provide either a PDF file or non-empty text.")

    if file is not None:
        raw = await file.read()
        try:
            candidate = extract_pdf_text(raw)
        except Exception as exc:  # noqa: BLE001 - a corrupt PDF is a 422, not a 500
            raise HTTPException(status_code=422, detail=f"Could not extract text from this PDF: {exc}") from exc
    else:
        candidate = text or ""

    if not candidate.strip():
        raise HTTPException(status_code=422, detail="Extracted text is empty.")

    previous = _current_text()
    return ResumePreview(
        text=candidate,
        length=len(candidate),
        extraction=_extraction_of(candidate),
        warnings=check_resume_extraction_quality(candidate, previous),
        invalidates_cached_analyses=_analyzed_count(session),
        previous_length=len(previous) if previous is not None else None,
    )


@router.post("/confirm", response_model=ResumeConfirmResponse)
def confirm_resume(
    payload: ResumeConfirmRequest,
    session: Session = Depends(get_session),
) -> ResumeConfirmResponse:
    if not payload.text.strip():
        raise HTTPException(status_code=422, detail="Refusing to save an empty resume.")

    invalidated = _analyzed_count(session)
    path = _resume_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload.text, encoding="utf-8")
    return ResumeConfirmResponse(saved=True, length=len(payload.text), invalidated_cached_analyses=invalidated)
