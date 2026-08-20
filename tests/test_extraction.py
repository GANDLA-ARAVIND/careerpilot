from extraction import extract_jd_requirements, extract_resume_sections

# ---------------------------------------------------------------------------
# JD extraction
# ---------------------------------------------------------------------------


def test_colon_form_header_survives_newlineless_text():
    """Some source text has no line breaks at all (e.g. a JD field that was
    already flattened upstream) - the colon form must still work as a
    fallback signal even when the standalone-line form has nothing to match."""
    description = (
        "About PhonePe Limited: some company history and culture text here. "
        "Requirements: Strong passion for Android development. Strong problem-solving skills. "
        "Over 1-2 years of Android experience with Kotlin, MVVM, Dagger 2. "
        "We are building India's largest platform! PhonePe Full Time Employee Benefits Insurance Benefits."
    )
    text, extracted = extract_jd_requirements(description)
    assert extracted is True
    assert text.startswith("Strong passion for Android development")
    assert "Kotlin" in text
    assert "About PhonePe Limited" not in text


def test_standalone_line_header_survives_when_newlines_are_preserved():
    """Ashby/Lever plain text keeps real line breaks - the real Sarvam shape,
    header alone on its own line with no colon."""
    description = (
        "About Sarvam\n\nSarvam is building sovereign AI for India.\n\n"
        "What We're Looking For\n"
        "Strong Python and PyTorch.\n"
        "Experience with transformer architectures.\n\n"
        "Bonus Points\n"
        "Experience with vision-language models.\n\n"
        "Why Sarvam?\n"
        "We are a fast-moving team."
    )
    text, extracted = extract_jd_requirements(description)
    assert extracted is True
    assert "Strong Python and PyTorch" in text
    assert "vision-language models" in text  # Bonus Points included as content
    assert "fast-moving team" not in text  # stopped before the "Why Sarvam?" blurb
    assert "About Sarvam" not in text


def test_curly_apostrophe_header_still_matches():
    description = "Intro text.\n\nWhat We’re Looking For\nStrong Go and Rust experience.\n\nBenefits\nHealth insurance."
    text, extracted = extract_jd_requirements(description)
    assert extracted is True
    assert "Go and Rust" in text


def test_no_recognized_header_falls_back_to_full_text_and_reports_failure():
    description = "This posting describes the role in flowing prose with no labeled sections of any kind."
    text, extracted = extract_jd_requirements(description)
    assert extracted is False
    assert text == description  # unmodified fallback, not a guess


def test_stop_header_bounds_extraction_when_present():
    description = "Intro.\n\nQualifications\nPython and SQL required.\n\nBenefits\nHealth insurance and PTO."
    text, extracted = extract_jd_requirements(description)
    assert extracted is True
    assert "Python and SQL" in text
    assert "Health insurance" not in text


# ---------------------------------------------------------------------------
# Resume extraction
# ---------------------------------------------------------------------------


def test_resume_sections_extracted_between_recognized_headers():
    resume = (
        "Aravind Gandla\nProfessional Summary\nSome summary text about the candidate.\n\n"
        "Technical Skills\nPython, FastAPI, PostgreSQL, RAG, LangChain\n\n"
        "Projects\nWattWise - FastAPI energy tracker.\nSolvera - RAG math assistant.\n\n"
        "Education\nB.Tech, 2022-2026."
    )
    skills, projects, extracted = extract_resume_sections(resume)
    assert extracted is True
    assert "LangChain" in skills
    assert "Professional Summary" not in skills
    assert "WattWise" in projects
    assert "Education" not in projects
    assert "B.Tech" not in projects


def test_resume_missing_projects_header_reports_failure():
    resume = "Professional Summary\nSome text.\n\nTechnical Skills\nPython, SQL.\n\nEducation\nB.Tech."
    skills, projects, extracted = extract_resume_sections(resume)
    assert extracted is False
    assert skills == ""
    assert projects == ""


def test_resume_missing_skills_header_reports_failure():
    resume = "Professional Summary\nSome text.\n\nProjects\nWattWise.\n\nEducation\nB.Tech."
    skills, projects, extracted = extract_resume_sections(resume)
    assert extracted is False
    assert skills == ""
    assert projects == ""


# ---------------------------------------------------------------------------
# Non-ASCII punctuation survives extraction unchanged. A real bug turned out
# to be in pipeline.py's print() step (stdout defaulting to cp1252 on
# Windows when not a real console - see pipeline.py's __main__), not here -
# these tests lock in that this layer was never the problem, so a future
# regression here is caught precisely instead of blamed on the wrong file.
# ---------------------------------------------------------------------------


def test_jd_extraction_preserves_smart_quotes_and_dashes():
    description = (
        "About the Role\n\n"
        "Requirements\n"
        "Master’s degree or Ph.D. required.\n"
        "5–9 years of experience – senior candidates preferred.\n"
        "This is a hybrid role — 3 days a week in office."
    )
    text, extracted = extract_jd_requirements(description)
    assert extracted is True
    assert "Master’s degree" in text
    assert "5–9 years" in text
    assert "hybrid role — 3 days" in text


def test_resume_extraction_preserves_smart_quotes_and_dashes():
    resume = (
        "Technical Skills\n"
        "Python, Node.js – backend focus.\n\n"
        "Projects\n"
        "Built a system that’s used by 1000+ users — see GitHub.\n\n"
        "Education\nB.Tech."
    )
    skills, projects, extracted = extract_resume_sections(resume)
    assert extracted is True
    assert "Node.js – backend focus" in skills
    assert "that’s used by 1000+ users — see GitHub" in projects
