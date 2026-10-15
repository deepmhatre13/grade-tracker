"""Comprehensive tests for the bulk import command and transaction semantics (Issue #31).

Tests cover:
- Basic functionality (import valid CSV, non-existent destination creation, schema & counts).
- Validation (missing/empty/unreadable/malformed file, bad headers, bad values, row numbers, no partial import).
- Conflict resolution (intra-file duplicates, existing-in-dest duplicates, error vs skip, case-insensitivity, first-encountered wins).
- Atomicity and failure recovery (tempfile creation fail, write/fsync fail, os.replace fail, destination and source immutability, cleanup).
- Concurrency (independent OS processes importing overlapping records, import running concurrently with add, lock serialization).
"""

import contextlib
import csv
import io
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from grade_tracker.cli import (
    main,
    import_grades_csv,
    ImportResult,
    _lock_path_for,
    _InterProcessLock,
)

# Use 'spawn' start method on all platforms for multi-process concurrency tests
try:
    multiprocessing.set_start_method("spawn", force=True)
except RuntimeError:
    pass


HEADER = "student_id,subject,score,max_score,date,credits,semester\n"


def _worker_import(source_path, dest_path, on_conflict, result_queue, barrier):
    """Run import_grades_csv from an independent child process."""
    try:
        barrier.wait(timeout=10.0)
        from grade_tracker.cli import import_grades_csv
        result = import_grades_csv(source_path, dest_path, on_conflict=on_conflict)
        result_queue.put(("OK", result.imported, result.skipped, result.considered))
    except ValueError as exc:
        result_queue.put(("DUPLICATE", str(exc)))
    except Exception as exc:  # pragma: no cover
        result_queue.put(("ERROR", repr(exc)))


def _worker_add(dest_path, student_id, subject, score, max_score, date, credits, semester, result_queue, barrier):
    """Run save_grade from an independent child process."""
    try:
        barrier.wait(timeout=10.0)
        from grade_tracker.cli import save_grade
        save_grade(
            student_id=student_id,
            subject=subject,
            score=score,
            max_score=max_score,
            date=date,
            credits=credits,
            semester=semester,
            csv_path=str(dest_path),
        )
        result_queue.put(("OK_ADD", subject))
    except ValueError as exc:
        result_queue.put(("DUPLICATE_ADD", str(exc)))
    except Exception as exc:  # pragma: no cover
        result_queue.put(("ERROR_ADD", repr(exc)))


class TestImportCLI(unittest.TestCase):
    """Regression tests for the 'import' CLI command and import_grades_csv function."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_dir = Path(self.temp_dir.name)
        self.dest_path = self.data_dir / "grades.csv"
        self.source_path = self.data_dir / "import_source.csv"

    def write_csv(self, path, rows, header=None):
        text = (HEADER if header is None else header) + "".join(rows)
        path.write_text(text, encoding="utf-8", newline="")

    def read_rows(self, path=None):
        target = self.dest_path if path is None else Path(path)
        if not target.exists():
            return []
        with open(target, mode="r", encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))

    def run_cli_import(self, source_path, dest_path=None, on_conflict=None):
        dest = self.dest_path if dest_path is None else Path(dest_path)
        argv = ["grade_tracker", "--data", str(dest), "import", str(source_path)]
        if on_conflict is not None:
            argv.extend(["--on-conflict", on_conflict])
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

    # --- 1. Basic Functionality ---

    def test_import_valid_csv_success(self):
        self.write_csv(
            self.source_path,
            [
                "S1,Mathematics,90,100,2026-01-10,3.0,1\n",
                "S1,Physics,85,100,2026-01-11,4.0,1\n",
            ],
        )
        code, stdout, stderr = self.run_cli_import(self.source_path)
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Import completed successfully: 2 record(s) imported, 0 record(s) skipped (2 valid record(s) considered).", stdout)
        rows = self.read_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["student_id"], "S1")
        self.assertEqual(rows[0]["subject"], "Mathematics")
        self.assertEqual(rows[1]["subject"], "Physics")

    def test_import_into_nonexistent_destination(self):
        dest = self.data_dir / "nested" / "dest_grades.csv"
        self.assertFalse(dest.exists())
        self.write_csv(
            self.source_path,
            ["S1,Mathematics,90,100,2026-01-10,3.0,1\n"],
        )
        code, stdout, stderr = self.run_cli_import(self.source_path, dest_path=dest)
        self.assertEqual(code, 0)
        self.assertTrue(dest.exists())
        rows = self.read_rows(dest)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject"], "Mathematics")

    def test_import_preserves_destination_schema(self):
        self.write_csv(
            self.dest_path,
            ["S1,History,80,100,2026-01-01,2.0,1\n"],
        )
        self.write_csv(
            self.source_path,
            ["S2,Biology,95,100,2026-01-02,3.0,1\n"],
        )
        code, _, _ = self.run_cli_import(self.source_path)
        self.assertEqual(code, 0)
        with open(self.dest_path, mode="r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            header = next(reader)
            self.assertEqual(
                header,
                ["student_id", "subject", "score", "max_score", "date", "credits", "semester"],
            )
        rows = self.read_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["subject"], "History")
        self.assertEqual(rows[1]["subject"], "Biology")

    def test_import_legacy_source_defaults_credits_and_semester(self):
        legacy_header = "student_id,subject,score,max_score,date\n"
        self.write_csv(
            self.source_path,
            ["S1,Mathematics,90,100,2026-01-10\n"],
            header=legacy_header,
        )
        code, stdout, stderr = self.run_cli_import(self.source_path)
        self.assertEqual(code, 0)
        rows = self.read_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["credits"], "3.0")
        self.assertEqual(rows[0]["semester"], "1")

    # --- 2. Validation & Diagnostic Errors ---

    def test_reject_missing_source_file(self):
        non_existent = self.data_dir / "does_not_exist.csv"
        code, stdout, stderr = self.run_cli_import(non_existent)
        self.assertEqual(code, 1)
        self.assertIn("file not found", stderr)
        self.assertFalse(self.dest_path.exists())

    def test_reject_empty_source_file(self):
        self.source_path.write_text("", encoding="utf-8")
        code, stdout, stderr = self.run_cli_import(self.source_path)
        self.assertEqual(code, 1)
        self.assertIn("file is empty", stderr)

    def test_reject_same_source_and_destination(self):
        self.write_csv(self.dest_path, ["S1,Mathematics,90,100,2026-01-10,3.0,1\n"])
        code, stdout, stderr = self.run_cli_import(self.dest_path, dest_path=self.dest_path)
        self.assertEqual(code, 1)
        self.assertIn("must not resolve to the same file", stderr)

    def test_reject_missing_required_column(self):
        bad_header = "student_id,subject,score,date,credits,semester\n"
        self.write_csv(
            self.source_path,
            ["S1,Mathematics,90,2026-01-10,3.0,1\n"],
            header=bad_header,
        )
        code, stdout, stderr = self.run_cli_import(self.source_path)
        self.assertEqual(code, 1)
        self.assertIn("missing required column(s): max_score", stderr)
        self.assertFalse(self.dest_path.exists())

    def test_reject_malformed_record_field_count(self):
        self.write_csv(
            self.source_path,
            [
                "S1,Mathematics,90,100,2026-01-10,3.0,1\n",
                "S1,Physics,85,100,2026-01-11,4.0,1,EXTRA\n",
            ],
        )
        code, stdout, stderr = self.run_cli_import(self.source_path)
        self.assertEqual(code, 1)
        self.assertIn("row 3: malformed record: expected 7 fields, got 8", stderr)
        self.assertFalse(self.dest_path.exists())

    def test_reject_invalid_field_values_shows_row_numbers(self):
        self.write_csv(
            self.source_path,
            [
                "S1,Mathematics,not_a_num,100,2026-01-10,3.0,1\n",
                "S1,Physics,150,100,2026-01-11,4.0,1\n",
                "S1,Chemistry,90,100,2026-02-30,3.0,1\n",
            ],
        )
        code, stdout, stderr = self.run_cli_import(self.source_path)
        self.assertEqual(code, 1)
        self.assertIn("row 2, field score:", stderr)
        self.assertIn("row 3, field score:", stderr)
        self.assertIn("row 4, field date:", stderr)
        self.assertFalse(self.dest_path.exists())

    def test_all_or_nothing_invalid_row_aborts_entire_import(self):
        self.write_csv(
            self.source_path,
            [
                "S1,Mathematics,90,100,2026-01-10,3.0,1\n",
                "S1,Physics,-5,100,2026-01-11,4.0,1\n",
                "S1,Chemistry,90,100,2026-01-12,3.0,1\n",
            ],
        )
        code, _, stderr = self.run_cli_import(self.source_path)
        self.assertEqual(code, 1)
        self.assertIn("row 3, field score:", stderr)
        self.assertFalse(self.dest_path.exists())

    # --- 3. Conflict Resolution ---

    def test_conflict_intra_file_error_policy(self):
        self.write_csv(
            self.source_path,
            [
                "S1,Mathematics,90,100,2026-01-10,3.0,1\n",
                "S1,Mathematics,95,100,2026-01-10,3.0,1\n",
            ],
        )
        code, stdout, stderr = self.run_cli_import(self.source_path, on_conflict="error")
        self.assertEqual(code, 1)
        self.assertIn("duplicate assessment at row 3", stderr)
        self.assertIn("duplicates row 2", stderr)
        self.assertFalse(self.dest_path.exists())

    def test_conflict_existing_in_destination_error_policy(self):
        self.write_csv(
            self.dest_path,
            ["S1,Mathematics,80,100,2026-01-10,3.0,1\n"],
        )
        original_dest_bytes = self.dest_path.read_bytes()
        self.write_csv(
            self.source_path,
            [
                "S1,Physics,85,100,2026-01-11,4.0,1\n",
                "S1,Mathematics,90,100,2026-01-10,3.0,1\n",
            ],
        )
        code, stdout, stderr = self.run_cli_import(self.source_path, on_conflict="error")
        self.assertEqual(code, 1)
        self.assertIn("assessment at row 3 already exists in destination", stderr)
        self.assertEqual(self.dest_path.read_bytes(), original_dest_bytes)

    def test_conflict_skip_policy_intra_and_dest(self):
        self.write_csv(
            self.dest_path,
            ["S1,Mathematics,80,100,2026-01-10,3.0,1\n"],
        )
        self.write_csv(
            self.source_path,
            [
                "S1,Mathematics,90,100,2026-01-10,3.0,1\n",   # conflict with dest -> skip
                "S1,Physics,85,100,2026-01-11,4.0,1\n",       # new -> import
                "S1,Physics,88,100,2026-01-11,4.0,1\n",       # duplicate in file -> skip
                "S1,Chemistry,95,100,2026-01-12,3.0,1\n",     # new -> import
            ],
        )
        code, stdout, stderr = self.run_cli_import(self.source_path, on_conflict="skip")
        self.assertEqual(code, 0)
        self.assertIn("2 record(s) imported, 2 record(s) skipped (4 valid record(s) considered)", stdout)
        rows = self.read_rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["subject"], "Mathematics")
        self.assertEqual(rows[0]["score"], "80")
        self.assertEqual(rows[1]["subject"], "Physics")
        self.assertEqual(rows[1]["score"], "85")
        self.assertEqual(rows[2]["subject"], "Chemistry")
        self.assertEqual(rows[2]["score"], "95")

    def test_conflict_case_insensitivity_and_different_semester(self):
        self.write_csv(
            self.dest_path,
            ["S1,mathematics,80,100,2026-01-10,3.0,1\n"],
        )
        self.write_csv(
            self.source_path,
            [
                "S1,MATHEMATICS,85,100,2026-01-10,3.0,1\n",  # case-insensitive match -> conflict
                "S1,Mathematics,90,100,2026-01-10,3.0,2\n",  # different semester -> distinct
            ],
        )
        code, stdout, stderr = self.run_cli_import(self.source_path, on_conflict="skip")
        self.assertEqual(code, 0)
        self.assertIn("1 record(s) imported, 1 record(s) skipped", stdout)
        rows = self.read_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["semester"], "1")
        self.assertEqual(rows[1]["semester"], "2")

    # --- 4. Atomicity and Failure Recovery ---

    def test_source_file_is_never_modified(self):
        self.write_csv(
            self.source_path,
            [
                "S1,Mathematics,90,100,2026-01-10,3.0,1\n",
                "S1,Physics,85,100,2026-01-11,4.0,1\n",
            ],
        )
        source_bytes = self.source_path.read_bytes()
        self.run_cli_import(self.source_path)
        self.assertEqual(self.source_path.read_bytes(), source_bytes)

    def test_failure_on_tempfile_creation_leaves_dest_unchanged(self):
        self.write_csv(self.dest_path, ["S1,Mathematics,80,100,2026-01-10,3.0,1\n"])
        dest_bytes = self.dest_path.read_bytes()
        self.write_csv(self.source_path, ["S1,Physics,85,100,2026-01-11,4.0,1\n"])

        with mock.patch("tempfile.mkstemp", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                import_grades_csv(self.source_path, self.dest_path)

        self.assertEqual(self.dest_path.read_bytes(), dest_bytes)

    def test_failure_on_write_or_fsync_cleans_up_temp_and_leaves_dest_unchanged(self):
        self.write_csv(self.dest_path, ["S1,Mathematics,80,100,2026-01-10,3.0,1\n"])
        dest_bytes = self.dest_path.read_bytes()
        self.write_csv(self.source_path, ["S1,Physics,85,100,2026-01-11,4.0,1\n"])

        with mock.patch("os.fsync", side_effect=OSError("Sync error")):
            with self.assertRaises(OSError):
                import_grades_csv(self.source_path, self.dest_path)

        self.assertEqual(self.dest_path.read_bytes(), dest_bytes)
        # Verify no .tmp files left in dest directory
        tmp_files = list(self.data_dir.glob("*.tmp"))
        self.assertEqual(tmp_files, [])

    def test_failure_on_replace_cleans_up_and_leaves_dest_unchanged(self):
        self.write_csv(self.dest_path, ["S1,Mathematics,80,100,2026-01-10,3.0,1\n"])
        dest_bytes = self.dest_path.read_bytes()
        self.write_csv(self.source_path, ["S1,Physics,85,100,2026-01-11,4.0,1\n"])

        with mock.patch("os.replace", side_effect=OSError("Permission denied on replace")):
            with self.assertRaises(OSError):
                import_grades_csv(self.source_path, self.dest_path)

        self.assertEqual(self.dest_path.read_bytes(), dest_bytes)
        tmp_files = list(self.data_dir.glob("*.tmp"))
        self.assertEqual(tmp_files, [])

    # --- 5. Concurrency ---

    def test_concurrent_independent_process_imports_overlapping_records(self):
        """Two OS processes importing files with overlapping assessments.
        Under on_conflict='error', exactly one process must succeed and one must fail
        with a duplicate error, leaving no duplicate in destination.
        """
        src1 = self.data_dir / "src1.csv"
        src2 = self.data_dir / "src2.csv"
        self.write_csv(src1, ["S1,Mathematics,90,100,2026-01-10,3.0,1\n"])
        self.write_csv(src2, ["S1,Mathematics,95,100,2026-01-10,3.0,1\n"])

        result_queue = multiprocessing.Queue()
        barrier = multiprocessing.Barrier(2)

        p1 = multiprocessing.Process(
            target=_worker_import,
            args=(str(src1), str(self.dest_path), "error", result_queue, barrier),
        )
        p2 = multiprocessing.Process(
            target=_worker_import,
            args=(str(src2), str(self.dest_path), "error", result_queue, barrier),
        )

        p1.start()
        p2.start()

        p1.join(timeout=10.0)
        p2.join(timeout=10.0)

        results = []
        while not result_queue.empty():
            results.append(result_queue.get_nowait())

        self.assertEqual(len(results), 2, f"Unexpected results: {results}")
        status_types = [r[0] for r in results]
        self.assertIn("OK", status_types)
        self.assertIn("DUPLICATE", status_types)

        rows = self.read_rows()
        self.assertEqual(len(rows), 1, "Exactly one assessment record should be present")

    def test_concurrent_import_and_add(self):
        """One process importing while another process adds the same assessment."""
        src = self.data_dir / "src_add.csv"
        self.write_csv(src, ["S1,History,90,100,2026-01-10,3.0,1\n"])

        result_queue = multiprocessing.Queue()
        barrier = multiprocessing.Barrier(2)

        p_import = multiprocessing.Process(
            target=_worker_import,
            args=(str(src), str(self.dest_path), "error", result_queue, barrier),
        )
        p_add = multiprocessing.Process(
            target=_worker_add,
            args=(str(self.dest_path), "S1", "History", 85.0, 100.0, "2026-01-10", 3.0, "1", result_queue, barrier),
        )

        p_import.start()
        p_add.start()

        p_import.join(timeout=10.0)
        p_add.join(timeout=10.0)

        results = []
        while not result_queue.empty():
            results.append(result_queue.get_nowait())

        self.assertEqual(len(results), 2, f"Results: {results}")
        # One of them must have succeeded and one must have received a duplicate error
        import_res = next(r for r in results if r[0] in ("OK", "DUPLICATE"))
        add_res = next(r for r in results if r[0] in ("OK_ADD", "DUPLICATE_ADD"))

        successes = (1 if import_res[0] == "OK" else 0) + (1 if add_res[0] == "OK_ADD" else 0)
        self.assertEqual(successes, 1, f"Expected exactly one success: {results}")

        rows = self.read_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject"], "History")


if __name__ == "__main__":
    unittest.main()
