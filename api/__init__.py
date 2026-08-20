"""FastAPI layer over CareerPilot's existing modules.

This package wraps - never reimplements. Every endpoint calls straight into
the same functions the CLI and the Streamlit dashboard already use
(adapters/, filters.py, agents/, orchestrator.py, db.py, extraction.py,
rag.py, config.py). If an endpoint here needed logic that doesn't exist
elsewhere, that's a signal the logic belongs in the module it's about, not
in a router.

Read-only by default. The only endpoints that write anything are:
  POST /api/jobs/{content_hash}/status   application_status + applied_at
  POST /api/resume/confirm               data/resume.txt
  PUT  /api/preferences                  data/preferences.json
  POST /api/preferences/reset            data/preferences.json
  POST /api/companies                    companies.yaml
  POST /api/run                          triggers the orchestrator (which writes)
  POST /api/companies/scout              writes nothing; spends LLM quota

Nothing here submits an application to an employer, and no endpoint exists
that could - per CLAUDE.md, the system finds and ranks; the human applies.
"""
