"""Thread-local dedicated SQL Server connections for background workers."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from database.connection import SQLServerConnection


@contextmanager
def dedicated_connection(*, commit: bool = True) -> Iterator[SQLServerConnection]:
    """Open a private connection for one background task; close when done."""
    db = SQLServerConnection()
    db.connect()
    try:
        yield db
        if commit:
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
