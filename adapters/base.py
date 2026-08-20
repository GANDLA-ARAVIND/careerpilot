import html
import re
import time
from typing import Optional

import requests

from models import RemoteType


class ATSAdapterError(Exception):
    """Base for adapter-level failures. pipeline.py decides whether to continue."""


class BoardNotFoundError(ATSAdapterError):
    """The configured board token doesn't exist on the ATS — a companies.yaml
    config error, not a transient failure. Never swallowed inside an adapter."""

    def __init__(self, source: str, company: str, board_token: str):
        self.source = source
        self.company = company
        self.board_token = board_token
        super().__init__(
            f"{source}: no board found for company={company!r} token={board_token!r} "
            "(check companies.yaml)"
        )


class RobotsDisallowedError(ATSAdapterError):
    """robots.txt disallows the path this adapter needs to fetch. Distinct
    from BoardNotFoundError - the board is real, the config is correct, but
    fetching it isn't permitted. CLAUDE.md's own rule for the scraping
    fallback ("must respect robots.txt") applies here too: the check runs
    before any real request, and a disallowed tenant is skipped entirely,
    not queried-then-discarded. See adapters/workday.py, the first adapter
    that needs this (Greenhouse/Lever/Ashby's board APIs carry no robots.txt
    restriction on the endpoints these adapters use)."""

    def __init__(self, source: str, company: str, url: str):
        self.source = source
        self.company = company
        self.url = url
        super().__init__(f"{source}: robots.txt disallows {url!r} for company={company!r}")


_HYBRID_RE = re.compile(r"\bhybrid\b", re.IGNORECASE)
_REMOTE_RE = re.compile(r"\bremote\b", re.IGNORECASE)


def infer_remote_type(location: Optional[str]) -> RemoteType:
    """Word-boundary match, not substring — a plain "remote" in location.lower()
    would also fire on things like "Remotely" or a place name containing it.
    Hybrid is checked first since it's the more specific signal when a listing
    mentions both (e.g. "Remote/Hybrid options")."""
    if not location:
        return RemoteType.UNKNOWN
    if _HYBRID_RE.search(location):
        return RemoteType.HYBRID
    if _REMOTE_RE.search(location):
        return RemoteType.REMOTE
    return RemoteType.UNKNOWN


def resolve_remote_type(
    structured_value: Optional[str], mapping: dict[str, RemoteType], location: Optional[str]
) -> RemoteType:
    """Shared "trust the structured field, else infer from location" policy.
    `structured_value` is lowercased before the lookup so each source's own
    casing (Ashby's "OnSite", Lever's "onsite"/"on-site") doesn't need a branch
    here - the caller's `mapping` just needs lowercase keys. Centralized so this
    fallback policy changes in one place as real location strings get tuned,
    not once per adapter."""
    mapped = mapping.get((structured_value or "").lower())
    if mapped is not None and mapped is not RemoteType.UNKNOWN:
        return mapped
    return infer_remote_type(location)


_BLOCK_TAG_RE = re.compile(r"</?(?:p|div|h[1-6]|li|br|tr)\b[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(raw: str) -> str:
    """Unescape entities first, then strip tags — some ATSs (Greenhouse) return
    HTML with its own tags entity-escaped, so stripping tags before unescaping
    matches nothing and leaves literal "&lt;h2&gt;"-style text behind.

    Block-level tags (p, div, h1-h6, li, br, tr) become newlines instead of
    collapsing to a space, so paragraph and list-item boundaries survive into
    the plain text — extraction.py's section-header detection depends on
    real line breaks existing, and the raw HTML already has this structure;
    collapsing it away here was throwing away a signal we now need. All
    other (inline) tags still collapse to a space, as before."""
    text = html.unescape(raw)
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"[^\S\n]+", " ", text)  # collapse horizontal whitespace, but not newlines
    text = re.sub(r"[^\S\n]*\n[^\S\n]*", "\n", text)  # trim spaces hugging a newline
    text = re.sub(r"\n{3,}", "\n\n", text)  # cap blank-line runs at one blank line
    return text.strip()


MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 1.0
TIMEOUT_SECONDS = 15


def request_with_backoff(url: str, params: dict) -> requests.Response:
    """Shared GET-with-retry for ATS job board APIs. 404 is returned, not raised
    or retried — it's not transient, and only the caller knows whether "not
    found" means "invalid board token" for its own source. 429/5xx are retried
    with exponential backoff; any other non-2xx is wrapped and raised as
    ATSAdapterError rather than leaking a raw requests exception."""
    last_error: Optional[Exception] = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
        try:
            response = requests.get(url, params=params, timeout=TIMEOUT_SECONDS)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = exc
            continue

        if response.status_code == 404:
            return response
        if response.status_code == 429 or response.status_code >= 500:
            last_error = requests.HTTPError(f"{response.status_code} from {url}")
            continue
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise ATSAdapterError(f"Request to {url} failed: {exc}") from exc
        return response

    raise ATSAdapterError(
        f"Request to {url} failed after {MAX_ATTEMPTS} attempts: {last_error}"
    ) from last_error
