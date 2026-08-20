"""Single point of contact between the API and app.py's data helpers.

app.py is the Streamlit dashboard, but its data functions are pure and
already tested (tests/test_app.py), and the module is importable without
Streamlit - its `import streamlit as st` lives inside `if __name__ ==
"__main__"`. Re-implementing load_dashboard_jobs' cache-key derivation or
run_filter_pass' SQL-pushdown filtering in the API would mean two
implementations of the same logic drifting apart, which is exactly what
"wrap, don't reimplement" is meant to prevent.

The coupling is real but deliberately contained: every router imports from
*this* module, never from `app` directly. When the React frontend replaces
the Streamlit dashboard and app.py is deleted, these functions move to a
neutral home and only the import line below changes - one file, one edit,
instead of hunting through seven routers.
"""

from app import (  # noqa: F401 - re-exported on purpose, see module docstring
    DashboardJob,
    FilterPassResult,
    check_resume_extraction_quality,
    compute_company_stats,
    extract_pdf_text,
    load_applied_jobs,
    load_dashboard_jobs,
    run_filter_pass,
)

__all__ = [
    "DashboardJob",
    "FilterPassResult",
    "check_resume_extraction_quality",
    "compute_company_stats",
    "extract_pdf_text",
    "load_applied_jobs",
    "load_dashboard_jobs",
    "run_filter_pass",
]
