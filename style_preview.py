"""Visual style preview - one of each dashboard element (job card per
verdict, unscored card, missing-skills overflow, stats strip, empty state,
tabs), with light and dark variants forced side by side so both are visible
regardless of your actual OS/browser theme.

Run: streamlit run style_preview.py

Why this exists: CSS bugs in app.py (invisible text, wrong layout order)
are otherwise undiagnosable from this environment - there's no real browser
available to inspect rendered DOM/computed styles, only AppTest, which
verifies the Python widget tree and HTML *content*, never how it actually
renders. Every fix to PAGE_CSS up to now was a best-reasoned guess, checked
against the installed streamlit package's own JS bundle for which
selectors are real, but never actually seen rendered. This page is the
missing verification step - after any change to PAGE_CSS in app.py, rerun
this and look at both columns directly, rather than discovering a broken
element piece by piece in the real dashboard.

Both variants are forced via explicit CSS classes (.cp-force-light /
.cp-force-dark) that redeclare every --cp-* custom property locally, rather
than relying on prefers-color-scheme - a real browser only ever reports
one OS theme at a time, so there's no way to see both side by side any
other way. This does NOT preview Streamlit's own native chrome (title,
tabs, buttons) in both themes - PAGE_CSS deliberately never touches native
element colour any more (see docs/decisions.md), precisely so it can't go
wrong regardless of which theme Streamlit itself is in. The one native tab
demo below reflects whatever theme your browser is actually in right now,
not a forced comparison.
"""

from datetime import datetime, timezone

import streamlit as st

from app import (
    PAGE_CSS,
    DashboardJob,
    render_empty_state_html,
    render_job_card_html,
    render_missing_chips_html,
    render_stats_strip_html,
    render_unscored_card_html,
)
from models import ATSSource, JobPosting

st.set_page_config(page_title="CareerPilot - Style Preview", page_icon="\U0001f3a8", layout="wide")
st.markdown(PAGE_CSS, unsafe_allow_html=True)

# Redeclares every --cp-* variable locally per column - the same values
# PAGE_CSS's :root and @media (prefers-color-scheme: dark) blocks define,
# copied here rather than imported, since there's no single source to pull
# "just the light values" or "just the dark values" from separately without
# a bigger refactor of PAGE_CSS itself. If PAGE_CSS's palette changes, this
# needs updating too - a real but accepted duplication, worth it for being
# able to see both at once.
FORCE_VARIANT_CSS = """
<style>
.cp-force-light, .cp-force-dark { padding: 1.25rem; border-radius: 12px; }
.cp-force-light {
    --cp-accent: #4361EE; --cp-accent-soft: #EEF1FF; --cp-text: #1A1D29; --cp-text-muted: #6B7280;
    --cp-border: #E5E7EB; --cp-bg-card: #FFFFFF; --cp-bg-page: #F8F9FB;
    --cp-strong-bg: #EAF7F1; --cp-strong-border: #57AB8C; --cp-strong-text: #1F6E52;
    --cp-possible-bg: #FBF3E4; --cp-possible-border: #C99A4C; --cp-possible-text: #8A6423;
    --cp-weak-bg: #F5EFEE; --cp-weak-border: #B08A85; --cp-weak-text: #7A5750;
    --cp-unscored-bg: #F1F2F4; --cp-unscored-border: #9CA3AF; --cp-unscored-text: #4B5563;
    background: var(--cp-bg-page); color: var(--cp-text);
}
.cp-force-dark {
    --cp-accent: #7C96FF; --cp-accent-soft: #232A4D; --cp-text: #E7E9EE; --cp-text-muted: #9AA1B2;
    --cp-border: #2E3244; --cp-bg-card: #1B1F2E; --cp-bg-page: #12141C;
    --cp-strong-bg: #163A2E; --cp-strong-border: #4F9E80; --cp-strong-text: #8FDCBB;
    --cp-possible-bg: #3A2F17; --cp-possible-border: #C99A4C; --cp-possible-text: #E4C07E;
    --cp-weak-bg: #332523; --cp-weak-border: #8C6B65; --cp-weak-text: #D2ADA6;
    --cp-unscored-bg: #262A38; --cp-unscored-border: #6B7280; --cp-unscored-text: #C3C8D3;
    background: var(--cp-bg-page); color: var(--cp-text);
}
</style>
"""
st.markdown(FORCE_VARIANT_CSS, unsafe_allow_html=True)

st.title("Style Preview")
st.caption(
    "Both variants forced explicitly via CSS custom properties, not your OS/browser theme - "
    "see this file's module docstring for why. If something is illegible here, it's a real bug "
    "in PAGE_CSS, not a rendering fluke."
)


def _sample_job(verdict: str, fit_score: int, is_new: bool = False) -> DashboardJob:
    job = JobPosting(
        source=ATSSource.ASHBY,
        source_job_id="preview",
        company="Acme Corp",
        title="Sample Job Title For Preview Purposes",
        location="Bangalore, India",
        description="Requirements: Python.",
        url="https://example.com/jobs/preview",
    )
    return DashboardJob(
        job=job,
        content_hash="preview",
        fit_score=fit_score,
        verdict=verdict,
        matched_skills=["Python", "FastAPI", "PostgreSQL"],
        missing_skills=[f"Skill {i}" for i in range(12)],  # 12 > MISSING_SKILLS_VISIBLE_COUNT - forces the "+N more" collapse
        years_required=4.0,
        resume_meets_it=(verdict == "strong"),
        reasoning="Sample reasoning sentence, long enough to check wrapping and line height look right.",
        model="gemini-3.5-flash-lite",
        application_status="new",
        first_seen=datetime.now(timezone.utc).replace(tzinfo=None),
        is_new=is_new,
        is_unscored=False,
    )


def _sample_unscored_job() -> DashboardJob:
    job = JobPosting(
        source=ATSSource.ASHBY,
        source_job_id="preview-unscored",
        company="Acme Corp",
        title="Sample Unscored Posting",
        location="Bangalore, India",
        description="No concrete requirements here.",
        url="https://example.com/jobs/preview-unscored",
    )
    return DashboardJob(
        job=job,
        content_hash="preview-unscored",
        fit_score=None,
        verdict="unscored",
        matched_skills=[],
        missing_skills=[],
        years_required=None,
        resume_meets_it=False,
        reasoning="No technical requirements were extracted from this posting.",
        model="gemini-3.5-flash-lite",
        application_status="new",
        first_seen=datetime.now(timezone.utc).replace(tzinfo=None),
        is_new=False,
        is_unscored=True,
    )


col_light, col_dark = st.columns(2)
for col, variant_class, label in [(col_light, "cp-force-light", "Light"), (col_dark, "cp-force-dark", "Dark")]:
    with col:
        st.subheader(label)
        parts = [f'<div class="{variant_class}">']
        parts.append(render_stats_strip_html(total_fetched=7564, survived=37, analyzed=30, unanalyzed=7))
        parts.append(render_job_card_html(_sample_job("strong", 85, is_new=True)))
        parts.append(render_job_card_html(_sample_job("possible", 55)))
        parts.append(render_job_card_html(_sample_job("weak", 20)))
        parts.append(render_unscored_card_html(_sample_unscored_job()))
        parts.append(
            render_empty_state_html(
                "Nothing matches this filter", "Try widening the status filter above, or check back later."
            )
        )
        parts.append("</div>")
        st.markdown("".join(parts), unsafe_allow_html=True)

st.divider()
st.subheader("Missing-skills chip overflow, isolated")
st.caption("The '+N more' <details> disclosure on its own, forced to each variant.")
col_light2, col_dark2 = st.columns(2)
for col, variant_class in [(col_light2, "cp-force-light"), (col_dark2, "cp-force-dark")]:
    with col:
        chips_html = render_missing_chips_html([f"Skill {i}" for i in range(15)])
        st.markdown(f'<div class="{variant_class}">{chips_html}</div>', unsafe_allow_html=True)

st.divider()
st.subheader("Native tabs (your browser's actual current theme, not a forced comparison)")
st.caption(
    "PAGE_CSS deliberately never sets colour on native Streamlit elements (tabs, title, buttons) "
    "any more - only Streamlit's own theme governs those, so there's nothing here that could go "
    "invisible the way the bug report described. This just confirms the active-tab border renders."
)
demo_tab_a, demo_tab_b, demo_tab_c = st.tabs(["First", "Second (active)", "Third"])
with demo_tab_b:
    st.write("This is the active tab - it should have a bold label and an accent-coloured bottom border.")
