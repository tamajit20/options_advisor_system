"""Tests for lifecycle/sql_backup.py and archive_export.py."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from lifecycle import sql_backup
from lifecycle.archive_export import (
    _pending_row_counts_match_live,
    acknowledge_export,
    require_hot_backup_after_export,
    run_archive_export,
)


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

    def test_missing_directory_is_noop(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        missing = tmp_path / "archive"
        assert sql_backup.prune_bak_files(missing, keep_names=set()) == 0

    def test_prunes_archive_dir_but_not_hot_latest(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        hot = tmp_path / "OptionsAdvisorDB-latest.bak"
        hot.write_bytes(b"h" * 100)
        arch = tmp_path / "archive"
        arch.mkdir()
        chunk = arch / "week.bak"
        chunk.write_bytes(b"c" * 100)
        n = sql_backup.prune_bak_files(arch, keep_names=set())
        assert n == 1
        assert hot.is_file()
        assert not chunk.is_file()


class TestHotBackupFilename:
    def test_rotating_latest_name(self):
        assert sql_backup.hot_backup_filename("OptionsAdvisorDB") == (
            "OptionsAdvisorDB-latest.bak"
        )


class TestWriteHotBackupMarker:
    def test_writes_completed_at_and_bytes(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        mocker.patch(
            "utils.now_ist",
            return_value=datetime(2026, 9, 11, 15, 38, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
        )
        path = sql_backup.write_hot_backup_marker(
            bak_name="OptionsAdvisorDB-latest.bak", size_bytes=4096,
        )
        data = path.read_text(encoding="utf-8")
        assert "OptionsAdvisorDB-latest.bak" in data
        assert "4096" in data
        assert "2026-09-11T15:38:00+05:30" in data


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
        marker = tmp_path / "archive" / "LAST_HOT_BACKUP.json"
        assert marker.is_file()
        assert "OptionsAdvisorDB-latest.bak" in marker.read_text(encoding="utf-8")

    def test_raises_when_bak_missing(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        mocker.patch.object(sql_backup, "_job_timeout", return_value=120)
        db = MagicMock()
        db.connection = MagicMock(autocommit=False, timeout=60)
        db.execute.return_value = MagicMock(nextset=MagicMock(return_value=False))
        with pytest.raises(RuntimeError, match="missing"):
            sql_backup.run_hot_backup(db)


class TestShrinkTransactionLog:
    def test_skips_when_already_near_target(self):
        db = MagicMock()
        db.fetch_one.return_value = {"file_name": "OptionsAdvisorDB_log", "size_mb": 140}
        n = sql_backup.shrink_transaction_log(db, target_mb=128)
        assert n == 140
        db.execute.assert_not_called()

    def test_skips_when_recovery_is_full(self):
        db = MagicMock()
        db.fetch_one.side_effect = [
            {"file_name": "OptionsAdvisorDB_log", "size_mb": 1480},
            {"m": "FULL"},
        ]
        n = sql_backup.shrink_transaction_log(db, target_mb=128)
        assert n == 1480
        db.execute.assert_not_called()

    def test_checkpoint_and_shrinkfile_when_oversized(self):
        db = MagicMock()
        db.connection = MagicMock(autocommit=False)
        db.fetch_one.side_effect = [
            {"file_name": "OptionsAdvisorDB_log", "size_mb": 1480},
            {"m": "SIMPLE"},
            {"size_mb": 128},
        ]
        cur = MagicMock()
        cur.nextset.return_value = False
        db.execute.return_value = cur

        n = sql_backup.shrink_transaction_log(db, target_mb=128)
        assert n == 128
        sqls = [c.args[0] for c in db.execute.call_args_list]
        assert sqls[0] == "CHECKPOINT"
        assert "DBCC SHRINKFILE (N'OptionsAdvisorDB_log', 128)" in sqls[1]
        assert db.connection.autocommit is False

    def test_quietly_swallows_errors(self):
        db = MagicMock()
        db.fetch_one.side_effect = RuntimeError("odbc")
        assert sql_backup.shrink_transaction_log_quietly(db) == 0

    def test_returns_zero_when_no_log_file(self):
        db = MagicMock()
        db.fetch_one.return_value = None
        assert sql_backup.shrink_transaction_log(db) == 0
        db.execute.assert_not_called()

    def test_skips_exactly_at_slack_boundary(self):
        db = MagicMock()
        db.fetch_one.return_value = {"file_name": "OptionsAdvisorDB_log", "size_mb": 160}
        n = sql_backup.shrink_transaction_log(db, target_mb=128)
        assert n == 160
        db.execute.assert_not_called()

    def test_floors_target_to_64_mb(self):
        db = MagicMock()
        db.connection = MagicMock(autocommit=False)
        db.fetch_one.side_effect = [
            {"file_name": "OptionsAdvisorDB_log", "size_mb": 500},
            {"m": "SIMPLE"},
            {"size_mb": 64},
        ]
        cur = MagicMock()
        cur.nextset.return_value = False
        db.execute.return_value = cur
        n = sql_backup.shrink_transaction_log(db, target_mb=10)
        assert n == 64
        sqls = [c.args[0] for c in db.execute.call_args_list]
        assert "DBCC SHRINKFILE (N'OptionsAdvisorDB_log', 64)" in sqls[1]

    def test_restores_autocommit_when_checkpoint_fails(self):
        db = MagicMock()
        conn = MagicMock(autocommit=False)
        db.connection = conn
        db.fetch_one.side_effect = [
            {"file_name": "OptionsAdvisorDB_log", "size_mb": 1480},
            {"m": "SIMPLE"},
        ]
        db.execute.side_effect = RuntimeError("checkpoint failed")
        with pytest.raises(RuntimeError, match="checkpoint failed"):
            sql_backup.shrink_transaction_log(db, target_mb=128)
        assert conn.autocommit is False

    def test_rejects_unsafe_log_file_name(self):
        db = MagicMock()
        db.fetch_one.return_value = {
            "file_name": "log]; DROP DATABASE x;--",
            "size_mb": 2000,
        }
        with pytest.raises(ValueError, match="unsafe SQL identifier"):
            sql_backup.shrink_transaction_log(db, target_mb=128)

    def test_raises_when_disconnected(self):
        db = MagicMock()
        db.connection = None
        db.fetch_one.side_effect = [
            {"file_name": "OptionsAdvisorDB_log", "size_mb": 1480},
            {"m": "SIMPLE"},
        ]
        with pytest.raises(RuntimeError, match="not connected"):
            sql_backup.shrink_transaction_log(db, target_mb=128)


class TestArchiveExport:
    def test_skips_when_no_rows(self, tmp_path, mocker):
        mocker.patch(
            "lifecycle.archive_export.ensure_archive_tables",
        )
        mocker.patch(
            "lifecycle.archive_export.archive_table_row_counts",
            return_value={"options_fo_eod_Archive": 0},
        )
        mocker.patch(
            "lifecycle.archive_export.host_backup_dir",
            return_value=tmp_path,
        )
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        (tmp_path / "archive").mkdir()
        clear = mocker.patch("lifecycle.archive_export._remove_pending_export_files")
        backup = mocker.patch("lifecycle.archive_export.backup_database")
        n = run_archive_export(MagicMock())
        assert n == 0
        backup.assert_not_called()
        clear.assert_called_once()

    def test_writes_pending_manifest_and_keeps_old_chunks(self, tmp_path, mocker):
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
        assert stale.is_file()
        assert (arch / "OptionsAdvisorDB_ArchiveExport-20260911-153600.bak").is_file()

    def test_empty_archive_leaves_existing_pending(self, tmp_path, mocker):
        mocker.patch("lifecycle.archive_export.ensure_archive_tables")
        mocker.patch(
            "lifecycle.archive_export.archive_table_row_counts",
            return_value={"options_fo_eod_Archive": 0},
        )
        mocker.patch(
            "lifecycle.archive_export.host_backup_dir",
            return_value=tmp_path,
        )
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        arch = tmp_path / "archive"
        arch.mkdir()
        bak = arch / "waiting.bak"
        bak.write_bytes(b"w" * 2048)
        (arch / "PENDING.json").write_text(
            '{"bak_name": "waiting.bak", "exported_at": "2026-09-04T15:36:00+05:30"}',
            encoding="utf-8",
        )
        backup = mocker.patch("lifecycle.archive_export.backup_database")
        n = run_archive_export(MagicMock())
        assert n == 0
        backup.assert_not_called()
        assert bak.is_file()
        assert (arch / "PENDING.json").is_file()

    def test_ack_deletes_hot_then_archive_when_backup_confirmed(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        mocker.patch(
            "lifecycle.archive_export.host_backup_dir",
            return_value=tmp_path,
        )
        arch = tmp_path / "archive"
        arch.mkdir()
        bak = arch / "chunk.bak"
        bak.write_bytes(b"z" * 2048)
        (arch / "PENDING.json").write_text(
            '{"exported_at": "2026-09-11T15:36:00+05:30", "bak_name": "chunk.bak",'
            ' "row_counts": {"options_fo_eod_Archive": 9}}',
            encoding="utf-8",
        )
        (arch / "LAST_HOT_BACKUP.json").write_text(
            '{"completed_at": "2026-09-11T15:38:00+05:30", "bak_name": "OptionsAdvisorDB-latest.bak"}',
            encoding="utf-8",
        )
        mocker.patch(
            "database.archive_repo.archive_table_row_counts",
            return_value={"options_fo_eod_Archive": 9},
        )
        delete_hot = mocker.patch(
            "database.archive_repo.delete_hot_rows_present_in_archive",
            return_value=4,
        )
        truncate = mocker.patch(
            "database.archive_repo.truncate_all_archive_tables",
            return_value=9,
        )
        shrink = mocker.patch(
            "lifecycle.sql_backup.shrink_transaction_log_quietly",
            return_value=128,
        )
        db = MagicMock()
        n = acknowledge_export(db)
        assert n == 4
        delete_hot.assert_called_once_with(db)
        truncate.assert_called_once_with(db)
        shrink.assert_called_once_with(db)
        db.commit.assert_called()
        assert not bak.is_file()
        assert not (arch / "PENDING.json").is_file()
        assert (arch / "LAST_HOT_BACKUP.json").is_file()

    def test_ack_refuses_without_hot_backup(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        mocker.patch(
            "lifecycle.archive_export.host_backup_dir",
            return_value=tmp_path,
        )
        arch = tmp_path / "archive"
        arch.mkdir()
        (arch / "PENDING.json").write_text(
            '{"exported_at": "2026-09-11T15:36:00+05:30", "bak_name": "chunk.bak"}',
            encoding="utf-8",
        )
        mocker.patch(
            "database.archive_repo.archive_table_row_counts",
            return_value={"options_fo_eod_Archive": 3},
        )
        delete_hot = mocker.patch("database.archive_repo.delete_hot_rows_present_in_archive")
        truncate = mocker.patch("database.archive_repo.truncate_all_archive_tables")
        with pytest.raises(RuntimeError, match="hot backup not confirmed"):
            acknowledge_export(MagicMock())
        delete_hot.assert_not_called()
        truncate.assert_not_called()
        assert (arch / "PENDING.json").is_file()

    def test_ack_refuses_when_archive_counts_changed(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        mocker.patch(
            "lifecycle.archive_export.host_backup_dir",
            return_value=tmp_path,
        )
        arch = tmp_path / "archive"
        arch.mkdir()
        (arch / "PENDING.json").write_text(
            '{"exported_at": "2026-09-11T15:36:00+05:30", "bak_name": "chunk.bak",'
            ' "row_counts": {"options_fo_eod_Archive": 9}}',
            encoding="utf-8",
        )
        (arch / "LAST_HOT_BACKUP.json").write_text(
            '{"completed_at": "2026-09-11T15:38:00+05:30", "bak_name": "OptionsAdvisorDB-latest.bak"}',
            encoding="utf-8",
        )
        mocker.patch(
            "database.archive_repo.archive_table_row_counts",
            return_value={"options_fo_eod_Archive": 15},
        )
        delete_hot = mocker.patch("database.archive_repo.delete_hot_rows_present_in_archive")
        truncate = mocker.patch("database.archive_repo.truncate_all_archive_tables")
        with pytest.raises(RuntimeError, match="row counts changed"):
            acknowledge_export(MagicMock())
        delete_hot.assert_not_called()
        truncate.assert_not_called()
        assert (arch / "PENDING.json").is_file()

    def test_ack_refuses_backup_older_than_export(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        mocker.patch(
            "lifecycle.archive_export.host_backup_dir",
            return_value=tmp_path,
        )
        arch = tmp_path / "archive"
        arch.mkdir()
        (arch / "LAST_HOT_BACKUP.json").write_text(
            '{"completed_at": "2026-09-11T15:30:00+05:30", "bak_name": "OptionsAdvisorDB-latest.bak"}',
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError, match="older than this archive export"):
            require_hot_backup_after_export(
                {"exported_at": "2026-09-11T15:36:00+05:30"},
            )

    def test_ack_does_not_delete_hot_latest_bak(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        mocker.patch(
            "lifecycle.archive_export.host_backup_dir",
            return_value=tmp_path,
        )
        hot = tmp_path / "OptionsAdvisorDB-latest.bak"
        hot.write_bytes(b"h" * 2048)
        arch = tmp_path / "archive"
        arch.mkdir()
        bak = arch / "chunk.bak"
        bak.write_bytes(b"z" * 2048)
        (arch / "PENDING.json").write_text(
            '{"exported_at": "2026-09-11T15:36:00+05:30", "bak_name": "chunk.bak",'
            ' "row_counts": {"options_fo_eod_Archive": 9}}',
            encoding="utf-8",
        )
        (arch / "LAST_HOT_BACKUP.json").write_text(
            '{"completed_at": "2026-09-11T15:38:00+05:30", "bak_name": "OptionsAdvisorDB-latest.bak"}',
            encoding="utf-8",
        )
        mocker.patch(
            "database.archive_repo.archive_table_row_counts",
            return_value={"options_fo_eod_Archive": 9},
        )
        mocker.patch(
            "database.archive_repo.delete_hot_rows_present_in_archive",
            return_value=4,
        )
        mocker.patch(
            "database.archive_repo.truncate_all_archive_tables",
            return_value=9,
        )
        mocker.patch(
            "lifecycle.sql_backup.shrink_transaction_log_quietly",
            return_value=128,
        )
        acknowledge_export(MagicMock())
        assert hot.is_file()
        assert not bak.is_file()

    def test_ack_noop_clears_stale_export_when_nothing_pending(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        mocker.patch(
            "lifecycle.archive_export.host_backup_dir",
            return_value=tmp_path,
        )
        arch = tmp_path / "archive"
        arch.mkdir()
        leftover = arch / "stale-chunk.bak"
        leftover.write_bytes(b"s" * 2048)
        mocker.patch(
            "database.archive_repo.archive_table_row_counts",
            return_value={"options_fo_eod_Archive": 0},
        )
        delete_hot = mocker.patch("database.archive_repo.delete_hot_rows_present_in_archive")
        truncate = mocker.patch("database.archive_repo.truncate_all_archive_tables")
        n = acknowledge_export(MagicMock())
        assert n == 0
        delete_hot.assert_not_called()
        truncate.assert_not_called()
        assert not leftover.is_file()

    def test_ack_refuses_when_archive_rows_exist_without_pending(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        mocker.patch(
            "lifecycle.archive_export.host_backup_dir",
            return_value=tmp_path,
        )
        (tmp_path / "archive").mkdir()
        mocker.patch(
            "database.archive_repo.archive_table_row_counts",
            return_value={"options_fo_eod_Archive": 3},
        )
        delete_hot = mocker.patch("database.archive_repo.delete_hot_rows_present_in_archive")
        with pytest.raises(RuntimeError, match="PENDING.json is missing"):
            acknowledge_export(MagicMock())
        delete_hot.assert_not_called()

    def test_ack_refuses_invalid_pending_json(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        mocker.patch(
            "lifecycle.archive_export.host_backup_dir",
            return_value=tmp_path,
        )
        arch = tmp_path / "archive"
        arch.mkdir()
        (arch / "PENDING.json").write_text("{not-json", encoding="utf-8")
        mocker.patch(
            "database.archive_repo.archive_table_row_counts",
            return_value={"options_fo_eod_Archive": 1},
        )
        with pytest.raises(RuntimeError, match="invalid PENDING.json"):
            acknowledge_export(MagicMock())

    def test_ack_refuses_pending_list(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        mocker.patch(
            "lifecycle.archive_export.host_backup_dir",
            return_value=tmp_path,
        )
        arch = tmp_path / "archive"
        arch.mkdir()
        (arch / "PENDING.json").write_text("[]", encoding="utf-8")
        mocker.patch(
            "database.archive_repo.archive_table_row_counts",
            return_value={"options_fo_eod_Archive": 1},
        )
        with pytest.raises(RuntimeError, match="JSON object"):
            acknowledge_export(MagicMock())

    def test_ack_refuses_when_table_set_differs(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        mocker.patch(
            "lifecycle.archive_export.host_backup_dir",
            return_value=tmp_path,
        )
        arch = tmp_path / "archive"
        arch.mkdir()
        (arch / "PENDING.json").write_text(
            '{"exported_at": "2026-09-11T15:36:00+05:30", "bak_name": "chunk.bak",'
            ' "row_counts": {"options_fo_eod_Archive": 9}}',
            encoding="utf-8",
        )
        (arch / "LAST_HOT_BACKUP.json").write_text(
            '{"completed_at": "2026-09-11T15:38:00+05:30", "bak_name": "OptionsAdvisorDB-latest.bak"}',
            encoding="utf-8",
        )
        mocker.patch(
            "database.archive_repo.archive_table_row_counts",
            return_value={
                "options_fo_eod_Archive": 9,
                "options_trades_Archive": 0,
            },
        )
        delete_hot = mocker.patch("database.archive_repo.delete_hot_rows_present_in_archive")
        with pytest.raises(RuntimeError, match="row counts changed"):
            acknowledge_export(MagicMock())
        delete_hot.assert_not_called()

    def test_skips_when_disabled(self, mocker):
        mocker.patch.dict(
            "config.ARCHIVE_EXPORT_CONFIG", {"enabled": False}, clear=False,
        )
        backup = mocker.patch("lifecycle.archive_export.backup_database")
        n = run_archive_export(MagicMock())
        assert n == 0
        backup.assert_not_called()


class TestPendingRowCountsMatch:
    def test_equal_counts_match(self):
        live = {"options_fo_eod_Archive": 9, "options_trades_Archive": 0}
        pending = {"row_counts": {"options_fo_eod_Archive": 9, "options_trades_Archive": 0}}
        assert _pending_row_counts_match_live(pending, live) is True

    def test_string_digits_still_match(self):
        live = {"options_fo_eod_Archive": 9}
        pending = {"row_counts": {"options_fo_eod_Archive": "9"}}
        assert _pending_row_counts_match_live(pending, live) is True

    def test_missing_row_counts_do_not_match(self):
        assert _pending_row_counts_match_live({}, {"options_fo_eod_Archive": 1}) is False

    def test_garbage_count_does_not_match(self):
        pending = {"row_counts": {"options_fo_eod_Archive": "nine"}}
        live = {"options_fo_eod_Archive": 9}
        assert _pending_row_counts_match_live(pending, live) is False


class TestRequireHotBackupAfterExport:
    def test_refuses_missing_exported_at(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        (tmp_path / "archive").mkdir()
        with pytest.raises(RuntimeError, match="missing exported_at"):
            require_hot_backup_after_export({})

    def test_refuses_missing_completed_at(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        arch = tmp_path / "archive"
        arch.mkdir()
        (arch / "LAST_HOT_BACKUP.json").write_text(
            '{"bak_name": "OptionsAdvisorDB-latest.bak"}',
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError, match="missing completed_at"):
            require_hot_backup_after_export(
                {"exported_at": "2026-09-11T15:36:00+05:30"},
            )

    def test_refuses_invalid_marker_json(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        arch = tmp_path / "archive"
        arch.mkdir()
        (arch / "LAST_HOT_BACKUP.json").write_text("{nope", encoding="utf-8")
        with pytest.raises(RuntimeError, match="invalid LAST_HOT_BACKUP"):
            require_hot_backup_after_export(
                {"exported_at": "2026-09-11T15:36:00+05:30"},
            )

    def test_accepts_utc_z_timestamp_after_export(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        arch = tmp_path / "archive"
        arch.mkdir()
        # 10:08Z == 15:38 IST, after 15:36 IST export
        (arch / "LAST_HOT_BACKUP.json").write_text(
            '{"completed_at": "2026-09-11T10:08:00Z", "bak_name": "OptionsAdvisorDB-latest.bak"}',
            encoding="utf-8",
        )
        require_hot_backup_after_export(
            {"exported_at": "2026-09-11T15:36:00+05:30"},
        )

    def test_accepts_equal_timestamps(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        arch = tmp_path / "archive"
        arch.mkdir()
        stamp = "2026-09-11T15:36:00+05:30"
        (arch / "LAST_HOT_BACKUP.json").write_text(
            '{"completed_at": "%s", "bak_name": "OptionsAdvisorDB-latest.bak"}' % stamp,
            encoding="utf-8",
        )
        require_hot_backup_after_export({"exported_at": stamp})

    def test_compares_naive_export_to_aware_backup(self, tmp_path, mocker):
        mocker.patch.object(sql_backup, "host_backup_dir", return_value=tmp_path)
        arch = tmp_path / "archive"
        arch.mkdir()
        (arch / "LAST_HOT_BACKUP.json").write_text(
            '{"completed_at": "2026-09-11T15:38:00+05:30", "bak_name": "OptionsAdvisorDB-latest.bak"}',
            encoding="utf-8",
        )
        require_hot_backup_after_export({"exported_at": "2026-09-11T15:36:00"})
