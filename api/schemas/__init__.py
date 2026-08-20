"""Pydantic response/request models for the API layer.

Deliberately separate from models.py's domain types (JobPosting,
CompanyConfig, Preferences). Those describe what the system *is*; these
describe what the HTTP boundary *serves*, and the two should be free to
diverge - a JobPosting carries a full description that the list endpoint
must not ship 40 copies of, and an API response carries derived fields
(fit_score, verdict) that live in a different table entirely.
"""
