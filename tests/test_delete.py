"""Regression and concurrency tests for the ``delete`` subcommand.

Covers:
1. Selection by student_id, semester, subject, date.
2. AND semantics with multiple selectors.
3. Rejecting requests without selectors.
4. Rejecting empty/invalid selector values (e.g. malformed dates).
5. Rejecting malformed CSV input without modifying the file.
6. Zero matches handling (no modification, clean exit).
7. Order and contents preservation of non-matching records.
8. Dry-run preview: accurate preview, zero modification, byte-for-byte identical, no temp files.
9. Confirmation safeguard: missing --yes fails without modification; --yes permits deletion.
10. Failure atomicity: write failure, replace failure, audit failure leave destination intact.
11. Temporary files cleaned up on failure.
12. Paths with spaces supported.
13. Inter-process concurrency safety: concurrent writers, no lost updates, evaluating against
    latest data under lock.
"""

import contextlib
import csv
import io
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from grade_tracker.cli import delete_grades, main, _lock_path_for

HEADER = "student_id,subject,score,max_score,date,credits,semester\n"


# Child worker for multiprocessing concurrency test
def _worker_concurrent_delete(csv_path, result_queue, barrier, student_id):
    """Delete a student's records concurrently from an independent process."""
    try:
        barrier.wait(timeout=10.0)
        from grade_tracker.cli import delete_grades

        count, _ = delete_grades(
            csv_path=csv_path,
            student_id=student_id,
            confirmed=True,
        )
        result_queue.put(f"OK-{student_id}-{count}")
    except Exception as exc:
        result_queue.put(f"ERROR: {exc!r}")


def _worker_concurrent_add(csv_path, result_queue, barrier, student_id):
    """Add a grade record concurrently from an independent process."""
    try:
        barrier.wait(timeout=10.0)
        from grade_tracker.cli import save_grade

        save_grade(
            student_id=student_id,
            subject="History",
            score=90.0,
            max_score=100.0,
            date="2026-10-10",
            credits=3.0,
            semester="1",
            csv_path=csv_path,
        )
        result_queue.put(f"OK-ADD-{student_id}")
    except Exception as exc:
        result_queue.put(f"ERROR: {exc!r}")


class DeleteTestCase(unittest.TestCase):
    """Base test case with helper methods for test setup and CLI invocation."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_path = Path(self.temp_dir.name) / "grades.csv"

    def write_csv(self, rows, header=None):
        text = (HEADER if header is None else header) + "".join(rows)
        self.data_path.write_text(text, encoding="utf-8", newline="")

    def file_bytes(self):
        return self.data_path.read_bytes()

    def read_rows(self):
        if not self.data_path.exists():
            return []
        with self.data_path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def temp_files(self):
        return sorted(
            p.name for p in Path(self.temp_dir.name).iterdir() if p.name.endswith(".tmp")
        )

    def run_cli(self, *args):
        argv = ["grade_tracker", "--data", str(self.data_path), "delete", *args]
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


class TestDeleteSelectionAndValidation(DeleteTestCase):
    """Test selector rules, validation, and matching logic."""

    def test_delete_by_student_id(self):
        self.write_csv([
            "S1,Mathematics,95,100,2025-01-15,3,1\n",
            "S1,Physics,85,100,2025-01-16,3,1\n",
            "S2,Mathematics,70,100,2025-01-15,3,1\n",
        ])
        code, stdout, stderr = self.run_cli("--student-id", "S1", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("Successfully deleted 2 records.", stdout)
        rows = self.read_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["student_id"], "S2")

    def test_delete_by_semester(self):
        self.write_csv([
            "S1,Mathematics,95,100,2025-01-15,3,1\n",
            "S1,History,85,100,2025-06-15,3,2\n",
            "S2,History,90,100,2025-06-15,3,2\n",
        ])
        code, stdout, stderr = self.run_cli("--semester", "2", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("Successfully deleted 2 records.", stdout)
        rows = self.read_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["semester"], "1")

    def test_delete_by_subject_case_insensitive(self):
        self.write_csv([
            "S1,Mathematics,95,100,2025-01-15,3,1\n",
            "S2,mathematics,80,100,2025-01-15,3,1\n",
            "S1,Physics,85,100,2025-01-16,3,1\n",
        ])
        code, stdout, stderr = self.run_cli("--subject", "MATHEMATICS", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("Successfully deleted 2 records.", stdout)
        rows = self.read_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject"], "Physics")

    def test_delete_by_date(self):
        self.write_csv([
            "S1,Mathematics,95,100,2025-01-15,3,1\n",
            "S1,Physics,85,100,2025-01-16,3,1\n",
        ])
        code, stdout, stderr = self.run_cli("--date", "2025-01-15", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("Successfully deleted 1 record.", stdout)
        rows = self.read_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject"], "Physics")

    def test_combine_multiple_selectors_and_semantics(self):
        self.write_csv([
            "S1,Mathematics,95,100,2025-01-15,3,1\n",
            "S1,Mathematics,80,100,2025-06-15,3,2\n",
            "S2,Mathematics,90,100,2025-01-15,3,1\n",
        ])
        # Only row 1 matches student_id=S1 AND semester=1
        code, stdout, stderr = self.run_cli("--student-id", "S1", "--semester", "1", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("Successfully deleted 1 record.", stdout)
        rows = self.read_rows()
        self.assertEqual(len(rows), 2)
        remaining = {(r["student_id"], r["semester"]) for r in rows}
        self.assertEqual(remaining, {("S1", "2"), ("S2", "1")})

    def test_reject_request_with_no_selectors(self):
        self.write_csv(["S1,Mathematics,95,100,2025-01-15,3,1\n"])
        before = self.file_bytes()
        code, stdout, stderr = self.run_cli("--yes")
        self.assertEqual(code, 2)
        self.assertIn("at least one selector is required", stderr)
        self.assertEqual(self.file_bytes(), before)

    def test_reject_blank_or_empty_selector_values(self):
        self.write_csv(["S1,Mathematics,95,100,2025-01-15,3,1\n"])
        before = self.file_bytes()

        for opt in ("--student-id", "--semester", "--subject"):
            with self.subTest(opt=opt):
                code, stdout, stderr = self.run_cli(opt, "   ", "--yes")
                self.assertEqual(code, 2)
                self.assertIn("cannot be empty or blank", stderr)
                self.assertEqual(self.file_bytes(), before)

    def test_reject_invalid_date_format_or_invalid_calendar_date(self):
        self.write_csv(["S1,Mathematics,95,100,2025-01-15,3,1\n"])
        before = self.file_bytes()

        # Malformed format
        code, stdout, stderr = self.run_cli("--date", "2025-1-15", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("YYYY-MM-DD format", stderr)
        self.assertEqual(self.file_bytes(), before)

        # Invalid calendar date
        code, stdout, stderr = self.run_cli("--date", "2025-02-30", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("valid calendar date", stderr)
        self.assertEqual(self.file_bytes(), before)

    def test_reject_malformed_csv_input_without_modifying_file(self):
        # Invalid CSV with non-numeric score
        self.write_csv(["S1,Mathematics,invalid_score,100,2025-01-15,3,1\n"])
        before = self.file_bytes()

        code, stdout, stderr = self.run_cli("--student-id", "S1", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("failed validation", stderr)
        self.assertEqual(self.file_bytes(), before)

    def test_handle_zero_matches_without_modifying_file(self):
        self.write_csv(["S1,Mathematics,95,100,2025-01-15,3,1\n"])
        before = self.file_bytes()

        code, stdout, stderr = self.run_cli("--student-id", "NONEXISTENT", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("0 records matched the criteria. No changes were made.", stdout)
        self.assertEqual(self.file_bytes(), before)

    def test_preserve_order_and_content_of_non_matching_records(self):
        rows_data = [
            "S1,Mathematics,95.0,100.0,2025-01-15,3.0,1\n",
            "S2,Physics,85.0,100.0,2025-01-16,4.0,1\n",
            "S3,Chemistry,75.0,100.0,2025-01-17,3.0,2\n",
            "S4,Biology,90.0,100.0,2025-01-18,2.0,2\n",
        ]
        self.write_csv(rows_data)

        # Delete S2 and S3
        code, stdout, stderr = self.run_cli("--semester", "1", "--subject", "Physics", "--yes")
        self.assertEqual(code, 0)

        rows = self.read_rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["student_id"], "S1")
        self.assertEqual(rows[1]["student_id"], "S3")
        self.assertEqual(rows[2]["student_id"], "S4")


class TestDeleteDryRunAndConfirmation(DeleteTestCase):
    """Test dry-run preview and confirmation safeguards."""

    def test_dry_run_reports_preview_and_does_not_modify_file(self):
        self.write_csv([
            "S1,Mathematics,95,100,2025-01-15,3,1\n",
            "S1,Physics,85,100,2025-01-16,3,1\n",
        ])
        before = self.file_bytes()

        code, stdout, stderr = self.run_cli("--student-id", "S1", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Dry run: 2 records would be deleted (no changes were made):", stdout)
        self.assertIn("Mathematics", stdout)
        self.assertIn("Physics", stdout)
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(self.temp_files(), [])

    def test_dry_run_with_zero_matches(self):
        self.write_csv(["S1,Mathematics,95,100,2025-01-15,3,1\n"])
        before = self.file_bytes()

        code, stdout, stderr = self.run_cli("--student-id", "UNKNOWN", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Dry run: 0 records match the criteria. No changes were made.", stdout)
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(self.temp_files(), [])

    def test_missing_confirmation_aborts_without_modification(self):
        self.write_csv(["S1,Mathematics,95,100,2025-01-15,3,1\n"])
        before = self.file_bytes()

        code, stdout, stderr = self.run_cli("--student-id", "S1")
        self.assertEqual(code, 2)
        self.assertIn("deletion requires confirmation; pass --yes", stderr)
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(self.temp_files(), [])

    def test_explicit_confirmation_via_yes_and_short_flag(self):
        self.write_csv([
            "S1,Mathematics,95,100,2025-01-15,3,1\n",
            "S2,Physics,85,100,2025-01-16,3,1\n",
        ])
        # Test -y short option
        code, stdout, stderr = self.run_cli("--student-id", "S1", "-y")
        self.assertEqual(code, 0)
        self.assertIn("Successfully deleted 1 record.", stdout)
        self.assertEqual(len(self.read_rows()), 1)

    def test_repeated_invocation_is_idempotent(self):
        self.write_csv(["S1,Mathematics,95,100,2025-01-15,3,1\n"])

        # First run deletes
        code1, stdout1, _ = self.run_cli("--student-id", "S1", "--yes")
        self.assertEqual(code1, 0)
        self.assertIn("Successfully deleted 1 record.", stdout1)

        # Second run has 0 matches
        code2, stdout2, _ = self.run_cli("--student-id", "S1", "--yes")
        self.assertEqual(code2, 0)
        self.assertIn("0 records matched the criteria. No changes were made.", stdout2)


class TestDeleteTransactionalityAndFailures(DeleteTestCase):
    """Test failure injection, atomic replace, and temp file hygiene."""

    def test_simulated_write_failure_preserves_destination(self):
        self.write_csv(["S1,Mathematics,95,100,2025-01-15,3,1\n"])
        before = self.file_bytes()

        with mock.patch("grade_tracker.cli.csv.writer") as mock_writer:
            mock_writer.side_effect = OSError("disk full")
            code, stdout, stderr = self.run_cli("--student-id", "S1", "--yes")

        self.assertEqual(code, 2)
        self.assertIn("failed to write", stderr)
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(self.temp_files(), [])

    def test_simulated_atomic_replace_failure_preserves_destination(self):
        self.write_csv(["S1,Mathematics,95,100,2025-01-15,3,1\n"])
        before = self.file_bytes()

        with mock.patch("os.replace", side_effect=OSError("permission denied")):
            code, stdout, stderr = self.run_cli("--student-id", "S1", "--yes")

        self.assertEqual(code, 2)
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(self.temp_files(), [])

    def test_paths_containing_spaces_work_correctly(self):
        spaced_dir = Path(self.temp_dir.name) / "my folder with spaces"
        spaced_dir.mkdir()
        spaced_csv = spaced_dir / "my grades.csv"
        spaced_csv.write_text(HEADER + "S1,Mathematics,95,100,2025-01-15,3,1\n", encoding="utf-8")

        argv = ["grade_tracker", "--data", str(spaced_csv), "delete", "--student-id", "S1", "--yes"]
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            mock.patch("sys.argv", argv),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                main()
                code = 0
            except SystemExit as exc:
                code = exc.code

        self.assertEqual(code, 0)
        with spaced_csv.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 0)

    def test_nonexistent_data_file_handled_cleanly(self):
        nonexistent = Path(self.temp_dir.name) / "missing.csv"
        argv = ["grade_tracker", "--data", str(nonexistent), "delete", "--student-id", "S1", "--yes"]
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            mock.patch("sys.argv", argv),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                main()
                code = 0
            except SystemExit as exc:
                code = exc.code

        self.assertEqual(code, 0)
        self.assertIn("0 records matched the criteria.", stdout.getvalue())


class TestDeleteConcurrency(DeleteTestCase):
    """Genuine inter-process concurrency tests for delete operations."""

    def _join_children_and_collect_results(self, children, result_queue):
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and any(p.is_alive() for p in children):
            for p in children:
                if p.is_alive():
                    p.join(timeout=max(0.1, deadline - time.monotonic()))
        if any(p.is_alive() for p in children):
            for p in children:
                p.terminate()
            self.fail("concurrent workers did not finish within the timeout")
        results = []
        while not result_queue.empty():
            try:
                results.append(result_queue.get_nowait())
            except Exception:
                break
        return results

    def test_concurrent_deletions_of_different_students(self):
        """Two processes deleting separate students serialize cleanly under lock."""
        self.write_csv([
            "S1,Mathematics,95,100,2025-01-15,3,1\n",
            "S2,Physics,85,100,2025-01-16,3,1\n",
            "S3,Chemistry,75,100,2025-01-17,3,1\n",
        ])

        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(2)
        result_queue = ctx.Queue()

        p1 = ctx.Process(
            target=_worker_concurrent_delete,
            args=(str(self.data_path), result_queue, barrier, "S1"),
        )
        p2 = ctx.Process(
            target=_worker_concurrent_delete,
            args=(str(self.data_path), result_queue, barrier, "S2"),
        )

        p1.start()
        p2.start()

        results = self._join_children_and_collect_results([p1, p2], result_queue)
        self.assertEqual(len(results), 2, f"expected 2 results: {results!r}")
        self.assertTrue(all(r.startswith("OK-") for r in results), f"unexpected result: {results!r}")

        rows = self.read_rows()
        self.assertEqual(len(rows), 1, "only S3 should remain")
        self.assertEqual(rows[0]["student_id"], "S3")

    def test_concurrent_delete_and_add_no_lost_updates(self):
        """A delete and a concurrent add both land cleanly without lost updates."""
        self.write_csv([
            "S1,Mathematics,95,100,2025-01-15,3,1\n",
            "S2,Physics,85,100,2025-01-16,3,1\n",
        ])

        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(2)
        result_queue = ctx.Queue()

        p_del = ctx.Process(
            target=_worker_concurrent_delete,
            args=(str(self.data_path), result_queue, barrier, "S1"),
        )
        p_add = ctx.Process(
            target=_worker_concurrent_add,
            args=(str(self.data_path), result_queue, barrier, "S3"),
        )

        p_del.start()
        p_add.start()

        results = self._join_children_and_collect_results([p_del, p_add], result_queue)
        self.assertEqual(len(results), 2, f"expected 2 results: {results!r}")
        self.assertTrue(all(r.startswith("OK-") for r in results), f"unexpected result: {results!r}")

        rows = self.read_rows()
        students = {r["student_id"] for r in rows}
        self.assertNotIn("S1", students, "S1 should have been deleted")
        self.assertIn("S2", students, "S2 should be preserved")
        self.assertIn("S3", students, "S3 should have been added")


if __name__ == "__main__":
    unittest.main()

