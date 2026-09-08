"""
SQL Server BACKUP DATABASE onto the host ./backups bind-mount.

The scheduler runs inside options_advisor, which has no docker CLI, so
deploy/backup.sh (docker compose exec / docker cp) cannot work there.
sqlserver writes the .bak to /var/opt/mssql/host-backups, which compose
bind-mounts to ./backups on the VM.
"""

from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from database.connection import SQLServerConnection

logger = logging.getLogger(__name__)

# Path inside the sqlserver container (docker-compose bind-mount).
SQL_BIND_BACKUP_DIR = "/var/opt/mssql/host-backups"

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BAK_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.bak$")
_MIN_BAK_BYTES = 1024
HOT_BACKUP_MARKER_NAME = "LAST_HOT_BACKUP.json"


def sql_ident(name: str) -> str:
    """Allow only simple unquoted identifiers (database / table names)."""
    if not name or not _IDENT_RE.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


def bak_filename(name: str) -> str:
    if not name or not _BAK_NAME_RE.match(name):
        raise ValueError(f"unsafe backup filename: {name!r}")
    return name


def hot_backup_filename(database: str) -> str:
    """Single rotating hot-backup name so Friday jobs do not stack .bak files."""
    return bak_filename(f"{sql_ident(database)}-latest.bak")


def prune_bak_files(directory: Path, *, keep_names: set[str] | frozenset[str]) -> int:
    """Delete ``*.bak`` in ``directory`` except ``keep_names`` (no recursion).

    Only ``./backups`` and ``./backups/archive`` are allowed.
    """
    directory = directory.resolve()
    root = host_backup_dir().resolve()
    archive = (root / "archive").resolve()
    if directory not in (root, archive):
        raise ValueError(f"refuse to prune backups outside {root}: {directory}")
    keep = {bak_filename(n) for n in keep_names}
    removed = 0
    if not directory.is_dir():
        return 0
    for path in directory.glob("*.bak"):
        if path.name in keep:
            continue
        try:
            path.unlink()
            removed += 1
            logger.info("removed leftover backup %s", path)
        except OSError:
            logger.warning("could not remove leftover backup %s", path, exc_info=True)
    return removed


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def host_backup_dir() -> Path:
    """Advisor-side path for ./backups (bind-mounted in Docker)."""
    return repo_root() / "backups"


def hot_backup_marker_path() -> Path:
    return host_backup_dir() / "archive" / HOT_BACKUP_MARKER_NAME


def write_hot_backup_marker(*, bak_name: str, size_bytes: int) -> Path:
    """Record a successful db_backup so ACK can refuse to delete hot rows without it."""
    from utils import now_ist

    path = hot_backup_marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "bak_name": bak_filename(bak_name),
        "completed_at": now_ist().isoformat(),
        "bytes": int(size_bytes),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("hot backup confirmed: %s", path)
    return path


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
    size = target.stat().st_size
    logger.info("backup wrote %s (%d bytes)", target, size)
    return target


def _job_timeout(job_name: str, default: int) -> int:
    from config import SCHEDULER_CONFIG

    raw = SCHEDULER_CONFIG.get("job_timeout_seconds", {}).get(job_name, default)
    return max(60, int(raw) - 30)


def run_hot_backup(db: SQLServerConnection) -> Path:
    """Snapshot the live OptionsAdvisorDB. Used by the db_backup job.

    Always writes ``<db>-latest.bak`` (replaces last week's file) and deletes
    any other ``*.bak`` in ``./backups`` (not ``./backups/archive``).
    """
    from config import DATABASE_CONFIG

    db_name = sql_ident(str(DATABASE_CONFIG.get("database") or "OptionsAdvisorDB"))
    filename = hot_backup_filename(db_name)
    timeout = _job_timeout("db_backup", 1800)
    dest = prepare_backup_dirs()
    path = backup_database(db, db_name, filename, timeout=timeout)
    prune_bak_files(dest, keep_names={filename})
    write_hot_backup_marker(bak_name=filename, size_bytes=path.stat().st_size)
    return path
