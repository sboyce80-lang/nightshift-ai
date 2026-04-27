#!/usr/bin/env python3
"""
Knight Shift — Database Engine + Session Machinery
==================================================
Centralized SQLAlchemy 2.0 setup. The web process and the RQ workers both
import from here so they share the same engine/session factory.

Use `session_scope()` as a context manager around any unit of work; it
commits on success, rolls back on exception, and always closes the session.

    from db import session_scope
    from models import Submission

    with session_scope() as s:
        sub = s.get(Submission, submission_id)
        sub.status = "processing"
"""

import os
import sys
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import DATABASE_URL


class Base(DeclarativeBase):
    """Shared declarative base — every model in models.py inherits this."""
    pass


# `pool_pre_ping=True` avoids stale-connection errors after a Postgres
# restart (Render free Postgres restarts periodically).
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional scope around a series of operations."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
