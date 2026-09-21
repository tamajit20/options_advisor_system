#!/usr/bin/env python3
"""Merge a weekly archive .bak chunk into cumulative local OptionsAdvisorDB_Archive.

Does not touch VM hot tables. Hot delete happens later on VM ACK after this merge
and a confirmed db_backup.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database.archive_registry import ARCHIVE_TABLE_SPECS, archive_table_name
from database.archive_repo import merge_chunk_insert_sql


def _sqlcmd(server: str, query: str, *, db: str | None = None) -> subprocess.CompletedProcess:
    # -I: QUOTED_IDENTIFIER ON (required for filtered indexes / some archive tables)
    args = ["sqlcmd", "-S", server, "-E", "-b", "-I", "-s", "|", "-W"]
    if db:
        args.extend(["-d", db])
    args.extend(["-Q", query])
    return subprocess.run(args, capture_output=True, text=True)


def _run_step(server: str, label: str, query: str, *, db: str | None = None) -> None:
    r = _sqlcmd(server, query, db=db)
    if r.returncode != 0:
        raise RuntimeError(f"{label} failed:\n{r.stderr or r.stdout}")
    if r.stdout.strip():
        print(r.stdout.strip())


def _sql_literal(path: Path | str) -> str:
    return str(path).replace("'", "''")


def _parse_filelistonly(stdout: str) -> list[tuple[str, str, str]]:
    """Return [(logical_name, physical_name, type)] from RESTORE FILELISTONLY."""
    rows: list[tuple[str, str, str]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("LogicalName") or line.startswith("-"):
            continue
        parts = [p.strip() for p in line.split("|") if p.strip()]
        if len(parts) >= 3:
            rows.append((parts[0], parts[1], parts[2].upper()))
    return rows


def _first_sqlcmd_value(stdout: str) -> str:
    for line in stdout.splitlines():
        line = line.strip()
        if not line or set(line) <= {"-", "|", " "}:
            continue
        if line.lower().startswith("----") or "rows affected" in line.lower():
            continue
        # Prefer first column when pipe-separated.
        return line.split("|", 1)[0].strip()
    return ""


def local_sql_data_dirs(server: str) -> tuple[Path, Path]:
    """Windows (or local) data/log dirs — never reuse Linux paths from a VM .bak."""
    r = _sqlcmd(
        server,
        "SET NOCOUNT ON; "
        "SELECT "
        "  ISNULL(CAST(SERVERPROPERTY('InstanceDefaultDataPath') AS nvarchar(512)), N''), "
        "  ISNULL(CAST(SERVERPROPERTY('InstanceDefaultLogPath') AS nvarchar(512)), N'');",
    )
    if r.returncode != 0:
        raise RuntimeError(f"default data path query failed:\n{r.stderr or r.stdout}")

    data_s = log_s = ""
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line or set(line) <= {"-", "|", " "} or line.lower().startswith("----"):
            continue
        if "rows affected" in line.lower():
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 2:
            data_s, log_s = parts[0], parts[1]
            break
        if parts and parts[0]:
            data_s = parts[0]

    if not data_s:
        # Fall back to master.mdf directory on this instance.
        r2 = _sqlcmd(
            server,
            "SET NOCOUNT ON; "
            "SELECT TOP 1 physical_name FROM sys.master_files "
            "WHERE database_id = 1 AND type = 0;",
        )
        if r2.returncode != 0:
            raise RuntimeError(f"master data path query failed:\n{r2.stderr or r2.stdout}")
        data_s = _first_sqlcmd_value(r2.stdout)

    if not data_s:
        raise RuntimeError(
            "Could not resolve a local SQL Server data directory for RESTORE WITH MOVE"
        )

    data_dir = Path(data_s)
    log_dir = Path(log_s) if log_s else data_dir
    # Guard: never write into a Linux container path that only exists inside the VM bak.
    for label, p in (("data", data_dir), ("log", log_dir)):
        posixish = str(p).replace("\\", "/").lower()
        if "/var/opt/mssql/" in posixish or posixish.startswith("/var/"):
            raise RuntimeError(
                f"Refusing to restore into non-local {label} path {p} "
                "(looks like a Docker/Linux path from the VM .bak)"
            )
    return data_dir, log_dir


def build_restore_move_clauses(
    filelist: list[tuple[str, str, str]],
    staging_db: str,
    data_dir: Path,
    log_dir: Path,
) -> list[str]:
    """Map each logical file to a fresh local mdf/ldf under the instance dirs."""
    move_sql: list[str] = []
    for idx, (logical, _phys, typ) in enumerate(filelist):
        is_log = typ.startswith("L")
        ext = "ldf" if is_log else "mdf"
        dest_dir = log_dir if is_log else data_dir
        dest = dest_dir / f"{staging_db}_{idx}.{ext}"
        move_sql.append(f"MOVE N'{_sql_literal(logical)}' TO N'{_sql_literal(dest)}'")
    return move_sql


def restore_staging(server: str, staging_db: str, bak_path: str) -> None:
    bak = Path(bak_path).resolve()
    if not bak.is_file():
        raise FileNotFoundError(bak)
    bak_sql = _sql_literal(bak)

    _run_step(
        server,
        "kill staging connections",
        f"""
        IF DB_ID(N'{staging_db}') IS NOT NULL
        BEGIN
          ALTER DATABASE [{staging_db}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
          DROP DATABASE [{staging_db}];
        END
        """,
    )

    fl = _sqlcmd(
        server,
        f"SET NOCOUNT ON; RESTORE FILELISTONLY FROM DISK = N'{bak_sql}';",
    )
    if fl.returncode != 0:
        raise RuntimeError(f"FILELISTONLY failed:\n{fl.stderr or fl.stdout}")

    rows = _parse_filelistonly(fl.stdout)
    if not rows:
        raise RuntimeError(f"Could not parse FILELISTONLY for {bak}")

    data_dir, log_dir = local_sql_data_dirs(server)
    move_sql = build_restore_move_clauses(rows, staging_db, data_dir, log_dir)
    print(f"  RESTORE WITH MOVE -> data={data_dir} log={log_dir}")

    _run_step(
        server,
        "restore staging",
        f"""
        RESTORE DATABASE [{staging_db}] FROM DISK = N'{bak_sql}'
        WITH {", ".join(move_sql)}, REPLACE, RECOVERY, STATS = 10;
        """,
    )


def ensure_target_db(server: str, target_db: str) -> None:
    _run_step(
        server,
        "ensure target db",
        f"""
        IF DB_ID(N'{target_db}') IS NULL
          CREATE DATABASE [{target_db}];
        """,
    )


def merge_table(server: str, staging_db: str, target_db: str, table: str, pk_cols: tuple[str, ...]) -> None:
    exists = _sqlcmd(
        server,
        f"SET NOCOUNT ON; SELECT CASE WHEN OBJECT_ID(N'{target_db}.dbo.{table}', N'U') IS NULL THEN 0 ELSE 1 END",
    )
    if exists.returncode != 0:
        raise RuntimeError(exists.stderr)
    target_has = exists.stdout.strip().splitlines()[-1].strip() == "1"

    staging_count = _sqlcmd(
        server,
        f"SET NOCOUNT ON; SELECT COUNT(*) FROM [{staging_db}].dbo.[{table}]",
    )
    if staging_count.returncode != 0:
        return
    n = int(staging_count.stdout.strip().splitlines()[-1].strip() or "0")
    if n == 0:
        print(f"  skip {table} (empty chunk)")
        return

    if not target_has:
        _run_step(
            server,
            f"bootstrap {table}",
            f"SELECT * INTO [{target_db}].dbo.[{table}] FROM [{staging_db}].dbo.[{table}] WHERE 1 = 0;",
        )

    _run_step(
        server,
        f"merge {table}",
        merge_chunk_insert_sql(target_db, staging_db, table, pk_cols),
    )
    print(f"  merged {table} (+up to {n} rows)")


def drop_staging(server: str, staging_db: str) -> None:
    _run_step(
        server,
        "drop staging",
        f"""
        IF DB_ID(N'{staging_db}') IS NOT NULL
        BEGIN
          ALTER DATABASE [{staging_db}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
          DROP DATABASE [{staging_db}];
        END
        """,
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bak", required=True, help="Path to archive chunk .bak")
    p.add_argument("--server", default=r"localhost\SQLEXPRESS")
    p.add_argument("--target-db", default="OptionsAdvisorDB_Archive")
    p.add_argument("--staging-db", default="OptionsAdvisorDB_Archive_Staging")
    args = p.parse_args()

    print(f"==> Restore staging from {args.bak}")
    restore_staging(args.server, args.staging_db, args.bak)
    ensure_target_db(args.server, args.target_db)

    print("==> Merge archive tables")
    for spec in ARCHIVE_TABLE_SPECS:
        table = archive_table_name(spec.hot_table)
        merge_table(args.server, args.staging_db, args.target_db, table, tuple(spec.pk_columns))

    drop_staging(args.server, args.staging_db)
    print("==> Merge complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
