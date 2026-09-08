"""Tests for lifecycle/sql_backup.py and archive_export.py."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from lifecycle import sql_backup
from lifecycle.archive_export import acknowledge_export, run_archive_export


class TestSqlIdent:
    def test_accepts_simple_names(self):
        assert sql_backup.sql_ident("OptionsAdvisorDB") == "OptionsAdvisorDB"
        assert sql_backup.sql_ident("options_fo_eod_Archive") == "options_fo_eod_Archive"

    def test_rejects_injection(self):
        with pytest.raises(ValueError):
            sql_backup.sql_ident("db]; DROP DATABASE x;--")
        with pytest.raises(ValueError):
            sql_backup.bak_filename("foo.bak; rm -rf /")


class TestSqlBackupDisk:
    def test_hot_and_archive_paths(self):
        assert sql_backup.sql_backup_disk("OptionsAdvisorDB-1.bak") == (
            "/var/opt/mssql/host-backups/OptionsAdvisorDB-1.bak"
        )
        assert sql_backup.sql_backup_disk("exp.bak", subdir="archive") == (
            "/var/opt/mssql/host-backups/archive/exp.bak"
        )

    def test_rejects_unknown_subdir(self):
        with pytest.raises(ValueError):
            sql_backup.sql_backup_disk("x.bak", subdir="tmp")


class TestPruneBakFiles:
    def test_removes_others_keeps_named(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        keep = tmp_path / "OptionsAdvisorDB-latest.bak"
        old = tmp_path / "OptionsAdvisorDB-20260101-000000.bak"
        keep.write_bytes(b"k" * 100)
        old.write_bytes(b"o" * 100)
        (tmp_path / "archive").mkdir()
        archived = tmp_path / "archive" / "chunk.bak"
        archived.write_bytes(b"a" * 100)
        n = sql_backup.prune_bak_files(
            tmp_path, keep_names={"OptionsAdvisorDB-latest.bak"},
        )
        assert n == 1
        assert keep.is_file()
        assert not old.is_file()
        assert archived.is_file()

    def test_refuses_unrelated_directory(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        other = tmp_path / "other"
        other.mkdir()
        with pytest.raises(ValueError, match="refuse"):
            sql_backup.prune_bak_files(other, keep_names=set())


class TestRunHotBackup:
    def test_issues_backup_and_requires_file(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        mocker.patch.object(sql_backup, "_job_timeout", return_value=120)

        db = MagicMock()
        conn = MagicMock()
        conn.autocommit = False
        conn.timeout = 60
        db.connection = conn

        stale = tmp_path / "OptionsAdvisorDB-20260901-000000.bak"
        stale.write_bytes(b"old" * 100)

        def _execute(sql, params=None):
            dest = tmp_path / "OptionsAdvisorDB-latest.bak"
            dest.write_bytes(b"x" * 2048)
            cur = MagicMock()
            cur.nextset.return_value = False
            return cur

        db.execute.side_effect = _execute

        path = sql_backup.run_hot_backup(db)
        assert path.name == "OptionsAdvisorDB-latest.bak"
        sql = db.execute.call_args[0][0]
        assert "BACKUP DATABASE [OptionsAdvisorDB]" in sql
        assert "/var/opt/mssql/host-backups/OptionsAdvisorDB-latest.bak" in sql
        assert conn.autocommit is False
        assert conn.timeout == 60
        assert not stale.is_file()

    def test_raises_when_bak_missing(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        mocker.patch.object(sql_backup, "_job_timeout", return_value=120)
        db = MagicMock()
        db.connection = MagicMock(autocommit=False, timeout=60)
        db.execute.return_value = MagicMock(nextset=MagicMock(return_value=False))
        with pytest.raises(RuntimeError, match="missing"):
            sql_backup.run_hot_backup(db)


class TestArchiveExport:
    def test_skips_when_no_rows(self, mocker):
        mocker.patch(
            "lifecycle.archive_export.ensure_archive_tables",
        )
        mocker.patch(
            "lifecycle.archive_export.archive_table_row_counts",
            return_value={"options_fo_eod_Archive": 0},
        )
        clear = mocker.patch("lifecycle.archive_export._remove_pending_export_files")
        backup = mocker.patch("lifecycle.archive_export.backup_database")
        n = run_archive_export(MagicMock())
        assert n == 0
        backup.assert_not_called()
        clear.assert_called_once()

    def test_writes_pending_manifest_and_prunes_old_chunks(self, tmp_path, mocker):
        mocker.patch("lifecycle.archive_export.ensure_archive_tables")
        mocker.patch(
            "lifecycle.archive_export.archive_table_row_counts",
            return_value={"options_fo_eod_Archive": 12},
        )
        mocker.patch("lifecycle.archive_export._build_export_database")
        mocker.patch(
            "lifecycle.archive_export.host_backup_dir",
            return_value=tmp_path,
        )
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        mocker.patch(
            "lifecycle.archive_export.now_ist",
            return_value=datetime(2026, 9, 11, 15, 36, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
        )
        mocker.patch("lifecycle.archive_export.prepare_backup_dirs", return_value=tmp_path)
        arch = tmp_path / "archive"
        arch.mkdir()
        stale = arch / "old-chunk.bak"
        stale.write_bytes(b"y" * 2048)

        def _backup(db, database, filename, *, timeout, dest_dir, sql_subdir=""):
            assert database == "OptionsAdvisorDB_ArchiveExport"
            assert sql_subdir == "archive"
            (dest_dir / filename).write_bytes(b"x" * 2048)
            return dest_dir / filename

        mocker.patch("lifecycle.archive_export.backup_database", side_effect=_backup)

        n = run_archive_export(MagicMock())
        assert n == 1
        manifest = tmp_path / "archive" / "PENDING.json"
        text = manifest.read_text(encoding="utf-8")
        assert "OptionsAdvisorDB_ArchiveExport-20260911-153600.bak" in text
        assert '"total_rows": 12' in text
        assert not stale.is_file()
        assert (arch / "OptionsAdvisorDB_ArchiveExport-20260911-153600.bak").is_file()

    def test_ack_deletes_bak_and_manifest(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        mocker.patch(
            "lifecycle.archive_export.host_backup_dir",
            return_value=tmp_path,
        )
        arch = tmp_path / "archive"
        arch.mkdir()
        bak = arch / "chunk.bak"
        bak.write_bytes(b"z" * 2048)
        (arch / "PENDING.json").write_text("{}", encoding="utf-8")
        mocker.patch(
            "database.archive_repo.truncate_all_archive_tables",
            return_value=9,
        )
        n = acknowledge_export(MagicMock())
        assert n == 9
        assert not bak.is_file()
        assert not (arch / "PENDING.json").is_file()
