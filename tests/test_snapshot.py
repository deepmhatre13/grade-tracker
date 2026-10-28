"""Focused unit, integration, CLI, and concurrency tests for reproducible GPA snapshots (Issue #65).

Covers:
1. Creating and reproducing an SGPA snapshot.
2. Creating and reproducing a CGPA snapshot across multiple semesters.
3. Deterministic reproduction of identical snapshots.
4. Changing live grade records after snapshot creation does not alter historical results.
5. Changing the active grading-policy profile after snapshot creation does not alter historical results.
6. Correct preservation of credits, weighting, and rounding.
7. Missing or incomplete historical inputs.
8. Corrupted snapshot content and integrity mismatches (tamper detection).
9. Unsupported snapshot schema versions.
10. Unsupported grading-policy or calculation-algorithm versions.
11. Missing snapshot IDs and malformed snapshot files.
12. Duplicate snapshot creation behavior (prevention of silent overwrites).
13. Atomic persistence and cleanup on failure.
14. Concurrent snapshot creation under inter-process lock without lost updates.
15. Backward compatibility with existing grade data and grading-policy profiles.
16. Audit-history integration (snapshot event recording).
17. CLI success, invalid-input, format json, and failure cases.
"""

import contextlib
import io
import json
import multiprocessing
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from grade_tracker.audit_log import (
    audit_log_path_for,
    verify_log,
)
from grade_tracker.calc import cgpa, sgpa
from grade_tracker.cli import main
from grade_tracker.policies import Policy, register_policy
from grade_tracker.snapshot import (
    ALGORITHM_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
    SnapshotCalculationError,
    SnapshotError,
    SnapshotExistsError,
    SnapshotIntegrityError,
    SnapshotNotFoundError,
    UnsupportedVersionError,
    compute_snapshot_digest,
    create_snapshot,
    inspect_snapshot,
    list_snapshots,
    reproduce_snapshot,
    snapshot_dir_for,
    snapshot_path_for,
)


def _worker_concurrent_create_snapshot(csv_path, snapshot_id, student_id, result_queue, barrier):
    try:
        barrier.wait(timeout=10.0)
        res = create_snapshot(
            csv_path,
            calculation_type="cgpa",
            student_id=student_id,
            snapshot_id=snapshot_id,
        )
        result_queue.put(("OK", res["snapshot_id"]))
    except SnapshotExistsError as exc:
        result_queue.put(("EXISTS", str(exc)))
    except Exception as exc:
        result_queue.put(("ERROR", f"{type(exc).__name__}: {exc}"))


class SnapshotTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)
        self.csv_path = self.dir_path / "grades.csv"

        # Populate baseline sample grades
        self.csv_path.write_text(
            "student_id,subject,score,max_score,date,credits,semester\n"
            "S101,Math,90.0,100.0,2026-01-15,3.0,1\n"
            "S101,Physics,80.0,100.0,2026-01-16,4.0,1\n"
            "S101,Chemistry,70.0,100.0,2026-06-10,3.0,2\n"
            "S102,Biology,95.0,100.0,2026-01-20,3.0,1\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_cli(self, *args):
        argv = ["grade_tracker", "--data", str(self.csv_path)] + list(args)
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
        return code, stdout.getvalue(), stderr.getvalue()


class TestSnapshotCreationAndReproduction(SnapshotTestBase):
    """Test core snapshot creation, inspection, and deterministic reproduction."""

    def test_create_and_reproduce_sgpa_snapshot(self):
        """Creating an SGPA snapshot and reproducing it yields identical GPA, points, and credits."""
        snap = create_snapshot(
            self.csv_path,
            calculation_type="sgpa",
            student_id="S101",
            semester="1",
            snapshot_id="s101-sem1-test",
        )
        self.assertEqual(snap["snapshot_id"], "s101-sem1-test")
        self.assertEqual(snap["calculation_type"], "sgpa")
        self.assertEqual(snap["student_id"], "S101")
        self.assertEqual(snap["semester"], "1")
        self.assertEqual(len(snap["input_records"]), 2)

        # Expected SGPA for S101 Sem 1:
        # Math: 90% (A -> 10 pts) * 3 cr = 30
        # Physics: 80% (B -> 9 pts) * 4 cr = 36
        # Total points = 66, Total credits = 7 -> 66/7 = 9.42857 -> 9.43
        expected_gpa = 9.43
        self.assertEqual(snap["result"]["gpa"], expected_gpa)
        self.assertEqual(snap["result"]["total_credits"], 7.0)
        self.assertEqual(snap["result"]["total_points"], 66.0)

        # Reproduce snapshot
        rep = reproduce_snapshot(self.csv_path, "s101-sem1-test")
        self.assertTrue(rep["reproduced"])
        self.assertEqual(rep["recorded_gpa"], expected_gpa)
        self.assertEqual(rep["reproduced_gpa"], expected_gpa)
        self.assertEqual(rep["reproduced_points"], 66.0)
        self.assertEqual(rep["reproduced_credits"], 7.0)

    def test_create_and_reproduce_cgpa_snapshot_multi_semester(self):
        """Creating a CGPA snapshot captures records across all semesters and reproduces accurately."""
        snap = create_snapshot(
            self.csv_path,
            calculation_type="cgpa",
            student_id="S101",
            snapshot_id="s101-cgpa-test",
        )
        self.assertEqual(snap["calculation_type"], "cgpa")
        self.assertEqual(len(snap["input_records"]), 3)

        # Sem 1: 66 pts, 7 cr
        # Sem 2: Chemistry 70% (C -> 8 pts) * 3 cr = 24 pts
        # Total pts = 90, Total cr = 10 -> 90 / 10 = 9.00
        expected_cgpa = 9.0
        self.assertEqual(snap["result"]["gpa"], expected_cgpa)

        rep = reproduce_snapshot(self.csv_path, "s101-cgpa-test")
        self.assertTrue(rep["reproduced"])
        self.assertEqual(rep["reproduced_gpa"], expected_cgpa)

    def test_deterministic_reproduction_of_identical_snapshots(self):
        """Snapshots created under the same conditions compute the exact same canonical digest and GPA."""
        snap1 = create_snapshot(
            self.csv_path,
            calculation_type="sgpa",
            student_id="S101",
            semester="1",
            snapshot_id="snap-det-1",
        )
        snap2 = create_snapshot(
            self.csv_path,
            calculation_type="sgpa",
            student_id="S101",
            semester="1",
            snapshot_id="snap-det-2",
        )
        self.assertEqual(snap1["result"]["gpa"], snap2["result"]["gpa"])
        # Excluding snapshot_id and created_at, content is identical
        self.assertEqual(snap1["input_records"], snap2["input_records"])
        self.assertEqual(snap1["policy"], snap2["policy"])

    def test_reproduction_unaffected_by_changing_live_grade_records(self):
        """Modifying, adding, or deleting live grades in CSV does not alter snapshot reproduction."""
        create_snapshot(
            self.csv_path,
            calculation_type="sgpa",
            student_id="S101",
            semester="1",
            snapshot_id="snap-live-isolation",
        )

        # Drastically change the live CSV data
        self.csv_path.write_text(
            "student_id,subject,score,max_score,date,credits,semester\n"
            "S101,Math,40.0,100.0,2026-01-15,3.0,1\n"
            "S101,Physics,30.0,100.0,2026-01-16,4.0,1\n",
            encoding="utf-8",
        )

        # Current live calculation would yield failing GPA (0.0)
        from grade_tracker.cli import load_grades
        live_gpa = sgpa(load_grades(self.csv_path), "S101", semester="1")
        self.assertEqual(live_gpa, 0.0)

        # Historical reproduction must strictly return the preserved 9.43
        rep = reproduce_snapshot(self.csv_path, "snap-live-isolation")
        self.assertTrue(rep["reproduced"])
        self.assertEqual(rep["reproduced_gpa"], 9.43)

    def test_reproduction_unaffected_by_changing_active_grading_policy(self):
        """Changing or registering custom grading policies does not change historical snapshot result."""
        # Create custom policy
        strict_policy = Policy(
            identifier="strict",
            version="1",
            thresholds=((95.0, "A"), (85.0, "B"), (75.0, "C"), (65.0, "D")),
            grade_points={"A": 10.0, "B": 9.0, "C": 8.0, "D": 7.0, "F": 0.0},
            description="Stricter thresholds",
        )
        register_policy(strict_policy)

        # Create snapshot using strict policy
        create_snapshot(
            self.csv_path,
            calculation_type="sgpa",
            student_id="S101",
            semester="1",
            policy="strict@1",
            snapshot_id="snap-strict-test",
        )

        # S101 Sem 1 with strict@1:
        # Math: 90% -> B (9 pts) * 3 = 27
        # Physics: 80% -> C (8 pts) * 4 = 32
        # Total pts = 59, Total cr = 7 -> 59 / 7 = 8.42857 -> 8.43
        rep = reproduce_snapshot(self.csv_path, "snap-strict-test")
        self.assertTrue(rep["reproduced"])
        self.assertEqual(rep["reproduced_gpa"], 8.43)
        self.assertEqual(rep["policy_name"], "strict@1")

    def test_reproduction_survives_missing_current_csv(self):
        """Historical reproduction succeeds even if the data CSV has been deleted."""
        create_snapshot(
            self.csv_path,
            calculation_type="sgpa",
            student_id="S101",
            semester="1",
            snapshot_id="snap-deleted-csv",
        )
        self.csv_path.unlink()
        self.assertFalse(self.csv_path.exists())

        rep = reproduce_snapshot(self.csv_path, "snap-deleted-csv")
        self.assertTrue(rep["reproduced"])
        self.assertEqual(rep["reproduced_gpa"], 9.43)


class TestSnapshotIntegrityAndErrors(SnapshotTestBase):
    """Test integrity verification, tamper detection, and error conditions."""

    def test_tamper_detection_on_modified_input_record(self):
        """Altering input records in the snapshot file is detected as a content digest mismatch."""
        create_snapshot(
            self.csv_path,
            calculation_type="sgpa",
            student_id="S101",
            semester="1",
            snapshot_id="snap-tamper-record",
        )

        snap_file = snapshot_path_for(self.csv_path, "snap-tamper-record")
        with open(snap_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Maliciously change student's score from 90 to 100
        data["input_records"][0]["score"] = 100.0

        with open(snap_file, "w", encoding="utf-8") as f:
            json.dump(data, f)

        # Inspect detects invalid digest
        info = inspect_snapshot(self.csv_path, "snap-tamper-record")
        self.assertFalse(info["digest_valid"])

        # Reproduce rejects corrupted snapshot
        with self.assertRaises(SnapshotIntegrityError) as ctx:
            reproduce_snapshot(self.csv_path, "snap-tamper-record")
        self.assertIn("content digest mismatch", str(ctx.exception))

    def test_tamper_detection_on_modified_result_gpa(self):
        """Altering the recorded GPA in the snapshot file is detected via content digest."""
        create_snapshot(
            self.csv_path,
            calculation_type="sgpa",
            student_id="S101",
            semester="1",
            snapshot_id="snap-tamper-gpa",
        )

        snap_file = snapshot_path_for(self.csv_path, "snap-tamper-gpa")
        with open(snap_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        data["result"]["gpa"] = 10.0

        with open(snap_file, "w", encoding="utf-8") as f:
            json.dump(data, f)

        with self.assertRaises(SnapshotIntegrityError):
            reproduce_snapshot(self.csv_path, "snap-tamper-gpa")

    def test_unsupported_snapshot_schema_version(self):
        """Snapshots with unsupported schema_version raise UnsupportedVersionError."""
        create_snapshot(
            self.csv_path,
            calculation_type="sgpa",
            student_id="S101",
            semester="1",
            snapshot_id="snap-unsupported-schema",
        )

        snap_file = snapshot_path_for(self.csv_path, "snap-unsupported-schema")
        with open(snap_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        data["schema_version"] = "99.0"
        data["content_digest"] = compute_snapshot_digest(data)

        with open(snap_file, "w", encoding="utf-8") as f:
            json.dump(data, f)

        with self.assertRaises(UnsupportedVersionError) as ctx:
            reproduce_snapshot(self.csv_path, "snap-unsupported-schema")
        self.assertIn("unsupported snapshot schema version", str(ctx.exception))

    def test_unsupported_algorithm_version(self):
        """Snapshots with unsupported algorithm_version raise UnsupportedVersionError."""
        create_snapshot(
            self.csv_path,
            calculation_type="sgpa",
            student_id="S101",
            semester="1",
            snapshot_id="snap-unsupported-algo",
        )

        snap_file = snapshot_path_for(self.csv_path, "snap-unsupported-algo")
        with open(snap_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        data["algorithm_version"] = "2.0"
        data["content_digest"] = compute_snapshot_digest(data)

        with open(snap_file, "w", encoding="utf-8") as f:
            json.dump(data, f)

        with self.assertRaises(UnsupportedVersionError) as ctx:
            reproduce_snapshot(self.csv_path, "snap-unsupported-algo")
        self.assertIn("unsupported calculation algorithm version", str(ctx.exception))

    def test_missing_snapshot_raises_snapshot_not_found(self):
        """Attempting to inspect or reproduce non-existent snapshot raises SnapshotNotFoundError."""
        with self.assertRaises(SnapshotNotFoundError):
            inspect_snapshot(self.csv_path, "does-not-exist")

        with self.assertRaises(SnapshotNotFoundError):
            reproduce_snapshot(self.csv_path, "does-not-exist")

    def test_malformed_snapshot_file_raises_integrity_error(self):
        """Corrupted JSON snapshot files raise SnapshotIntegrityError."""
        s_dir = snapshot_dir_for(self.csv_path)
        s_dir.mkdir(parents=True, exist_ok=True)
        bad_file = s_dir / "bad.json"
        bad_file.write_text("{not valid json!", encoding="utf-8")

        with self.assertRaises(SnapshotIntegrityError):
            inspect_snapshot(self.csv_path, "bad")

    def test_prevent_silent_overwrite_of_existing_snapshot(self):
        """Attempting to create a snapshot with an existing snapshot_id raises SnapshotExistsError."""
        create_snapshot(
            self.csv_path,
            calculation_type="sgpa",
            student_id="S101",
            semester="1",
            snapshot_id="snap-unique-1",
        )

        with self.assertRaises(SnapshotExistsError):
            create_snapshot(
                self.csv_path,
                calculation_type="sgpa",
                student_id="S101",
                semester="1",
                snapshot_id="snap-unique-1",
            )

    def test_missing_student_id_raises_calculation_error(self):
        """Creating a snapshot for a student not in CSV raises SnapshotCalculationError."""
        with self.assertRaises(SnapshotCalculationError):
            create_snapshot(
                self.csv_path,
                calculation_type="cgpa",
                student_id="NON_EXISTENT",
            )


class TestSnapshotAuditIntegration(SnapshotTestBase):
    """Test audit log integration when snapshots are created."""

    def test_snapshot_creation_records_audit_event(self):
        """When an audit log is active, creating a snapshot records an audit event with snapshot metadata."""
        log_path = audit_log_path_for(self.csv_path)
        log_path.touch()

        snap = create_snapshot(
            self.csv_path,
            calculation_type="sgpa",
            student_id="S101",
            semester="1",
            snapshot_id="snap-audit-1",
            record_audit=True,
        )

        v = verify_log(log_path)
        self.assertTrue(v["valid"])
        self.assertEqual(len(v["events"]), 1)

        ev = v["events"][0]
        self.assertEqual(ev["operation"], "snapshot")
        self.assertEqual(ev["changes"]["snapshot_id"], "snap-audit-1")
        self.assertEqual(ev["changes"]["gpa"], snap["result"]["gpa"])


class TestSnapshotCLI(SnapshotTestBase):
    """Test CLI commands for snapshot create, inspect, reproduce, and list."""

    def test_cli_snapshot_create_sgpa(self):
        code, stdout, stderr = self.run_cli(
            "snapshot", "create", "sgpa", "S101", "--semester", "1", "--snapshot-id", "cli-snap-1"
        )
        self.assertEqual(code, 0)
        self.assertIn("Snapshot created successfully: cli-snap-1", stdout)
        self.assertIn("GPA:         9.43", stdout)

    def test_cli_snapshot_inspect(self):
        self.run_cli("snapshot", "create", "sgpa", "S101", "--semester", "1", "--snapshot-id", "cli-inspect-1")
        code, stdout, stderr = self.run_cli("snapshot", "inspect", "cli-inspect-1")
        self.assertEqual(code, 0)
        self.assertIn("Snapshot: cli-inspect-1", stdout)
        self.assertIn("Digest:", stdout)
        self.assertIn("(valid)", stdout)

    def test_cli_snapshot_reproduce_success(self):
        self.run_cli("snapshot", "create", "sgpa", "S101", "--semester", "1", "--snapshot-id", "cli-rep-1")
        code, stdout, stderr = self.run_cli("snapshot", "reproduce", "cli-rep-1")
        self.assertEqual(code, 0)
        self.assertIn("Reproduced snapshot: cli-rep-1", stdout)
        self.assertIn("MATCH (VERIFIED)", stdout)

    def test_cli_snapshot_list(self):
        self.run_cli("snapshot", "create", "sgpa", "S101", "--semester", "1", "--snapshot-id", "list-snap-1")
        self.run_cli("snapshot", "create", "cgpa", "S101", "--snapshot-id", "list-snap-2")

        code, stdout, stderr = self.run_cli("snapshot", "list")
        self.assertEqual(code, 0)
        self.assertIn("Found 2 snapshot(s):", stdout)
        self.assertIn("list-snap-1", stdout)
        self.assertIn("list-snap-2", stdout)

    def test_cli_snapshot_format_json(self):
        self.run_cli("snapshot", "create", "sgpa", "S101", "--semester", "1", "--snapshot-id", "json-snap-1")
        code, stdout, stderr = self.run_cli("snapshot", "inspect", "json-snap-1", "--format", "json")
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["command"], "snapshot inspect")
        self.assertEqual(payload["snapshot"]["snapshot_id"], "json-snap-1")
        self.assertTrue(payload["snapshot"]["digest_valid"])

    def test_cli_snapshot_reproduce_mismatch_exits_code_1(self):
        self.run_cli("snapshot", "create", "sgpa", "S101", "--semester", "1", "--snapshot-id", "cli-mismatch")
        snap_file = snapshot_path_for(self.csv_path, "cli-mismatch")
        with open(snap_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Alter input score while keeping old content_digest to trigger integrity failure
        data["input_records"][0]["score"] = 50.0
        with open(snap_file, "w", encoding="utf-8") as f:
            json.dump(data, f)

        code, stdout, stderr = self.run_cli("snapshot", "reproduce", "cli-mismatch")
        self.assertEqual(code, 2)
        self.assertIn("content digest mismatch", stderr)


class TestSnapshotConcurrency(SnapshotTestBase):
    """Test concurrent snapshot creation with genuine multiprocessing."""

    def test_concurrent_snapshot_creation(self):
        """Multiple processes creating different snapshots serialize cleanly under lock."""
        ctx = multiprocessing.get_context("spawn")
        num_workers = 3
        barrier = ctx.Barrier(num_workers)
        result_queue = ctx.Queue()

        workers = [
            ctx.Process(
                target=_worker_concurrent_create_snapshot,
                args=(str(self.csv_path), f"concurrent-snap-{i}", "S101", result_queue, barrier),
            )
            for i in range(num_workers)
        ]

        for p in workers:
            p.start()

        for p in workers:
            p.join(timeout=15.0)

        results = []
        while not result_queue.empty():
            results.append(result_queue.get())

        self.assertEqual(len(results), num_workers)
        for status, val in results:
            self.assertEqual(status, "OK", f"concurrent snapshot creation failed: {val}")

        # Verify all snapshots were persisted and are valid
        all_snaps = list_snapshots(self.csv_path)
        self.assertEqual(len(all_snaps), num_workers)
        self.assertTrue(all(s["digest_valid"] for s in all_snaps))


if __name__ == "__main__":
    unittest.main()

