"""
Move aged rows from hot tables into matching *_Archive tables.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime
from typing import List, Optional, Sequence

from config import RETENTION_CONFIG
from database.archive_registry import (
    ARCHIVE_TABLE_SPECS,
    ArchiveTableSpec,
    archive_table_name,
    ordered_specs,
    roots_first_for_export,
)
from database.connection import SQLServerConnection

logger = logging.getLogger(__name__)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ARCHIVE_ONLY_COLS = frozenset({"archived_at", "archive_batch_id"})
_SKIP_COPY_TYPES = frozenset({"timestamp", "rowversion", "xml", "sql_variant", "image", "text", "ntext"})
_LEN_TYPES = frozenset({"varchar", "nvarchar", "char", "nchar", "varbinary", "binary"})
_PREC_SCALE_TYPES = frozenset({"decimal", "numeric"})
_SCALE_TYPES = frozenset({"datetime2", "datetimeoffset", "time"})
_ALLOWED_TYPES = frozenset({
    "int", "bigint", "smallint", "tinyint", "bit",
    "decimal", "numeric", "money", "smallmoney", "float", "real",
    "date", "datetime", "datetime2", "datetimeoffset", "smalldatetime", "time",
    "char", "varchar", "nchar", "nvarchar",
    "binary", "varbinary", "uniqueidentifier", "sysname",
})


def _ident(name: str) -> str:
    if not name or not _IDENT_RE.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


def sql_type_clause(row: dict) -> str:
    """Map a ``sys.columns`` row to a T-SQL type clause (allowlisted types)."""
    raw = str(row.get("type_name") or "").strip().lower()
    if raw == "sysname":
        return "nvarchar(128)"
    if raw not in _ALLOWED_TYPES or raw in _SKIP_COPY_TYPES:
        raise ValueError(f"unsupported column type: {raw!r}")
    if raw in _LEN_TYPES:
        n = int(row.get("max_length") or 0)
        if n < 0:
            return f"{raw}(MAX)"
        if raw in ("nvarchar", "nchar"):
            n = max(1, n // 2)
        return f"{raw}({n})"
    if raw in _PREC_SCALE_TYPES:
        return f"{raw}({int(row['precision'])},{int(row['scale'])})"
    if raw in _SCALE_TYPES:
        return f"{raw}({int(row['scale'])})"
    if raw == "float":
        prec = int(row.get("precision") or 53)
        if prec in (0, 53):
            return "float"
        return f"float({prec})"
    return raw


def _cutoff_for_spec(spec: ArchiveTableSpec, today: date) -> datetime | date:
    from datetime import timedelta

    days = int(RETENTION_CONFIG.get(spec.retention_key, 365))
    d = today - timedelta(days=days)
    if spec.date_type == "datetime":
        return datetime.combine(d, datetime.min.time())
    return d


def ensure_archive_tables(db: SQLServerConnection) -> None:
    """Create *_Archive tables and add columns that older shells are missing.

    ``SELECT * INTO`` copies the hot table only at first create. Later
    ``ALTER TABLE`` on hot (e.g. ``filled_quantity``) is not copied, so we
    add missing hot columns on every call, plus archive-only metadata.
    """
    for spec in ARCHIVE_TABLE_SPECS:
        hot = _ident(spec.hot_table)
        arch = _ident(archive_table_name(hot))
        db.execute(
            f"""
            IF OBJECT_ID(N'{arch}', N'U') IS NULL
            BEGIN
                SELECT * INTO {arch} FROM {hot} WHERE 1 = 0;
            END
            IF COL_LENGTH(N'{arch}', N'archived_at') IS NULL
            BEGIN
                ALTER TABLE {arch} ADD archived_at DATETIME2(0) NOT NULL
                    CONSTRAINT DF_{arch}_archived_at DEFAULT SYSDATETIME();
            END
            IF COL_LENGTH(N'{arch}', N'archive_batch_id') IS NULL
            BEGIN
                ALTER TABLE {arch} ADD archive_batch_id NVARCHAR(40) NULL;
            END
            """
        ).close()
        add_missing_hot_columns(db, hot, arch)


def _pk_match_sql(spec: ArchiveTableSpec, alias_src: str, alias_tgt: str) -> str:
    parts = [f"{alias_tgt}.{c} = {alias_src}.{c}" for c in spec.pk_columns]
    return " AND ".join(parts)


def _table_column_defs(db: SQLServerConnection, table: str) -> List[dict]:
    table = _ident(table)
    rows = db.fetch_all(
        f"""
        SELECT
          c.name AS col_name,
          TYPE_NAME(c.system_type_id) AS type_name,
          c.max_length,
          c.precision,
          c.scale,
          CAST(c.is_nullable AS INT) AS is_nullable,
          CAST(c.is_identity AS INT) AS is_identity,
          CAST(c.is_computed AS INT) AS is_computed
        FROM sys.columns c
        WHERE c.object_id = OBJECT_ID(N'dbo.{table}')
        ORDER BY c.column_id
        """
    )
    if not isinstance(rows, (list, tuple)):
        return []
    return list(rows)


def add_missing_hot_columns(
    db: SQLServerConnection, hot: str, arch: str,
) -> List[str]:
    """ALTER ``arch`` to add hot columns that did not exist at SELECT * INTO.

    New columns are always NULLABLE so existing archive rows stay valid.
    Identity and computed columns are skipped.
    """
    hot = _ident(hot)
    arch = _ident(arch)
    hot_defs = _table_column_defs(db, hot)
    arch_defs = _table_column_defs(db, arch)
    if not hot_defs:
        logger.warning("archive sync: no columns on hot table %s", hot)
        return []
    arch_names = {
        _ident(str(r["col_name"])).lower()
        for r in arch_defs
        if r.get("col_name")
    }
    added: List[str] = []
    for row in hot_defs:
        name = _ident(str(row["col_name"]))
        key = name.lower()
        if key in _ARCHIVE_ONLY_COLS or key in arch_names:
            continue
        if int(row.get("is_identity") or 0):
            logger.warning(
                "archive sync: skip identity column %s.%s missing on %s",
                hot, name, arch,
            )
            continue
        if int(row.get("is_computed") or 0):
            logger.warning(
                "archive sync: skip computed column %s.%s", hot, name,
            )
            continue
        try:
            typ = sql_type_clause(row)
        except ValueError:
            logger.warning(
                "archive sync: skip %s.%s type %r",
                hot, name, row.get("type_name"),
            )
            continue
        db.execute(
            f"ALTER TABLE {arch} ADD {name} {typ} NULL"
        ).close()
        added.append(name)
        logger.info("archive sync: added %s.%s %s NULL", arch, name, typ)
    return added


def _table_column_names(db: SQLServerConnection, table: str) -> List[str]:
    table = _ident(table)
    rows = db.fetch_all(
        f"""
        SELECT c.name AS col_name
        FROM sys.columns c
        WHERE c.object_id = OBJECT_ID(N'dbo.{table}')
        ORDER BY c.column_id
        """
    )
    names = [_ident(str(r["col_name"])) for r in rows]
    if not names:
        raise RuntimeError(f"no columns found for dbo.{table}")
    return names


def _table_has_identity(db: SQLServerConnection, table: str) -> bool:
    table = _ident(table)
    row = db.fetch_one(
        f"""
        SELECT CAST(CASE WHEN EXISTS (
            SELECT 1 FROM sys.columns c
            WHERE c.object_id = OBJECT_ID(N'dbo.{table}') AND c.is_identity = 1
        ) THEN 1 ELSE 0 END AS INT) AS has_identity
        """
    )
    return bool(row and row.get("has_identity"))


def wrap_identity_insert(arch: str, insert_sql: str, *, has_identity: bool) -> str:
    """IDENTITY_INSERT requires a column list; turn it off even if INSERT fails."""
    if not has_identity:
        return insert_sql
    arch = _ident(arch)
    return (
        f"SET IDENTITY_INSERT {arch} ON;\n"
        f"BEGIN TRY\n"
        f"{insert_sql}\n"
        f"END TRY\n"
        f"BEGIN CATCH\n"
        f"SET IDENTITY_INSERT {arch} OFF;\n"
        f"THROW;\n"
        f"END CATCH;\n"
        f"SET IDENTITY_INSERT {arch} OFF;"
    )


def archive_copy_insert_sql(
    *,
    hot: str,
    arch: str,
    hot_columns: Sequence[str],
    where_sql: str,
    has_identity: bool,
    arch_columns: Optional[Sequence[str]] = None,
) -> str:
    """INSERT hot rows into *_Archive with an explicit column list.

    ``SELECT * INTO`` copies IDENTITY, so ``INSERT ... SELECT s.*`` raises
    8101 unless IDENTITY_INSERT is ON and the column list is named.

    Only columns present on **both** tables are copied, so a hot ALTER that
    has not yet been synced onto archive cannot break the INSERT.
    """
    hot = _ident(hot)
    arch = _ident(arch)
    arch_set = {
        _ident(c).lower()
        for c in (arch_columns if arch_columns is not None else hot_columns)
    }
    cols = []
    seen = set()
    for c in hot_columns:
        name = _ident(c)
        key = name.lower()
        if key in _ARCHIVE_ONLY_COLS or key in seen:
            continue
        if key not in arch_set:
            continue
        cols.append(name)
        seen.add(key)
    if not cols:
        raise RuntimeError(f"no copyable columns for {hot} → {arch}")
    insert_cols = ", ".join(cols + ["archived_at", "archive_batch_id"])
    select_cols = ", ".join(f"s.{c}" for c in cols) + ", SYSDATETIME(), ?"
    insert_sql = (
        f"INSERT INTO {arch} ({insert_cols})\n"
        f"SELECT {select_cols}\n"
        f"FROM {hot} s\n"
        f"{where_sql}"
    )
    return wrap_identity_insert(arch, insert_sql, has_identity=has_identity)


def merge_chunk_insert_sql(
    target_db: str,
    staging_db: str,
    table: str,
    pk_columns: Sequence[str],
) -> str:
    """Laptop merge: copy staging archive rows, preserving identity values."""
    target_db = _ident(target_db)
    staging_db = _ident(staging_db)
    table = _ident(table)
    pk = " AND ".join(
        f"t.[{_ident(c)}] = s.[{_ident(c)}]" for c in pk_columns
    )
    tgt = f"[{target_db}].dbo.[{table}]"
    stg = f"[{staging_db}].dbo.[{table}]"
    return f"""
DECLARE @has_ident bit = 0;
IF EXISTS (
  SELECT 1
  FROM [{target_db}].sys.columns c
  INNER JOIN [{target_db}].sys.tables t ON c.object_id = t.object_id
  INNER JOIN [{target_db}].sys.schemas sch ON t.schema_id = sch.schema_id
  WHERE sch.name = N'dbo' AND t.name = N'{table}' AND c.is_identity = 1
) SET @has_ident = 1;

DECLARE @cols nvarchar(max);
SELECT @cols = STUFF((
  SELECT N',' + QUOTENAME(c.name)
  FROM [{staging_db}].sys.columns c
  INNER JOIN [{staging_db}].sys.tables t ON c.object_id = t.object_id
  INNER JOIN [{staging_db}].sys.schemas sch ON t.schema_id = sch.schema_id
  WHERE sch.name = N'dbo' AND t.name = N'{table}'
  ORDER BY c.column_id
  FOR XML PATH(''), TYPE
).value(N'.', N'nvarchar(max)'), 1, 1, N'');

IF @cols IS NULL
  THROW 50001, N'No columns found for {table}', 1;

DECLARE @on nvarchar(max) = N'';
DECLARE @off nvarchar(max) = N'';
IF @has_ident = 1
BEGIN
  SET @on = N'SET IDENTITY_INSERT {tgt} ON; ';
  SET @off = N'SET IDENTITY_INSERT {tgt} OFF; ';
END

DECLARE @sql nvarchar(max) =
  @on
  + N'BEGIN TRY INSERT INTO {tgt} (' + @cols + N')
SELECT s.* FROM {stg} s
WHERE NOT EXISTS (
  SELECT 1 FROM {tgt} t WHERE {pk}
); END TRY BEGIN CATCH ' + @off + N'THROW; END CATCH; '
  + @off;
EXEC sys.sp_executesql @sql;
"""


def _insert_archive_rows(
    db: SQLServerConnection,
    spec: ArchiveTableSpec,
    batch_id: str,
    where_sql: str,
    params: list,
) -> int:
    hot = spec.hot_table
    arch = archive_table_name(hot)
    hot_columns = _table_column_names(db, hot)
    arch_columns = _table_column_names(db, arch)
    has_identity = _table_has_identity(db, arch)
    sql = archive_copy_insert_sql(
        hot=hot,
        arch=arch,
        hot_columns=hot_columns,
        arch_columns=arch_columns,
        where_sql=where_sql,
        has_identity=has_identity,
    )
    ins = db.execute(sql, params)
    n_ins = ins.rowcount or 0
    ins.close()
    return n_ins


def _move_child_by_parent(
    db: SQLServerConnection,
    spec: ArchiveTableSpec,
    batch_id: str,
    parent_hot: str,
    parent_date_col: str,
    parent_cutoff: datetime | date,
    parent_extra: Optional[str],
) -> int:
    arch = archive_table_name(spec.hot_table)
    pk = _pk_match_sql(spec, "s", "t")
    extra = f" AND ({parent_extra})" if parent_extra else ""
    parent_sub = (
        f"SELECT {spec.parent_key} FROM {parent_hot} "
        f"WHERE {parent_date_col} < ?{extra}"
    )
    where_sql = (
        f"WHERE s.{spec.parent_key} IN ({parent_sub})\n"
        f"  AND NOT EXISTS (\n"
        f"            SELECT 1 FROM {arch} t WHERE {pk}\n"
        f"          )"
    )
    n_ins = _insert_archive_rows(
        db, spec, batch_id, where_sql, [batch_id, parent_cutoff],
    )
    de = db.execute(
        f"DELETE FROM {spec.hot_table} WHERE {spec.parent_key} IN ({parent_sub})",
        [parent_cutoff],
    )
    n_del = de.rowcount or 0
    de.close()
    return max(n_ins, n_del)


def move_spec(
    db: SQLServerConnection,
    spec: ArchiveTableSpec,
    batch_id: str,
    today: date,
) -> int:
    if spec.child_of:
        parent = next(s for s in ARCHIVE_TABLE_SPECS if s.hot_table == spec.child_of)
        cutoff = _cutoff_for_spec(parent, today)
        return _move_child_by_parent(
            db, spec, batch_id,
            parent.hot_table, parent.date_column, cutoff, parent.extra_where,
        )

    cutoff = _cutoff_for_spec(spec, today)
    hot = spec.hot_table
    arch = archive_table_name(hot)
    pk = _pk_match_sql(spec, "s", "t")
    extra = f" AND ({spec.extra_where})" if spec.extra_where else ""
    where_sql = (
        f"WHERE s.{spec.date_column} < ?{extra}\n"
        f"          AND NOT EXISTS (SELECT 1 FROM {arch} t WHERE {pk})"
    )
    _insert_archive_rows(db, spec, batch_id, where_sql, [batch_id, cutoff])
    de = db.execute(
        f"DELETE FROM {hot} WHERE {spec.date_column} < ?{extra}",
        [cutoff],
    )
    n_del = de.rowcount or 0
    de.close()
    logger.info("archive %s: moved ~%d rows (cutoff %s)", hot, n_del, cutoff)
    return n_del


def run_weekly_archive(db: SQLServerConnection, today: date) -> int:
    ensure_archive_tables(db)
    batch_id = uuid.uuid4().hex[:12]
    total = 0
    for spec in ordered_specs():
        try:
            total += move_spec(db, spec, batch_id, today)
        except Exception:
            logger.exception("archive move failed for %s", spec.hot_table)
            raise
    return total


def archive_table_row_counts(db: SQLServerConnection) -> dict[str, int]:
    out: dict[str, int] = {}
    for spec in ARCHIVE_TABLE_SPECS:
        arch = archive_table_name(spec.hot_table)
        row = db.fetch_one(f"SELECT COUNT(*) AS n FROM {arch}")
        out[arch] = int(row["n"]) if row else 0
    return out


def truncate_all_archive_tables(db: SQLServerConnection) -> int:
    n = 0
    for spec in roots_first_for_export():
        arch = archive_table_name(spec.hot_table)
        cur = db.execute(f"DELETE FROM {arch}")
        n += cur.rowcount or 0
        cur.close()
    return n
