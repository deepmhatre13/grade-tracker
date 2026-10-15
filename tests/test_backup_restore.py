"""Tests for verified backup and restore of grade data (GitHub Issue #34).

Covers:
1. Successful backup creation.
2. Correct SHA-256 checksum and metadata generation.
3. Unique backup names for repeated operations.
4. Backup behavior when the source file is missing or invalid.
5. Successful restoration of a valid backup.
6. Checksum mismatch rejection.
7. Missing or malformed manifest rejection.
8. Unsupported backup-format or schema-version rejection.
9. Invalid CSV header or records rejection.
10. Destination unchanged after every verification or validation failure.
11. Atomic replacement and temporary-file cleanup.
12. Restoration when the destination does not exist.
13. Backup consistency with concurrent writers.
14. Restore serialization with concurrent writers.
15. Filesystem errors during backup and restore.
16. Paths containing spaces and platform-specific path handling.
"""

import contextlib
import csv
import io
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from grade_tracker.cli import (
    DEFAULT_DATA_PATH,
    _InterProcessLock,
    _lock_path_for,
    compute_sha256,
    create_backup,
    main,
    restore_backup,
    save_grade,
)

HEADER = "student_id,subject,score,max_score,date,credits,semester\n"

# Concurrency worker functions defined at top-level for spawn multiprocessing
def _worker_writer_add(csv_path, student_id, subject, result_queue, barrier):
    """Wait for barrier then perform save_grade under lock."""
    try:
        barrier.wait(timeout=10.0)
        save_grade(
            student_id=student_id,
            subject=subject,
            score=90.0,
            max_score=100.0,
            date="2026-10-15",
            credits=3.0,
            semester="1",
            csv_path=str(csv_path),
        )
        result_queue.put("OK")
    except Exception as exc:
        result_queue.put(f"ERROR: {exc!r}")


def _worker_backup(csv_path, backup_dir, result_queue, barrier):
    """Wait for barrier then perform create_backup under lock."""
    try:
        barrier.wait(timeout=10.0)
        res = create_backup(csv_path=csv_path, backup_dir=backup_dir)
        result_queue.put(("OK", str(res["backup_csv"]), str(res["manifest"])))
    except Exception as exc:
        result_queue.put((f"ERROR: {exc!r}", None, None))


def _worker_restore(backup_path, dest_path, result_queue, barrier):
    """Wait for barrier then perform restore_backup under lock."""
    try:
        barrier.wait(timeout=10.0)
        restore_backup(backup_path=backup_path, dest_path=dest_path)
        result_queue.put("OK")
    except Exception as exc:
        result_queue.put(f"ERROR: {exc!r}")


class TestBackupRestoreBase(unittest.TestCase):
    """Base setup with temporary directory and test helpers."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_dir = Path(self.temp_dir.name)
        self.data_path = self.data_dir / "grades.csv"
        self.backup_dir = self.data_dir / "backups"

    def write_csv(self, rows, path=None, header=None):
        target = self.data_path if path is None else Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        text = (HEADER if header is None else header) + "".join(rows)
        target.write_text(text, encoding="utf-8", newline="")

    def file_bytes(self, path=None):
        target = self.data_path if path is None else Path(path)
        return target.read_bytes()

    def run_cli(self, *args, data_path=None):
        csv_file = self.data_path if data_path is None else Path(data_path)
        argv = ["grade_tracker", "--data", str(csv_file), *args]
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            mock.patch("sys.argv", argv),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                main()
            except SystemExit as exc:
                return exc.code, stdout.getvalue(), stderr.getvalue()
        return 0, stdout.getvalue(), stderr.getvalue()


class TestBackupCreation(TestBackupRestoreBase):
    """Tests covering backup creation, metadata, unique names, and error handling."""

    def test_backup_creation_success(self):
        self.write_csv([
            "CS101,Mathematics,95,100,2025-01-15,3,1\n",
            "CS101,Physics,85,100,2025-01-16,3,1\n",
        ])
        result = create_backup(csv_path=self.data_path, backup_dir=self.backup_dir)

        self.assertTrue(result["backup_csv"].exists())
        self.assertTrue(result["manifest"].exists())
        self.assertEqual(result["backup_csv"].read_bytes(), self.data_path.read_bytes())

        # Verify manifest content
        meta = json.loads(result["manifest"].read_text(encoding="utf-8"))
        self.assertEqual(meta["format_version"], "1.0")
        self.assertEqual(meta["schema_version"], "1.0")
        self.assertEqual(meta["records"], 2)
        self.assertEqual(meta["bytes"], len(self.data_path.read_bytes()))
        self.assertEqual(meta["sha256"], compute_sha256(self.data_path))
        self.assertEqual(
            meta["header"],
            ["student_id", "subject", "score", "max_score", "date", "credits", "semester"],
        )

    def test_backup_cli_command_success(self):
        self.write_csv([
            "CS101,Mathematics,95,100,2025-01-15,3,1\n",
        ])
        code, stdout, stderr = self.run_cli("backup")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Backup created successfully:", stdout)
        self.assertIn("Status: verified", stdout)
        self.assertIn("Records: 1", stdout)

    def test_unique_backup_names_for_repeated_operations(self):
        self.write_csv(["CS101,Mathematics,95,100,2025-01-15,3,1\n"])

        # Create two backups rapidly
        res1 = create_backup(csv_path=self.data_path, backup_dir=self.backup_dir)
        res2 = create_backup(csv_path=self.data_path, backup_dir=self.backup_dir)

        self.assertNotEqual(res1["backup_csv"], res2["backup_csv"])
        self.assertNotEqual(res1["manifest"], res2["manifest"])
        self.assertTrue(res1["backup_csv"].exists())
        self.assertTrue(res2["backup_csv"].exists())

    def test_backup_fails_when_source_missing(self):
        missing_csv = self.data_dir / "nonexistent.csv"
        with self.assertRaises(FileNotFoundError) as ctx:
            create_backup(csv_path=missing_csv)
        self.assertIn("source data file does not exist", str(ctx.exception))

        # CLI invocation
        code, stdout, stderr = self.run_cli("backup", data_path=missing_csv)
        self.assertEqual(code, 1)
        self.assertIn("source data file does not exist", stderr)

    def test_backup_fails_when_source_is_invalid(self):
        # CSV with invalid row score
        self.write_csv(["CS101,Mathematics,abc,100,2025-01-15,3,1\n"])
        with self.assertRaises(ValueError) as ctx:
            create_backup(csv_path=self.data_path, backup_dir=self.backup_dir)
        self.assertIn("cannot backup invalid source CSV", str(ctx.exception))

        code, stdout, stderr = self.run_cli("backup")
        self.assertEqual(code, 1)
        self.assertIn("cannot backup invalid source CSV", stderr)


class TestRestoreValidation(TestBackupRestoreBase):
    """Tests covering backup verification, schema validation, and integrity checks."""

    def test_successful_restore_of_valid_backup(self):
        self.write_csv([
            "CS101,Mathematics,95,100,2025-01-15,3,1\n",
            "CS101,Physics,85,100,2025-01-16,3,1\n",
        ])
        backup_info = create_backup(csv_path=self.data_path, backup_dir=self.backup_dir)

        # Modify destination
        dest_csv = self.data_dir / "target.csv"
        self.write_csv(["CS999,History,50,100,2025-01-01,3,1\n"], path=dest_csv)

        # Restore using backup CSV path
        restore_backup(backup_path=backup_info["backup_csv"], dest_path=dest_csv)
        self.assertEqual(dest_csv.read_bytes(), backup_info["backup_csv"].read_bytes())

    def test_successful_restore_via_manifest_path(self):
        self.write_csv([
            "CS101,Mathematics,95,100,2025-01-15,3,1\n",
        ])
        backup_info = create_backup(csv_path=self.data_path, backup_dir=self.backup_dir)
        dest_csv = self.data_dir / "target.csv"

        # Restore using manifest JSON path
        restore_backup(backup_path=backup_info["manifest"], dest_path=dest_csv)
        self.assertEqual(dest_csv.read_bytes(), backup_info["backup_csv"].read_bytes())

    def test_restore_cli_command_success(self):
        self.write_csv(["CS101,Mathematics,95,100,2025-01-15,3,1\n"])
        backup_info = create_backup(csv_path=self.data_path, backup_dir=self.backup_dir)
        target_csv = self.data_dir / "target.csv"

        code, stdout, stderr = self.run_cli(
            "restore", str(backup_info["backup_csv"]), data_path=target_csv
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Restore completed successfully:", stdout)
        self.assertIn("Status: verified", stdout)
        self.assertEqual(target_csv.read_bytes(), backup_info["backup_csv"].read_bytes())

    def test_restore_when_destination_does_not_exist(self):
        self.write_csv(["CS101,Mathematics,95,100,2025-01-15,3,1\n"])
        backup_info = create_backup(csv_path=self.data_path, backup_dir=self.backup_dir)
        non_existent_target = self.data_dir / "new_folder" / "restored.csv"

        restore_backup(backup_path=backup_info["backup_csv"], dest_path=non_existent_target)
        self.assertTrue(non_existent_target.exists())
        self.assertEqual(non_existent_target.read_bytes(), backup_info["backup_csv"].read_bytes())

    def test_reject_checksum_mismatch(self):
        self.write_csv(["CS101,Mathematics,95,100,2025-01-15,3,1\n"])
        backup_info = create_backup(csv_path=self.data_path, backup_dir=self.backup_dir)

        # Tamper with backup CSV
        with open(backup_info["backup_csv"], "a", encoding="utf-8") as f:
            f.write("CS101,Physics,80,100,2025-01-16,3,1\n")

        dest_csv = self.data_dir / "target.csv"
        initial_target_bytes = b"initial,target,bytes\n"
        dest_csv.write_bytes(initial_target_bytes)

        with self.assertRaises(ValueError) as ctx:
            restore_backup(backup_path=backup_info["backup_csv"], dest_path=dest_csv)
        self.assertIn("checksum mismatch", str(ctx.exception))
        # Destination unchanged
        self.assertEqual(dest_csv.read_bytes(), initial_target_bytes)

    def test_reject_missing_manifest(self):
        self.write_csv(["CS101,Mathematics,95,100,2025-01-15,3,1\n"])
        backup_info = create_backup(csv_path=self.data_path, backup_dir=self.backup_dir)
        backup_info["manifest"].unlink()

        dest_csv = self.data_dir / "target.csv"
        dest_csv.write_bytes(b"initial\n")

        with self.assertRaises(FileNotFoundError) as ctx:
            restore_backup(backup_path=backup_info["backup_csv"], dest_path=dest_csv)
        self.assertIn("backup manifest file not found", str(ctx.exception))
        self.assertEqual(dest_csv.read_bytes(), b"initial\n")

    def test_reject_malformed_manifest_json(self):
        self.write_csv(["CS101,Mathematics,95,100,2025-01-15,3,1\n"])
        backup_info = create_backup(csv_path=self.data_path, backup_dir=self.backup_dir)
        backup_info["manifest"].write_text("{bad-json:", encoding="utf-8")

        dest_csv = self.data_dir / "target.csv"
        dest_csv.write_bytes(b"initial\n")

        with self.assertRaises(ValueError) as ctx:
            restore_backup(backup_path=backup_info["backup_csv"], dest_path=dest_csv)
        self.assertIn("malformed backup manifest", str(ctx.exception))
        self.assertEqual(dest_csv.read_bytes(), b"initial\n")

    def test_reject_unsupported_format_version(self):
        self.write_csv(["CS101,Mathematics,95,100,2025-01-15,3,1\n"])
        backup_info = create_backup(csv_path=self.data_path, backup_dir=self.backup_dir)
        meta = json.loads(backup_info["manifest"].read_text(encoding="utf-8"))
        meta["format_version"] = "99.0"
        backup_info["manifest"].write_text(json.dumps(meta), encoding="utf-8")

        dest_csv = self.data_dir / "target.csv"
        dest_csv.write_bytes(b"initial\n")

        with self.assertRaises(ValueError) as ctx:
            restore_backup(backup_path=backup_info["backup_csv"], dest_path=dest_csv)
        self.assertIn("unsupported backup format version", str(ctx.exception))
        self.assertEqual(dest_csv.read_bytes(), b"initial\n")

    def test_reject_unsupported_schema_version(self):
        self.write_csv(["CS101,Mathematics,95,100,2025-01-15,3,1\n"])
        backup_info = create_backup(csv_path=self.data_path, backup_dir=self.backup_dir)
        meta = json.loads(backup_info["manifest"].read_text(encoding="utf-8"))
        meta["schema_version"] = "2.0"
        backup_info["manifest"].write_text(json.dumps(meta), encoding="utf-8")

        dest_csv = self.data_dir / "target.csv"
        dest_csv.write_bytes(b"initial\n")

        with self.assertRaises(ValueError) as ctx:
            restore_backup(backup_path=backup_info["backup_csv"], dest_path=dest_csv)
        self.assertIn("unsupported backup schema version", str(ctx.exception))
        self.assertEqual(dest_csv.read_bytes(), b"initial\n")

    def test_reject_corrupt_or_invalid_csv_records(self):
        # Create valid backup
        self.write_csv(["CS101,Mathematics,95,100,2025-01-15,3,1\n"])
        backup_info = create_backup(csv_path=self.data_path, backup_dir=self.backup_dir)

        # Alter backup CSV with invalid row and update manifest checksum to match
        corrupt_content = HEADER + "CS101,Mathematics,999,100,2025-01-15,3,1\n"
        backup_info["backup_csv"].write_text(corrupt_content, encoding="utf-8")
        meta = json.loads(backup_info["manifest"].read_text(encoding="utf-8"))
        meta["sha256"] = compute_sha256(backup_info["backup_csv"])
        backup_info["manifest"].write_text(json.dumps(meta), encoding="utf-8")

        dest_csv = self.data_dir / "target.csv"
        dest_csv.write_bytes(b"initial\n")

        with self.assertRaises(ValueError) as ctx:
            restore_backup(backup_path=backup_info["backup_csv"], dest_path=dest_csv)
        self.assertIn("backup CSV validation failed", str(ctx.exception))
        self.assertEqual(dest_csv.read_bytes(), b"initial\n")

    def test_reject_restore_over_backup_source_or_manifest(self):
        self.write_csv(["CS101,Mathematics,95,100,2025-01-15,3,1\n"])
        backup_info = create_backup(csv_path=self.data_path, backup_dir=self.backup_dir)

        # Attempt to restore over the backup file itself
        with self.assertRaises(ValueError) as ctx:
            restore_backup(
                backup_path=backup_info["backup_csv"],
                dest_path=backup_info["backup_csv"],
            )
        self.assertIn("destination cannot be the backup file or its manifest", str(ctx.exception))

        # Attempt to restore over manifest
        with self.assertRaises(ValueError) as ctx:
            restore_backup(
                backup_path=backup_info["backup_csv"],
                dest_path=backup_info["manifest"],
            )
        self.assertIn("destination cannot be the backup file or its manifest", str(ctx.exception))

    def test_temporary_file_cleanup_on_failure(self):
        self.write_csv(["CS101,Mathematics,95,100,2025-01-15,3,1\n"])
        backup_info = create_backup(csv_path=self.data_path, backup_dir=self.backup_dir)

        dest_csv = self.data_dir / "target.csv"
        dest_csv.write_bytes(b"original_content\n")

        # Mock os.replace to simulate failure during destination swap
        with mock.patch("os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                restore_backup(backup_path=backup_info["backup_csv"], dest_path=dest_csv)

        # Ensure original file remains intact
        self.assertEqual(dest_csv.read_bytes(), b"original_content\n")
        # Ensure temporary files are cleaned up in destination directory
        tmp_files = list(self.data_dir.glob(".grades-restore-*.tmp"))
        self.assertEqual(tmp_files, [])


class TestPathsWithSpacesAndSpecialChars(TestBackupRestoreBase):
    """Test paths containing spaces and platform-specific path formats."""

    def test_backup_and_restore_with_spaces_in_path(self):
        spaced_dir = self.data_dir / "folder with spaces"
        spaced_dir.mkdir(parents=True, exist_ok=True)
        spaced_csv = spaced_dir / "my grades file.csv"
        self.write_csv(["CS101,Mathematics,95,100,2025-01-15,3,1\n"], path=spaced_csv)

        backup_spaced_dir = spaced_dir / "backups folder"
        result = create_backup(csv_path=spaced_csv, backup_dir=backup_spaced_dir)

        self.assertTrue(result["backup_csv"].exists())
        self.assertTrue(result["manifest"].exists())

        dest_spaced = spaced_dir / "target grades.csv"
        restore_result = restore_backup(
            backup_path=result["backup_csv"], dest_path=dest_spaced
        )
        self.assertEqual(dest_spaced.read_bytes(), spaced_csv.read_bytes())
        self.assertEqual(restore_result["records"], 1)


class TestConcurrencyMultiProcess(TestBackupRestoreBase):
    """Genuine inter-process concurrency tests using spawn start method."""

    def test_backup_consistency_with_concurrent_writer(self):
        self.write_csv(["CS101,Mathematics,95,100,2025-01-15,3,1\n"])
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(2)
        result_queue = ctx.Queue()

        writer_proc = ctx.Process(
            target=_worker_writer_add,
            args=(self.data_path, "CS101", "Physics", result_queue, barrier),
        )
        backup_proc = ctx.Process(
            target=_worker_backup,
            args=(self.data_path, self.backup_dir, result_queue, barrier),
        )

        writer_proc.start()
        backup_proc.start()

        writer_proc.join(timeout=15.0)
        backup_proc.join(timeout=15.0)

        self.assertFalse(writer_proc.is_alive())
        self.assertFalse(backup_proc.is_alive())

        results = []
        while not result_queue.empty():
            results.append(result_queue.get())

        self.assertEqual(len(results), 2)
        # Verify backup is valid and consistent
        backup_res = [r for r in results if isinstance(r, tuple)][0]
        self.assertEqual(backup_res[0], "OK")
        backup_csv = Path(backup_res[1])
        # The backup must either contain 1 record (before writer) or 2 records (after writer)
        with open(backup_csv, newline="", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
            self.assertIn(len(reader), (1, 2))

    def test_restore_serialization_with_concurrent_writer(self):
        # Prepare a valid backup containing CS101,Mathematics
        self.write_csv(["CS101,Mathematics,95,100,2025-01-15,3,1\n"])
        backup_info = create_backup(csv_path=self.data_path, backup_dir=self.backup_dir)

        # Now have concurrent writer and restore targeting the same destination
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(2)
        result_queue = ctx.Queue()

        writer_proc = ctx.Process(
            target=_worker_writer_add,
            args=(self.data_path, "CS102", "Chemistry", result_queue, barrier),
        )
        restore_proc = ctx.Process(
            target=_worker_restore,
            args=(backup_info["backup_csv"], self.data_path, result_queue, barrier),
        )

        writer_proc.start()
        restore_proc.start()

        writer_proc.join(timeout=15.0)
        restore_proc.join(timeout=15.0)

        self.assertFalse(writer_proc.is_alive())
        self.assertFalse(restore_proc.is_alive())

        # Verify destination CSV is not corrupted and matches valid CSV schema
        with open(self.data_path, newline="", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
            # Must have either restored state (1 record) or writer applied (1 or 2 records depending on order)
            self.assertTrue(len(reader) >= 1)
            # Ensure each row has valid structure
            for row in reader:
                self.assertIn("student_id", row)
                self.assertIn("score", row)


if __name__ == "__main__":
    unittest.main()

