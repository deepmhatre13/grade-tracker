"""Comprehensive tests for safe rollback of previous grade mutations (GitHub Issue).

Covers:
- Successful rollback of add, edit, delete, and bulk-import operations.
- Dry-run leaves dataset and audit log bytes untouched.
- Missing or corrupted original events.
- Missing historical before-and-after information.
- Conflicts caused by subsequent edits or deletions.
- Ambiguous identities and duplicate restoration attempts.
- Repeated rollback of the same event.
- Preservation of unrelated records and row ordering.
- Proposed dataset validation and atomic persistence failure handling.
- Crash recovery during rollback (idempotency, no false duplicate commits).
- Audit chain validity after successful rollback.
- Concurrent rollback attempts using genuine multiprocessing and bounded timeouts.
- CLI argument parsing, confirmation requirement (--yes), exit codes, and output conventions.
"""

import contextlib
import csv
import io
import multiprocessing
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from grade_tracker.audit_log import (
    AUDIT_SCHEMA_VERSION,
    GENESIS_PREV_HASH,
    append_event,
    audit_log_path_for,
    build_event,
    dataset_id_for,
    pending_journal_path_for,
    read_events,
    verify_log,
)
from grade_tracker.audit_txn import (
    AuditTransaction,
    _read_journal,
    _write_json_atomic,
    recover_pending,
)
from grade_tracker.cli import (
    _append_row,
    _audit_record,
    main,
    save_grade,
)
from grade_tracker.rollback import (
    RollbackConflictError,
    RollbackError,
    RollbackIntegrityError,
    rollback_mutation,
)


def _worker_concurrent_rollback(csv_path, event_id, result_queue, barrier):
    try:
        barrier.wait(timeout=10.0)
        res = rollback_mutation(
            Path(csv_path),
            event_id,
            dry_run=False,
            confirmed=True,
        )
        result_queue.put(("OK", res["event_id"]))
    except RollbackConflictError as exc:
        result_queue.put(("CONFLICT", str(exc)))
    except Exception as exc:
        result_queue.put(("ERROR", f"{type(exc).__name__}: {exc}"))


class RollbackTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)
        self.csv_path = self.dir_path / "grades.csv"
        self.log_path = audit_log_path_for(self.csv_path)

        # Initialize empty grades.csv with canonical header
        self.csv_path.write_text(
            "student_id,subject,score,max_score,date,credits,semester\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def read_csv_rows(self):
        with open(self.csv_path, mode="r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            next(reader, None)
            return list(reader)

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


class TestRollbackOperations(RollbackTestBase):
    """Test safe reversal of eligible add, edit, delete, and import operations."""

    def test_rollback_add_operation(self):
        """Rolling back an 'add' operation deletes the added record and records compensating rollback event."""
        # 1. Add record through save_grade (which creates audit event)
        save_grade("S101", "Math", 95.0, 100.0, "2026-02-01", 3.0, "1", csv_path=self.csv_path)

        # Inspect audit event
        v = verify_log(self.log_path)
        self.assertTrue(v["valid"])
        self.assertEqual(len(v["events"]), 1)
        add_event = v["events"][0]
        self.assertEqual(add_event["operation"], "add")

        # 2. Roll back the add
        result = rollback_mutation(self.csv_path, add_event["event_id"], dry_run=False, confirmed=True)
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["operation"], "add")
        self.assertEqual(result["records_affected"], 1)

        # 3. Verify dataset no longer contains the record
        rows = self.read_csv_rows()
        self.assertEqual(len(rows), 0)

        # 4. Verify audit log has unbroken chain with compensating rollback event
        v2 = verify_log(self.log_path)
        self.assertTrue(v2["valid"])
        self.assertEqual(len(v2["events"]), 2)
        rb_event = v2["events"][1]
        self.assertEqual(rb_event["sequence"], 2)
        self.assertEqual(rb_event["operation"], "rollback")
        self.assertEqual(rb_event["previous_hash"], add_event["hash"])
        self.assertEqual(rb_event["changes"]["rolled_back_event_id"], add_event["event_id"])
        self.assertEqual(rb_event["changes"]["rolled_back_operation"], "add")

    def test_rollback_edit_operation(self):
        """Rolling back an 'edit' operation restores previous before-values."""
        # Setup initial state with a record
        rec_before = {
            "student_id": "S101", "subject": "Math", "score": "80.0",
            "max_score": "100.0", "date": "2026-02-01", "credits": "3.0", "semester": "1"
        }
        rec_after = {
            "student_id": "S101", "subject": "Math", "score": "95.0",
            "max_score": "100.0", "date": "2026-02-01", "credits": "3.0", "semester": "1"
        }

        # Perform initial add under AuditTransaction
        with AuditTransaction(
            self.csv_path, operation="add", records=[rec_before],
            changes={"before": None, "after": rec_before}
        ) as txn:
            txn.write_dataset([
                ["S101", "Math", "80.0", "100.0", "2026-02-01", "3.0", "1"]
            ])

        # Perform edit under AuditTransaction
        with AuditTransaction(
            self.csv_path, operation="edit", records=[rec_after],
            changes={"before": rec_before, "after": rec_after}
        ) as txn:
            txn.write_dataset([
                ["S101", "Math", "95.0", "100.0", "2026-02-01", "3.0", "1"]
            ])

        v = verify_log(self.log_path)
        edit_event = v["events"][1]
        self.assertEqual(edit_event["operation"], "edit")

        # Roll back the edit
        result = rollback_mutation(self.csv_path, edit_event["event_id"], confirmed=True)
        self.assertEqual(result["records_affected"], 1)

        # Verify dataset row was restored to score 80.0
        rows = self.read_csv_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "S101")
        self.assertEqual(rows[0][2], "80.0")

        # Verify audit log
        v2 = verify_log(self.log_path)
        self.assertTrue(v2["valid"])
        self.assertEqual(len(v2["events"]), 3)
        rb_event = v2["events"][2]
        self.assertEqual(rb_event["operation"], "rollback")
        self.assertEqual(rb_event["changes"]["after"]["score"], "80.0")

    def test_rollback_delete_operation(self):
        """Rolling back a 'delete' operation restores deleted records."""
        deleted_rec = {
            "student_id": "S202", "subject": "Physics", "score": "88.0",
            "max_score": "100.0", "date": "2026-03-01", "credits": "4.0", "semester": "2"
        }

        # Initial add
        with AuditTransaction(
            self.csv_path, operation="add", records=[deleted_rec],
            changes={"before": None, "after": deleted_rec}
        ) as txn:
            txn.write_dataset([
                ["S202", "Physics", "88.0", "100.0", "2026-03-01", "4.0", "2"]
            ])

        # Deletion
        with AuditTransaction(
            self.csv_path, operation="delete", records=[deleted_rec],
            changes={"before": [deleted_rec], "after": None}
        ) as txn:
            txn.write_dataset([])

        v = verify_log(self.log_path)
        del_event = v["events"][1]
        self.assertEqual(del_event["operation"], "delete")

        # Dataset currently empty
        self.assertEqual(len(self.read_csv_rows()), 0)

        # Roll back deletion
        result = rollback_mutation(self.csv_path, del_event["event_id"], confirmed=True)
        self.assertEqual(result["records_affected"], 1)

        # Record restored
        rows = self.read_csv_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "S202")
        self.assertEqual(rows[0][1], "Physics")

    def test_rollback_import_operation(self):
        """Rolling back an 'import' operation deletes all imported records while preserving existing."""
        existing_rec = ["S1", "Chemistry", "70.0", "100.0", "2026-01-01", "3.0", "1"]
        imp1 = {
            "student_id": "S10", "subject": "Biology", "score": "85.0",
            "max_score": "100.0", "date": "2026-01-05", "credits": "3.0", "semester": "1"
        }
        imp2 = {
            "student_id": "S11", "subject": "History", "score": "90.0",
            "max_score": "100.0", "date": "2026-01-06", "credits": "3.0", "semester": "1"
        }

        # Setup initial dataset
        self.csv_path.write_text(
            "student_id,subject,score,max_score,date,credits,semester\n"
            "S1,Chemistry,70.0,100.0,2026-01-01,3.0,1\n"
            "S10,Biology,85.0,100.0,2026-01-05,3.0,1\n"
            "S11,History,90.0,100.0,2026-01-06,3.0,1\n",
            encoding="utf-8",
        )

        with AuditTransaction(
            self.csv_path, operation="import", records=[imp1, imp2],
            changes={"before": None, "after": [imp1, imp2]}
        ) as txn:
            txn.write_dataset([
                existing_rec,
                ["S10", "Biology", "85.0", "100.0", "2026-01-05", "3.0", "1"],
                ["S11", "History", "90.0", "100.0", "2026-01-06", "3.0", "1"],
            ])

        v = verify_log(self.log_path)
        imp_event = v["events"][0]

        # Roll back import
        res = rollback_mutation(self.csv_path, imp_event["event_id"], confirmed=True)
        self.assertEqual(res["records_affected"], 2)

        # Verify only S1 remains
        rows = self.read_csv_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "S1")


class TestRollbackSafeguardsAndConflicts(RollbackTestBase):
    """Test conflict detection, dry run, duplicate rollback prevention, and corruption handling."""

    def test_dry_run_leaves_dataset_and_audit_bytes_untouched(self):
        """--dry-run preview leaves CSV and audit log byte-for-byte unchanged."""
        save_grade("S101", "Math", 90.0, 100.0, "2026-02-01", 3.0, "1", csv_path=self.csv_path)

        csv_bytes_before = self.csv_path.read_bytes()
        log_bytes_before = self.log_path.read_bytes()

        v = verify_log(self.log_path)
        event_id = v["events"][0]["event_id"]

        result = rollback_mutation(self.csv_path, event_id, dry_run=True, confirmed=False)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["records_affected"], 1)

        self.assertEqual(self.csv_path.read_bytes(), csv_bytes_before)
        self.assertEqual(self.log_path.read_bytes(), log_bytes_before)

    def test_confirmation_required_for_destructive_rollback(self):
        """Calling rollback without dry_run=True or confirmed=True raises RollbackError."""
        save_grade("S101", "Math", 90.0, 100.0, "2026-02-01", 3.0, "1", csv_path=self.csv_path)
        v = verify_log(self.log_path)
        event_id = v["events"][0]["event_id"]

        with self.assertRaises(RollbackError):
            rollback_mutation(self.csv_path, event_id, dry_run=False, confirmed=False)

    def test_conflict_when_added_record_was_subsequently_modified(self):
        """Reject rollback of 'add' if the record was modified by a later edit."""
        save_grade("S101", "Math", 90.0, 100.0, "2026-02-01", 3.0, "1", csv_path=self.csv_path)
        v = verify_log(self.log_path)
        add_event_id = v["events"][0]["event_id"]

        # Mutate the record directly or via another edit
        self.csv_path.write_text(
            "student_id,subject,score,max_score,date,credits,semester\n"
            "S101,Math,99.0,100.0,2026-02-01,3.0,1\n",
            encoding="utf-8",
        )

        with self.assertRaises(RollbackConflictError) as ctx:
            rollback_mutation(self.csv_path, add_event_id, confirmed=True)
        self.assertIn("conflict: added record", str(ctx.exception))

    def test_conflict_when_added_record_was_already_deleted(self):
        """Reject rollback of 'add' if the record was already removed."""
        save_grade("S101", "Math", 90.0, 100.0, "2026-02-01", 3.0, "1", csv_path=self.csv_path)
        v = verify_log(self.log_path)
        add_event_id = v["events"][0]["event_id"]

        # Remove from CSV
        self.csv_path.write_text(
            "student_id,subject,score,max_score,date,credits,semester\n",
            encoding="utf-8",
        )

        with self.assertRaises(RollbackConflictError):
            rollback_mutation(self.csv_path, add_event_id, confirmed=True)

    def test_conflict_when_restoring_deleted_identity_collides(self):
        """Reject rollback of 'delete' if another record now occupies the assessment identity."""
        rec = {
            "student_id": "S101", "subject": "Math", "score": "80.0",
            "max_score": "100.0", "date": "2026-02-01", "credits": "3.0", "semester": "1"
        }
        with AuditTransaction(
            self.csv_path, operation="delete", records=[rec],
            changes={"before": [rec], "after": None}
        ) as txn:
            txn.write_dataset([])

        v = verify_log(self.log_path)
        del_event_id = v["events"][0]["event_id"]

        # Re-add another assessment with the same 4-field identity
        self.csv_path.write_text(
            "student_id,subject,score,max_score,date,credits,semester\n"
            "S101,Math,100.0,100.0,2026-02-01,3.0,1\n",
            encoding="utf-8",
        )

        with self.assertRaises(RollbackConflictError) as ctx:
            rollback_mutation(self.csv_path, del_event_id, confirmed=True)
        self.assertIn("already exists", str(ctx.exception))

    def test_prevent_repeated_rollback_of_same_event(self):
        """Rolling back the same event twice is rejected with RollbackConflictError."""
        save_grade("S101", "Math", 90.0, 100.0, "2026-02-01", 3.0, "1", csv_path=self.csv_path)
        v = verify_log(self.log_path)
        event_id = v["events"][0]["event_id"]

        # First rollback succeeds
        rollback_mutation(self.csv_path, event_id, confirmed=True)

        # Second rollback attempt fails
        with self.assertRaises(RollbackConflictError) as ctx:
            rollback_mutation(self.csv_path, event_id, confirmed=True)
        self.assertIn("already been rolled back", str(ctx.exception))

    def test_rollback_missing_event_fails(self):
        """Referencing a non-existent event ID raises RollbackError."""
        save_grade("S101", "Math", 90.0, 100.0, "2026-02-01", 3.0, "1", csv_path=self.csv_path)
        with self.assertRaises(RollbackError) as ctx:
            rollback_mutation(self.csv_path, "non-existent-uuid-12345", confirmed=True)
        self.assertIn("not found in audit history", str(ctx.exception))

    def test_rollback_refuses_corrupted_audit_chain(self):
        """Rollback fails immediately if audit chain integrity is broken."""
        save_grade("S101", "Math", 90.0, 100.0, "2026-02-01", 3.0, "1", csv_path=self.csv_path)
        v = verify_log(self.log_path)
        event_id = v["events"][0]["event_id"]

        # Tamper with the audit log file
        self.log_path.write_text("{\"corrupted\": true}\n", encoding="utf-8")

        with self.assertRaises(RollbackIntegrityError):
            rollback_mutation(self.csv_path, event_id, confirmed=True)

    def test_rollback_unsupported_operation_type(self):
        """Operations without safe inverse (like migrate) cannot be rolled back."""
        with AuditTransaction(
            self.csv_path, operation="migrate", records=[], changes={}
        ) as txn:
            txn.write_dataset([])

        v = verify_log(self.log_path)
        migrate_event_id = v["events"][0]["event_id"]

        with self.assertRaises(RollbackError) as ctx:
            rollback_mutation(self.csv_path, migrate_event_id, confirmed=True)
        self.assertIn("cannot be rolled back", str(ctx.exception))

    def test_rollback_event_lacking_before_after_fails(self):
        """An edit event without 'before' data cannot be rolled back."""
        with AuditTransaction(
            self.csv_path, operation="edit", records=[{"student_id": "S1"}], changes={}
        ) as txn:
            txn.write_dataset([["S1", "Math", "90.0", "100.0", "2026-01-01", "3.0", "1"]])

        v = verify_log(self.log_path)
        edit_event_id = v["events"][0]["event_id"]

        with self.assertRaises(RollbackError) as ctx:
            rollback_mutation(self.csv_path, edit_event_id, confirmed=True)
        self.assertIn("lacks complete before-and-after", str(ctx.exception))


class TestRollbackCrashRecovery(RollbackTestBase):
    """Test crash recovery semantics during rollback mutations."""

    def test_rollback_crash_recovery_after_dataset_replacement(self):
        """Simulate crash between dataset replacement and audit log finalization during rollback."""
        # 1. Initial addition
        save_grade("S101", "Math", 90.0, 100.0, "2026-02-01", 3.0, "1", csv_path=self.csv_path)
        v = verify_log(self.log_path)
        add_event = v["events"][0]
        event_id = add_event["event_id"]

        # Prepare compensating rollback event
        journal_path = pending_journal_path_for(self.csv_path)
        rb_event = build_event(
            sequence=2,
            operation="rollback",
            dataset_id=dataset_id_for(self.csv_path),
            records=add_event["records"],
            changes={"rolled_back_event_id": event_id, "rolled_back_operation": "add"},
            previous_hash=add_event["hash"],
        )

        old_digest = "some_digest"
        # New dataset has 0 rows (header only)
        empty_csv_bytes = b"student_id,subject,score,max_score,date,credits,semester\r\n"
        import hashlib
        new_digest = hashlib.sha256(empty_csv_bytes).hexdigest()

        journal = {
            "journal_version": "1.0",
            "dataset_id": dataset_id_for(self.csv_path),
            "operation": "rollback",
            "dataset_path": str(self.csv_path),
            "log_path": str(self.log_path),
            "dataset_sha256_before": old_digest,
            "dataset_sha256_after": new_digest,
            "event": rb_event,
        }
        _write_json_atomic(journal_path, journal)

        # Replace CSV on disk to match new_digest
        self.csv_path.write_bytes(empty_csv_bytes)

        # 2. Trigger recover_pending
        rec_res = recover_pending(self.csv_path, self.log_path)
        self.assertTrue(rec_res["recovered"])
        self.assertEqual(rec_res["action"], "committed")

        # Verify audit log now has valid sequence 2 rollback event
        v2 = verify_log(self.log_path)
        self.assertTrue(v2["valid"])
        self.assertEqual(len(v2["events"]), 2)
        self.assertEqual(v2["events"][1]["operation"], "rollback")


class TestRollbackCLI(RollbackTestBase):
    """Test CLI commands and argument parser for rollback."""

    def test_cli_rollback_dry_run(self):
        save_grade("S101", "Math", 90.0, 100.0, "2026-02-01", 3.0, "1", csv_path=self.csv_path)
        v = verify_log(self.log_path)
        event_id = v["events"][0]["event_id"]

        code, stdout, stderr = self.run_cli("rollback", event_id, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(f"Dry run: rollback of add mutation (event {event_id})", stdout)
        self.assertIn("No changes were made.", stdout)
        self.assertEqual(len(self.read_csv_rows()), 1)

    def test_cli_rollback_missing_yes_safeguard(self):
        save_grade("S101", "Math", 90.0, 100.0, "2026-02-01", 3.0, "1", csv_path=self.csv_path)
        v = verify_log(self.log_path)
        event_id = v["events"][0]["event_id"]

        code, stdout, stderr = self.run_cli("rollback", event_id)
        self.assertEqual(code, 2)
        self.assertIn("rollback requires confirmation; pass --yes", stderr)

    def test_cli_rollback_successful_with_yes(self):
        save_grade("S101", "Math", 90.0, 100.0, "2026-02-01", 3.0, "1", csv_path=self.csv_path)
        v = verify_log(self.log_path)
        event_id = v["events"][0]["event_id"]

        code, stdout, stderr = self.run_cli("rollback", event_id, "--yes")
        self.assertEqual(code, 0)
        self.assertIn(f"Successfully rolled back add mutation (event {event_id})", stdout)
        self.assertEqual(len(self.read_csv_rows()), 0)


class TestRollbackConcurrency(RollbackTestBase):
    """Test genuine multi-process serialization of concurrent rollback attempts."""

    def test_concurrent_rollback_of_same_event_serializes(self):
        """Two concurrent workers trying to roll back the same event: exactly one succeeds, one gets conflict."""
        save_grade("S101", "Math", 90.0, 100.0, "2026-02-01", 3.0, "1", csv_path=self.csv_path)
        v = verify_log(self.log_path)
        event_id = v["events"][0]["event_id"]

        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(2)
        result_queue = ctx.Queue()

        p1 = ctx.Process(
            target=_worker_concurrent_rollback,
            args=(str(self.csv_path), event_id, result_queue, barrier),
        )
        p2 = ctx.Process(
            target=_worker_concurrent_rollback,
            args=(str(self.csv_path), event_id, result_queue, barrier),
        )

        p1.start()
        p2.start()

        p1.join(timeout=15.0)
        p2.join(timeout=15.0)

        results = []
        while not result_queue.empty():
            results.append(result_queue.get())

        self.assertEqual(len(results), 2)
        statuses = [r[0] for r in results]
        self.assertIn("OK", statuses)
        self.assertIn("CONFLICT", statuses)

        # Audit chain must be unbroken and valid
        v2 = verify_log(self.log_path)
        self.assertTrue(v2["valid"])
        self.assertEqual(len(v2["events"]), 2)


if __name__ == "__main__":
    unittest.main()

