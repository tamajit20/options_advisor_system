"""
SQL Server BACKUP DATABASE onto the host ./backups bind-mount.

The scheduler runs inside options_advisor, which has no docker CLI, so
deploy/backup.sh (docker compose exec / docker cp) cannot work there.
sqlserver writes the .bak to /var/opt/mssql/host-backups, which compose
bind-mounts to ./backups on the VM.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from database.connection import SQLServerConnection
from utils import now_ist

logger = logging.getLogger(__name__)

# Path inside the sqlserver container (docker-compose bind-mount).
SQL_BIND_BACKUP_DIR = "/var/opt/mssql/host-backups"

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BAK_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.bak$")
_MIN_BAK_BYTES = 1024


def sql_ident(name: str) -> str:
    """Allow only simple unquoted identifiers (database / table names)."""
    if not name or not _IDENT_RE.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


def bak_filename(name: str) -> str:
    if not name or not _BAK_NAME_RE.match(name):
        raise ValueError(f"unsafe backup filename: {name!r}")
    return name


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def host_backup_dir() -> Path:
    """Advisor-side path for ./backups (bind-mounted in Docker)."""
    return repo_root() / "backups"


def prepare_backup_dirs() -> Path:
    """Create ./backups (+ archive/) and chmod so mssql uid 10001 can write."""
    root = host_backup_dir()
    archive = root / "archive"
    root.mkdir(parents=True, exist_ok=True)
    archive.mkdir(parents=True, exist_ok=True)
    for path in (root, archive):
        try:
            path.chmod(0o777)
        except OSError:
            logger.debug("chmod 777 %s failed", path, exc_info=True)
    return root


def sql_backup_disk(filename: str, *, subdir: str = "") -> str:
    name = bak_filename(filename)
    if not subdir:
        return f"{SQL_BIND_BACKUP_DIR}/{name}"
    if subdir != "archive":
        raise ValueError(f"unsupported backup subdir: {subdir!r}")
    return f"{SQL_BIND_BACKUP_DIR}/archive/{name}"


@contextmanager
def sql_autocommit(
    db: SQLServerConnection,
    *,
    timeout: int,
    lock_timeout_ms: int = -1,
) -> Iterator[None]:
    """BACKUP / CREATE DATABASE cannot run inside a pyodbc transaction."""
    db._ensure_connected()
    conn = db.connection
    if conn is None:
        raise RuntimeError("database is not connected")
    old_ac = conn.autocommit
    old_to = conn.timeout
    conn.autocommit = True
    conn.timeout = int(timeout)
    try:
        cur = conn.cursor()
        cur.execute(f"SET LOCK_TIMEOUT {int(lock_timeout_ms)}")
        cur.close()
        yield
    finally:
        try:
            from config import DATABASE_CONFIG

            restore_ms = int(DATABASE_CONFIG.get("lock_timeout_ms", 30_000))
            cur = conn.cursor()
            cur.execute(f"SET LOCK_TIMEOUT {restore_ms}")
            cur.close()
        except Exception:
            logger.debug("restore LOCK_TIMEOUT failed", exc_info=True)
        conn.autocommit = old_ac
        conn.timeout = old_to


def _consume_result_sets(cur) -> None:
    try:
        while cur.nextset():
            pass
    except Exception:
        logger.debug("nextset() after BACKUP ignored", exc_info=True)
    finally:
        cur.close()


def backup_database(
    db: SQLServerConnection,
    database: str,
    filename: str,
    *,
    timeout: int,
    dest_dir: Optional[Path] = None,
    sql_subdir: str = "",
) -> Path:
    """BACKUP DATABASE to the bind-mount and return the advisor-side Path."""
    database = sql_ident(database)
    disk = sql_backup_disk(filename, subdir=sql_subdir)
    dest_dir = dest_dir if dest_dir is not None else prepare_backup_dirs()
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        dest_dir.chmod(0o777)
    except OSError:
        pass
    target = dest_dir / filename
    if target.exists():
        target.unlink()

    sql = (
        f"BACKUP DATABASE [{database}] TO DISK = N'{disk}' "
        f"WITH INIT, STATS = 10"
    )
    with sql_autocommit(db, timeout=timeout):
        cur = db.execute(sql)
        _consume_result_sets(cur)

    if not target.is_file() or target.stat().st_size < _MIN_BAK_BYTES:
        raise RuntimeError(
            f"BACKUP DATABASE [{database}] finished but {target} is missing "
            "or too small. Bind-mount ./backups on sqlserver "
            f"({SQL_BIND_BACKUP_DIR}) and options_advisor (/app/backups)."
        )
    logger.info("backup wrote %s (%d bytes)", target, target.stat().st_size)
    return target


def _job_timeout(job_name: str, default: int) -> int:
    from config import SCHEDULER_CONFIG

    raw = SCHEDULER_CONFIG.get("job_timeout_seconds", {}).get(job_name, default)
    return max(60, int(raw) - 30)


def run_hot_backup(db: SQLServerConnection) -> Path:
    """Snapshot the live OptionsAdvisorDB. Used by the db_backup job."""
    from config import DATABASE_CONFIG

    db_name = sql_ident(str(DATABASE_CONFIG.get("database") or "OptionsAdvisorDB"))
    stamp = now_ist().strftime("%Y%m%d-%H%M%S")
    filename = bak_filename(f"{db_name}-{stamp}.bak")
    timeout = _job_timeout("db_backup", 1800)
    prepare_backup_dirs()
    return backup_database(db, db_name, filename, timeout=timeout)
