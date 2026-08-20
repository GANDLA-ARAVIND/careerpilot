import io

import pypdf

from config import GEMINI_MODEL_STAGE1
from tests.conftest import add_analyst_result


def _make_pdf_bytes() -> bytes:
    """A structurally valid single-page PDF built with pypdf's own writer,
    not a hand-rolled byte string.

    add_blank_page() both creates and appends - calling add_page() on its
    return value as well adds the same page object twice and produces a
    file pypdf then rejects with "cyclic page references". The page carries
    no text, which is deliberate: it stands in for a scanned/image-only
    resume, the case that must be refused rather than saved empty."""
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# GET /api/resume
# ---------------------------------------------------------------------------


def test_get_resume_returns_current_text_and_extraction(client):
    body = client.get("/api/resume").json()

    assert body["exists"] is True
    assert "Python" in body["text"]
    assert body["extraction"]["extracted"] is True
    assert "Python" in body["extraction"]["skills"]


def test_get_resume_when_none_saved(client, temp_env):
    temp_env["resume_path"].unlink()

    body = client.get("/api/resume").json()

    assert body["exists"] is False
    assert body["length"] == 0


# ---------------------------------------------------------------------------
# POST /api/resume  (preview - writes nothing)
# ---------------------------------------------------------------------------


def test_preview_pasted_text_does_not_save(client, temp_env):
    before = temp_env["resume_path"].read_text(encoding="utf-8")

    body = client.post("/api/resume", data={"text": "Technical Skills\nGo, Rust\n\nProjects\nAnother thing."}).json()

    assert "Go, Rust" in body["text"]
    assert temp_env["resume_path"].read_text(encoding="utf-8") == before  # unchanged


def test_preview_reports_invalidation_count(client, seeded, temp_env):
    add_analyst_result(temp_env["engine"], seeded["survivor_a"], model=GEMINI_MODEL_STAGE1)

    body = client.post("/api/resume", data={"text": "Technical Skills\nGo\n\nProjects\nX."}).json()

    assert body["invalidates_cached_analyses"] == 1


def test_preview_flags_a_mangled_extraction(client):
    """A single 400-character run with no spaces trips the long-word and
    space-ratio tripwires - the multi-column-PDF failure mode."""
    body = client.post("/api/resume", data={"text": "x" * 400}).json()

    assert body["warnings"]


def test_preview_warns_when_extractor_falls_back(client):
    """No recognized headers means the FULL text goes to the Analyst
    instead of a trimmed section - visible, not silent."""
    body = client.post("/api/resume", data={"text": "just some prose with no recognised headers at all"}).json()

    assert body["extraction"]["extracted"] is False


def test_preview_accepts_a_pdf_upload(client, monkeypatch):
    """The upload path end to end. extract_pdf_text is stubbed because
    pypdf can write a valid PDF but not typeset text into one, so a
    pypdf-authored file extracts to nothing - the real extractor is
    covered by tests/test_app.py against a fixture PDF. What's under test
    here is the HTTP path: multipart in, preview out."""
    import api.routers.resume as resume_router

    monkeypatch.setattr(
        resume_router, "extract_pdf_text", lambda data: "Technical Skills\nRust\n\nProjects\nFrom a PDF."
    )

    response = client.post("/api/resume", files={"file": ("resume.pdf", _make_pdf_bytes(), "application/pdf")})

    assert response.status_code == 200
    assert "Rust" in response.json()["text"]


def test_preview_rejects_a_pdf_that_yields_no_text(client):
    """A structurally valid PDF whose pages contain no extractable text
    (scanned images, or the blank page pypdf writes here) must be a clear
    422, not a silently-saved empty resume."""
    response = client.post("/api/resume", files={"file": ("resume.pdf", _make_pdf_bytes(), "application/pdf")})

    assert response.status_code == 422
    assert "empty" in response.json()["detail"].lower()


def test_preview_rejects_a_corrupt_pdf_with_422_not_500(client):
    response = client.post("/api/resume", files={"file": ("resume.pdf", b"not a pdf at all", "application/pdf")})

    assert response.status_code == 422


def test_preview_requires_some_input(client):
    assert client.post("/api/resume", data={}).status_code == 422


def test_preview_rejects_whitespace_only_text(client):
    assert client.post("/api/resume", data={"text": "   \n  "}).status_code == 422


# ---------------------------------------------------------------------------
# POST /api/resume/confirm  (the only write)
# ---------------------------------------------------------------------------


def test_confirm_saves_the_text(client, temp_env):
    new_text = "Technical Skills\nElixir\n\nProjects\nSomething new."

    body = client.post("/api/resume/confirm", json={"text": new_text}).json()

    assert body["saved"] is True
    assert temp_env["resume_path"].read_text(encoding="utf-8") == new_text


def test_confirm_reports_how_many_analyses_go_stale(client, seeded, temp_env):
    add_analyst_result(temp_env["engine"], seeded["survivor_a"], model=GEMINI_MODEL_STAGE1)

    body = client.post("/api/resume/confirm", json={"text": "Technical Skills\nElixir\n\nProjects\nX."}).json()

    assert body["invalidated_cached_analyses"] == 1


def test_confirm_refuses_empty_text(client, temp_env):
    before = temp_env["resume_path"].read_text(encoding="utf-8")

    assert client.post("/api/resume/confirm", json={"text": "  "}).status_code == 422
    assert temp_env["resume_path"].read_text(encoding="utf-8") == before
