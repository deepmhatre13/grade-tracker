"""Tests for versioned CSV schema migrations (GitHub Issue #43).

Covers:
1. Detection of every supported legacy schema (0.1, 0.2, 0.3) and canonical schema (1.0).
2. Every supported migration path and correct ordering of migration steps.
3. Current-schema detection and no-op migration (already current).
4. Missing optional credits and semester fields with legacy defaults (3.0, 1) preserved.
5. Preservation of all populated fields, custom credits/semesters, and record ordering.
6. UTF-8 BOM handling and normalization.
7. Unknown schema versions, ambiguous headers, duplicate columns, and unexpected/missing columns.
8. Malformed CSV, malformed records, and integrity validation failures before migration.
9. Dry-run output and byte-for-byte preservation of the source (no temp files).
10. Successful migration to the canonical schema.
11. Repeated invocation and idempotency.
12. Write errors, disk/write failures, and atomic replacement failures.
13. Temporary-file cleanup on success and failure.
14. Original-file byte-for-byte preservation whenever migration fails.
15. CLI exit codes, stdout, stderr, global --data, and actionable diagnostics.
16. Extensibility of migration registry and custom migration registration.
17. Concurrent migration processes serialized under inter-process lock.
18. Concurrent migration and grade-writing operations with no lost updates.
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
    CANONICAL_COLUMNS,
    CURRENT_CSV_SCHEMA_VERSION,
    DEFAULT_DATA_PATH,
    _InterProcessLock,
    _lock_path_for,
    detect_csv_schema_version,
    get_migration_plan,
    main,
    migrate_grades,
    register_migration,
    save_grade,
    SCHEMA_MIGRATION_REGISTRY,
)

HEADER_V0_1 = "student_id,subject,score,max_score,date\n"
HEADER_V0_2 = "student_id,subject,score,max_score,date,semester\n"
HEADER_V0_3 = "student_id,subject,score,max_score,date,credits\n"
HEADER_V1_0 = "student_id,subject,score,max_score,date,credits,semester\n"


# Force the ``spawn`` start method so these tests exercise independent OS
# processes (the only model available on Windows for ``multiprocessing``).
try:
    multiprocessing.set_start_method("spawn", force=True)
except RuntimeError:
    pass


def _worker_migrate(csv_path, result_queue, barrier):
    """Wait for barrier then perform migrate_grades under lock."""
    try:
        barrier.wait(timeout=20.0)
        res = migrate_grades(csv_path=csv_path)
        result_queue.put(f"OK-MIGRATE-{res['from_version']}->{res['to_version']}")
    except Exception as exc:
        result_queue.put(f"ERROR: {exc!r}")


def _worker_add_grade(csv_path, student_id, subject, result_queue, barrier):
    """Wait for barrier then perform save_grade under lock, retrying if migration has not run yet."""
    try:
        barrier.wait(timeout=10.0)
        # If save_grade wins the race before migration completes, save_grade will
        # legitimately fail with 'unexpected schema' on legacy file. In a concurrent
        # environment, the writer retries until migration completes.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                save_grade(
                    student_id=student_id,
                    subject=subject,
                    score=95.0,
                    max_score=100.0,
                    date="2026-10-20",
                    credits=3.0,
                    semester="1",
                    csv_path=str(csv_path),
                )
                result_queue.put(f"OK-ADD-{student_id}")
                return
            except ValueError as val_err:
                if "unexpected schema" in str(val_err):
                    time.sleep(0.05)
                    continue
                raise
        result_queue.put("ERROR: timeout waiting for migration before adding grade")
    except Exception as exc:
        result_queue.put(f"ERROR: {exc!r}")


class MigrationTestCase(unittest.TestCase):
    """Base test case with helper methods for test setup and CLI invocation."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_path = Path(self.temp_dir.name) / "grades.csv"

    def write_csv(self, header, rows):
        text = header + "".join(rows)
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
        argv = ["grade_tracker", "--data", str(self.data_path), "migrate", *args]
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


class TestSchemaDetectionAndPlan(MigrationTestCase):
    """Unit tests for schema detection and migration registry planning."""

    def test_detect_canonical_schema(self):
        ver, method = detect_csv_schema_version(CANONICAL_COLUMNS)
        self.assertEqual(ver, "1.0")
        self.assertEqual(method, "canonical header")

    def test_detect_legacy_0_1(self):
        header = ["student_id", "subject", "score", "max_score", "date"]
        ver, method = detect_csv_schema_version(header)
        self.assertEqual(ver, "0.1")
        self.assertEqual(method, "legacy header")

    def test_detect_legacy_0_2(self):
        header = ["student_id", "subject", "score", "max_score", "date", "semester"]
        ver, method = detect_csv_schema_version(header)
        self.assertEqual(ver, "0.2")
        self.assertEqual(method, "legacy header")

    def test_detect_legacy_0_3(self):
        header = ["student_id", "subject", "score", "max_score", "date", "credits"]
        ver, method = detect_csv_schema_version(header)
        self.assertEqual(ver, "0.3")
        self.assertEqual(method, "legacy header")

    def test_detect_utf8_bom_normalized(self):
        header = ["\ufeffstudent_id", "subject", "score", "max_score", "date"]
        ver, _ = detect_csv_schema_version(header)
        self.assertEqual(ver, "0.1")

    def test_detect_empty_or_blank_header_fails(self):
        with self.assertRaises(ValueError) as cm:
            detect_csv_schema_version([])
        self.assertIn("file is empty", str(cm.exception))

        with self.assertRaises(ValueError) as cm:
            detect_csv_schema_version(["", "  "])
        self.assertIn("header is blank", str(cm.exception))

    def test_detect_duplicate_columns_fails(self):
        header = ["student_id", "subject", "score", "max_score", "date", "score"]
        with self.assertRaises(ValueError) as cm:
            detect_csv_schema_version(header)
        self.assertIn("duplicate header column(s)", str(cm.exception))

    def test_detect_unexpected_columns_fails(self):
        header = ["student_id", "subject", "score", "max_score", "date", "extra_col"]
        with self.assertRaises(ValueError) as cm:
            detect_csv_schema_version(header)
        self.assertIn("unexpected column(s)", str(cm.exception))

    def test_detect_missing_required_columns_fails(self):
        header = ["student_id", "subject", "date"]
        with self.assertRaises(ValueError) as cm:
            detect_csv_schema_version(header)
        self.assertIn("missing required column(s)", str(cm.exception))

    def test_detect_unknown_permutation_fails(self):
        header = ["date", "max_score", "score", "subject", "student_id"]
        with self.assertRaises(ValueError) as cm:
            detect_csv_schema_version(header)
        self.assertIn("unrecognized header layout", str(cm.exception))

    def test_migration_plan_generation(self):
        plan_0_1 = get_migration_plan("0.1", "1.0")
        self.assertEqual(len(plan_0_1), 1)
        self.assertEqual(plan_0_1[0]["from"], "0.1")
        self.assertEqual(plan_0_1[0]["to"], "1.0")

        plan_curr = get_migration_plan("1.0", "1.0")
        self.assertEqual(plan_curr, [])

    def test_extensibility_custom_migration_registration(self):
        # Register a mock transition 1.0 -> 2.0 and verify plan resolves
        @register_migration("1.0", "2.0", description="Mock transition to 2.0")
        def _migrate_1_0_to_2_0(row, header):
            return row + ["active"]

        try:
            plan = get_migration_plan("0.1", "2.0")
            self.assertEqual(len(plan), 2)
            self.assertEqual(plan[0]["from"], "0.1")
            self.assertEqual(plan[0]["to"], "1.0")
            self.assertEqual(plan[1]["from"], "1.0")
            self.assertEqual(plan[1]["to"], "2.0")
        finally:
            SCHEMA_MIGRATION_REGISTRY.pop(("1.0", "2.0"), None)


class TestMigrationExecutionAndPreservation(MigrationTestCase):
    """Test actual migrations from legacy schemas to canonical 1.0."""

    def test_migrate_from_0_1_defaults_credits_and_semester(self):
        self.write_csv(
            HEADER_V0_1,
            [
                "S1,Mathematics,95,100,2025-01-15\n",
                "S2,Physics,85,100,2025-01-16\n",
            ],
        )
        code, stdout, stderr = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Migration completed successfully", stdout)
        self.assertIn("From schema version: 0.1", stdout)
        self.assertIn("To schema version: 1.0", stdout)

        rows = self.read_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["student_id"], "S1")
        self.assertEqual(rows[0]["subject"], "Mathematics")
        self.assertEqual(rows[0]["score"], "95")
        self.assertEqual(rows[0]["max_score"], "100")
        self.assertEqual(rows[0]["date"], "2025-01-15")
        self.assertEqual(rows[0]["credits"], "3.0")
        self.assertEqual(rows[0]["semester"], "1")

        self.assertEqual(rows[1]["student_id"], "S2")
        self.assertEqual(rows[1]["credits"], "3.0")
        self.assertEqual(rows[1]["semester"], "1")

        # Verify header is canonical
        with self.data_path.open(newline="", encoding="utf-8") as f:
            header_line = f.readline()
        self.assertEqual(header_line.strip(), HEADER_V1_0.strip())

    def test_migrate_from_0_2_preserves_semester_and_defaults_credits(self):
        self.write_csv(
            HEADER_V0_2,
            [
                "S1,Mathematics,95,100,2025-01-15,3\n",
                "S2,Physics,85,100,2025-01-16,4\n",
            ],
        )
        code, stdout, stderr = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")

        rows = self.read_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["semester"], "3")
        self.assertEqual(rows[0]["credits"], "3.0")
        self.assertEqual(rows[1]["semester"], "4")
        self.assertEqual(rows[1]["credits"], "3.0")

    def test_migrate_from_0_3_preserves_credits_and_defaults_semester(self):
        self.write_csv(
            HEADER_V0_3,
            [
                "S1,Mathematics,95,100,2025-01-15,4.0\n",
                "S2,Physics,85,100,2025-01-16,2.5\n",
            ],
        )
        code, stdout, stderr = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")

        rows = self.read_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["credits"], "4.0")
        self.assertEqual(rows[0]["semester"], "1")
        self.assertEqual(rows[1]["credits"], "2.5")
        self.assertEqual(rows[1]["semester"], "1")

    def test_preserves_exact_record_ordering_and_populated_values(self):
        self.write_csv(
            HEADER_V0_1,
            [
                "S3,History,88,100,2025-02-01\n",
                "S1,Mathematics,92,100,2025-01-15\n",
                "S2,Physics,77,100,2025-01-20\n",
            ],
        )
        code, _, _ = self.run_cli()
        self.assertEqual(code, 0)

        rows = self.read_rows()
        self.assertEqual([r["student_id"] for r in rows], ["S3", "S1", "S2"])
        self.assertEqual([r["subject"] for r in rows], ["History", "Mathematics", "Physics"])

    def test_already_current_schema_is_noop(self):
        self.write_csv(
            HEADER_V1_0,
            ["S1,Mathematics,95,100,2025-01-15,3.0,1\n"],
        )
        before = self.file_bytes()

        code, stdout, stderr = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("already at the current schema version (1.0)", stdout)
        self.assertIn("No migration needed", stdout)
        self.assertEqual(self.file_bytes(), before)

    def test_idempotent_repeated_migration(self):
        self.write_csv(
            HEADER_V0_1,
            ["S1,Mathematics,95,100,2025-01-15\n"],
        )
        # First migration applies
        code1, stdout1, _ = self.run_cli()
        self.assertEqual(code1, 0)
        self.assertIn("Migration completed successfully", stdout1)
        migrated_bytes = self.file_bytes()

        # Second migration reports already current
        code2, stdout2, _ = self.run_cli()
        self.assertEqual(code2, 0)
        self.assertIn("already at the current schema version (1.0)", stdout2)
        self.assertEqual(self.file_bytes(), migrated_bytes)


class TestDryRunAndPreview(MigrationTestCase):
    """Test dry-run behavior and invariants."""

    def test_dry_run_reports_preview_without_modifying_file(self):
        self.write_csv(
            HEADER_V0_1,
            [
                "S1,Mathematics,95,100,2025-01-15\n",
                "S2,Physics,85,100,2025-01-16\n",
            ],
        )
        before = self.file_bytes()

        code, stdout, stderr = self.run_cli("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Dry run: Migration preview", stdout)
        self.assertIn("Detected schema version: 0.1", stdout)
        self.assertIn("Target schema version: 1.0", stdout)
        self.assertIn("Records to process: 2 records", stdout)
        self.assertIn("Add optional 'credits' (default: 3.0) and 'semester' (default: '1')", stdout)
        self.assertIn("Status: no changes were made (dry run)", stdout)

        # File must be byte-for-byte unchanged and no temp files left
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(self.temp_files(), [])

    def test_dry_run_on_already_current_file(self):
        self.write_csv(
            HEADER_V1_0,
            ["S1,Mathematics,95,100,2025-01-15,3.0,1\n"],
        )
        before = self.file_bytes()

        code, stdout, stderr = self.run_cli("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("already at the current schema version (1.0)", stdout)
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(self.temp_files(), [])


class TestValidationAndFailureAtomicity(MigrationTestCase):
    """Test integrity validation, failure handling, and rollback guarantees."""

    def test_reject_missing_file(self):
        non_existent = Path(self.temp_dir.name) / "missing.csv"
        argv = ["grade_tracker", "--data", str(non_existent), "migrate"]
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

        self.assertEqual(code, 1)
        self.assertIn("data file does not exist", stderr.getvalue())

    def test_reject_empty_file(self):
        self.data_path.write_text("", encoding="utf-8")
        code, stdout, stderr = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("file is empty", stderr)

    def test_reject_invalid_grade_records_in_legacy_file(self):
        # Legacy file has invalid score "not_a_number"
        self.write_csv(
            HEADER_V0_1,
            ["S1,Mathematics,not_a_number,100,2025-01-15\n"],
        )
        before = self.file_bytes()

        code, stdout, stderr = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("cannot migrate invalid CSV", stderr)
        self.assertIn("is not a numeric score", stderr)
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(self.temp_files(), [])

    def test_reject_duplicate_columns_header(self):
        bad_header = "student_id,subject,score,max_score,date,score\n"
        self.data_path.write_text(bad_header + "S1,Math,90,100,2025-01-15,90\n", encoding="utf-8")
        before = self.file_bytes()

        code, stdout, stderr = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("duplicate header column(s)", stderr)
        self.assertEqual(self.file_bytes(), before)

    def test_reject_unrecognized_header(self):
        bad_header = "student_id,subject,score,max_score,date,extra_unexpected\n"
        self.data_path.write_text(bad_header + "S1,Math,90,100,2025-01-15,foo\n", encoding="utf-8")
        before = self.file_bytes()

        code, stdout, stderr = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("unexpected column(s)", stderr)
        self.assertEqual(self.file_bytes(), before)

    def test_simulated_write_failure_preserves_original(self):
        self.write_csv(
            HEADER_V0_1,
            ["S1,Mathematics,95,100,2025-01-15\n"],
        )
        before = self.file_bytes()

        with mock.patch("grade_tracker.cli.csv.writer") as mock_writer:
            mock_writer.side_effect = OSError("simulated disk full")
            code, stdout, stderr = self.run_cli()

        self.assertEqual(code, 1)
        self.assertIn("simulated disk full", stderr)
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(self.temp_files(), [])

    def test_simulated_replace_failure_preserves_original(self):
        self.write_csv(
            HEADER_V0_1,
            ["S1,Mathematics,95,100,2025-01-15\n"],
        )
        before = self.file_bytes()

        with mock.patch("os.replace", side_effect=OSError("simulated permission error")):
            code, stdout, stderr = self.run_cli()

        self.assertEqual(code, 1)
        self.assertIn("simulated permission error", stderr)
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(self.temp_files(), [])

    def test_paths_containing_spaces_work_correctly(self):
        spaced_dir = Path(self.temp_dir.name) / "directory with spaces"
        spaced_dir.mkdir()
        spaced_csv = spaced_dir / "my grades file.csv"
        spaced_csv.write_text(HEADER_V0_1 + "S1,Mathematics,95,100,2025-01-15\n", encoding="utf-8")

        argv = ["grade_tracker", "--data", str(spaced_csv), "migrate"]
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
        with spaced_csv.open(newline="", encoding="utf-8") as f:
            header = f.readline()
        self.assertEqual(header.strip(), HEADER_V1_0.strip())


class TestMigrationConcurrency(MigrationTestCase):
    """Multi-process concurrency tests for migrate operations."""

    def _join_children_and_collect_results(self, children, result_queue):
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline and len(children) > 0:
            alive = [p for p in children if p.is_alive()]
            for p in alive:
                p.join(timeout=max(0.1, deadline - time.monotonic()))
            children = [p for p in children if p.is_alive()]
        if children:
            for p in children:
                p.terminate()
            self.fail("concurrent workers did not finish within timeout")
        results = []
        while not result_queue.empty():
            try:
                results.append(result_queue.get_nowait())
            except Exception:
                break
        return results

    def test_two_concurrent_migrations_serialize_cleanly(self):
        """Two concurrent migrations serialize under lock; exactly one transforms, one sees 1.0."""
        self.write_csv(
            HEADER_V0_1,
            ["S1,Mathematics,95,100,2025-01-15\n"],
        )

        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(2)
        result_queue = ctx.Queue()

        p1 = ctx.Process(target=_worker_migrate, args=(str(self.data_path), result_queue, barrier))
        p2 = ctx.Process(target=_worker_migrate, args=(str(self.data_path), result_queue, barrier))

        p1.start()
        p2.start()

        results = self._join_children_and_collect_results([p1, p2], result_queue)
        self.assertEqual(len(results), 2, f"unexpected results: {results!r}")
        self.assertTrue(all(r.startswith("OK-") for r in results), f"workers failed: {results!r}")

        # One process migrated 0.1->1.0 and the other detected 1.0->1.0
        self.assertIn("OK-MIGRATE-0.1->1.0", results)
        self.assertIn("OK-MIGRATE-1.0->1.0", results)

        rows = self.read_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["credits"], "3.0")
        self.assertEqual(rows[0]["semester"], "1")

    def test_concurrent_migration_and_add_grade_no_lost_updates(self):
        """Concurrent migration and grade addition both succeed without losing records."""
        self.write_csv(
            HEADER_V0_1,
            ["S1,Mathematics,95,100,2025-01-15\n"],
        )

        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(2)
        result_queue = ctx.Queue()

        p_mig = ctx.Process(target=_worker_migrate, args=(str(self.data_path), result_queue, barrier))
        p_add = ctx.Process(target=_worker_add_grade, args=(str(self.data_path), "S2", "Physics", result_queue, barrier))

        p_mig.start()
        p_add.start()

        results = self._join_children_and_collect_results([p_mig, p_add], result_queue)
        self.assertEqual(len(results), 2, f"unexpected results: {results!r}")
        self.assertTrue(all(r.startswith("OK-") for r in results), f"workers failed: {results!r}")

        rows = self.read_rows()
        self.assertEqual(len(rows), 2)
        students = {r["student_id"] for r in rows}
        self.assertEqual(students, {"S1", "S2"})

        # Header must be canonical 1.0
        with self.data_path.open(newline="", encoding="utf-8") as f:
            header_line = f.readline()
        self.assertEqual(header_line.strip(), HEADER_V1_0.strip())


if __name__ == "__main__":
    unittest.main()
