"""Shared FastAPI dependencies.

The engine is a module-level singleton created on first use rather than at
import time, so importing api.main (which tests and `uvicorn --reload` both
do freely) never touches the filesystem or runs db.py's migrations as a
side effect of the import itself.

get_session is the seam tests override to point the whole app at a temp
database - see tests/test_api_*.py's `client` fixture. Overriding the
dependency rather than monkeypatching db.get_engine keeps the redirection
inside FastAPI's own mechanism, where it's scoped to one TestClient and
can't leak into another test.
"""

from typing import Iterator, Optional

from sqlalchemy.orm import Session

from db import get_engine

_engine = None


def get_app_engine():
    """Process-wide engine. SQLAlchemy pools connections internally, and
    db.get_engine runs create_all + the applied_at migration + the WAL
    pragma every call, so building one engine and reusing it is both
    cheaper and less repetitive than one per request."""
    global _engine
    if _engine is None:
        _engine = get_engine()
    return _engine


def reset_app_engine(engine: Optional[object] = None) -> None:
    """Test hook: swap (or clear) the cached engine. Not used by the app."""
    global _engine
    _engine = engine


def get_session() -> Iterator[Session]:
    with Session(get_app_engine()) as session:
        yield session
