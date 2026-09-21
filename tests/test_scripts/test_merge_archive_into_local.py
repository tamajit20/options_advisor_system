"""Unit tests for archive merge restore path mapping (no SQL Server required)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.merge_archive_into_local import (
    _parse_filelistonly,
    build_restore_move_clauses,
)


def test_parse_filelistonly_pipe_rows():
    stdout = """
LogicalName|PhysicalName|Type|FileGroupName
-----------|------------|----|-------------
OptionsAdvisorDB_ArchiveExport|\\var\\opt\\mssql\\data\\x.mdf|D|PRIMARY
OptionsAdvisorDB_ArchiveExport_log|\\var\\opt\\mssql\\data\\x.ldf|L|NULL
(2 rows affected)
"""
    rows = _parse_filelistonly(stdout)
    assert len(rows) == 2
    assert rows[0][0] == "OptionsAdvisorDB_ArchiveExport"
    assert rows[0][2] == "D"
    assert rows[1][2] == "L"


def test_build_restore_move_uses_local_dirs_not_bak_paths():
    filelist = [
        ("OptionsAdvisorDB_ArchiveExport", r"\var\opt\mssql\data\x.mdf", "D"),
        ("OptionsAdvisorDB_ArchiveExport_log", r"\var\opt\mssql\data\x.ldf", "L"),
    ]
    data = Path(r"C:\SQLData")
    log = Path(r"C:\SQLLog")
    moves = build_restore_move_clauses(
        filelist, "OptionsAdvisorDB_Archive_Staging", data, log,
    )
    assert len(moves) == 2
    assert r"C:\SQLData\OptionsAdvisorDB_Archive_Staging_0.mdf" in moves[0]
    assert r"C:\SQLLog\OptionsAdvisorDB_Archive_Staging_1.ldf" in moves[1]
    assert "var" not in moves[0].lower()
    assert "var" not in moves[1].lower()


def test_build_restore_move_rejects_nothing_when_dirs_local():
    # Sanity: single data file typed D goes to data_dir.
    moves = build_restore_move_clauses(
        [("db", "/var/opt/mssql/data/x.mdf", "D")],
        "Staging",
        Path(r"D:\MSSQL\DATA"),
        Path(r"D:\MSSQL\DATA"),
    )
    assert moves[0].endswith(r"Staging_0.mdf'")
