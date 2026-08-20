"""Section extraction for ranking.py: pull the skills/requirements portion
out of a JD or resume before embedding, instead of feeding the whole
(truncation-prone) document in. Pattern-based on section headers - which
means it WILL miss headers it doesn't recognize. That's the dangerous
failure mode: a silent miss looks like every other score, so every function
here returns an explicit success flag instead of quietly falling back.
Callers must surface that flag, not swallow it.
"""

import re

from config import JD_CONTENT_HEADERS, JD_STOP_HEADERS, RESUME_PROJECT_HEADERS, RESUME_SKILL_HEADERS, RESUME_STOP_HEADERS

# Matches "Why Sarvam?" / "Why Us" style standalone headers - a common
# company-specific pattern no fixed phrase list can enumerate.
_WHY_HEADER_RE = re.compile(r"(?:^|\n)[ \t]*why\s+\w+\??[ \t]*(?:\n|$)", re.IGNORECASE)


def _iter_header_matches(text: str, headers: list[str]):
    """Yield (start, end) for every occurrence of any header in `headers`,
    matched two ways: "Header:" (survives HTML-stripped text where line
    breaks are gone - e.g. Greenhouse) and header alone on its own line
    (works when the source preserves line breaks - e.g. Ashby, Lever).
    Apostrophes are matched loosely (straight or curly) since source text
    varies."""
    for header in headers:
        pattern = re.escape(header).replace("'", "['’]")
        for m in re.finditer(rf"\b{pattern}\b\s*:", text, re.IGNORECASE):
            yield (m.start(), m.end())
        for m in re.finditer(rf"(?:^|\n)[ \t]*{pattern}[ \t]*(?:\n|$)", text, re.IGNORECASE):
            yield (m.start(), m.end())


def _earliest_match_end(text: str, headers: list[str]) -> int | None:
    matches = list(_iter_header_matches(text, headers))
    return min(matches, key=lambda pair: pair[0])[1] if matches else None


def _earliest_match_start_after(text: str, headers: list[str], after: int) -> int | None:
    starts = [start for start, _ in _iter_header_matches(text, headers) if start > after]
    starts += [m.start() for m in _WHY_HEADER_RE.finditer(text) if m.start() > after]
    return min(starts) if starts else None


def extract_jd_requirements(description: str) -> tuple[str, bool]:
    """(text, extracted). extracted=False means no recognized header was
    found anywhere in the description - text is then the full, unmodified
    description, an explicit fallback the caller must report, not a
    best-effort guess."""
    start = _earliest_match_end(description, JD_CONTENT_HEADERS)
    if start is None:
        return description, False

    end = _earliest_match_start_after(description, JD_STOP_HEADERS, start)
    if end is None:
        end = len(description)

    extracted_text = description[start:end].strip()
    if not extracted_text:
        return description, False

    return extracted_text, True


def extract_resume_sections(resume_text: str) -> tuple[str, str, bool]:
    """(skills_text, projects_text, extracted). extracted=False means either
    section's header wasn't found - both strings are then empty and the
    caller must fall back to the full resume text and report the failure."""
    skills_start = _earliest_match_end(resume_text, RESUME_SKILL_HEADERS)
    projects_start = _earliest_match_end(resume_text, RESUME_PROJECT_HEADERS)

    if skills_start is None or projects_start is None:
        return "", "", False

    skills_end = _earliest_match_start_after(resume_text, RESUME_PROJECT_HEADERS + RESUME_STOP_HEADERS, skills_start)
    if skills_end is None:
        skills_end = len(resume_text)

    projects_end = _earliest_match_start_after(resume_text, RESUME_STOP_HEADERS, projects_start)
    if projects_end is None:
        projects_end = len(resume_text)

    skills_text = resume_text[skills_start:skills_end].strip()
    projects_text = resume_text[projects_start:projects_end].strip()

    if not skills_text or not projects_text:
        return "", "", False

    return skills_text, projects_text, True
