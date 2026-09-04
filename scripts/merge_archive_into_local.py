#!/usr/bin/env python3
"""Merge a weekly archive .bak chunk into cumulative local OptionsAdvisorDB_Archive."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database.archive_registry import ARCHIVE_TABLE_SPECS, archive_table_name


def _sqlcmd(server: str, query: str, *, db: str | None = None) -> subprocess.CompletedProcess:
    args = ["sqlcmd", "-S", server, "-E", "-b", "-s", "|", "-W"]
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


def restore_staging(server: str, staging_db: str, bak_path: str) -> None:
    bak = Path(bak_path).resolve()
    if not bak.is_file():
        raise FileNotFoundError(bak)
    bak_sql = str(bak).replace("'", "''")

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

    rows: list[tuple[str, str, str]] = []
    for line in fl.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("LogicalName") or line.startswith("-"):
            continue
        parts = [p.strip() for p in line.split("|") if p.strip()]
        if len(parts) >= 3:
            rows.append((parts[0], parts[1], parts[2].upper()))

    if not rows:
        raise RuntimeError(f"Could not parse FILELISTONLY for {bak}")

    data_dir = Path(rows[0][1]).parent
    move_sql = []
    for idx, (logical, _phys, typ) in enumerate(rows):
        ext = "mdf" if typ == "D" or idx == 0 else "ldf"
        dest = data_dir / f"{staging_db}_{idx}.{ext}"
        move_sql.append(f"MOVE N'{logical}' TO N'{dest}'")

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

    pk = " AND ".join(f"t.[{c}] = s.[{c}]" for c in pk_cols)
    _run_step(
        server,
        f"merge {table}",
        f"""
        INSERT INTO [{target_db}].dbo.[{table}]
        SELECT s.* FROM [{staging_db}].dbo.[{table}] s
        WHERE NOT EXISTS (
          SELECT 1 FROM [{target_db}].dbo.[{table}] t WHERE {pk}
        );
        """,
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
