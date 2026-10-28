"""Tests for transactional audit coordination, crash recovery, and concurrency (Issue #56).

Covers:
- Transaction lifecycle: prepare, stage journal, commit dataset, finalize event, clear journal.
- Accurate before-and-after change records for grade mutations.
- Failed mutations before commit do not produce uncommitted audit events.
- Crash recovery when dataset replacement succeeds before audit finalization.
- Rollback when mutation fails before dataset replacement.
- Ambiguous transaction detection when dataset matches neither pre nor post digest.
- Repeated recovery idempotency without duplicating audit events.
- Audit integrity enforcement refusing to commit mutations on a broken audit chain.
- Concurrent grade mutations across independent OS processes with bounded timeouts.
"""

import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest

from grade_tracker.audit_log import (
    AuditIntegrityError,
    append_event,
    audit_log_path_for,
    build_event,
    chain_head,
    dataset_id_for,
    event_exists,
    pending_journal_path_for,
    read_events,
    verify_log,
)
from grade_tracker.audit_txn import (
    AmbiguousTransactionError,
    AuditTransaction,
    AuditTransactionError,
    JOURNAL_VERSION,
    _clear_journal,
    _read_journal,
    _sha256_bytes,
    _sha256_file,
    _write_json_atomic,
    journal_state,
    recover_pending,
)
from grade_tracker.cli import (
    _atomic_write_bytes,
    _atomic_write_rows,
    _csv_rows_bytes,
    save_grade,
)


def _concurrent_add_worker(csv_path, student_id, score, barrier, result_queue):
    """Worker function for multi-process concurrency test under spawn start method."""
    try:
        barrier.wait(timeout=10.0)
        save_grade(
            student_id=student_id,
            subject=f"Subj_{student_id}",
            score=score,
            max_score=100.0,
            date="2026-02-01",
            credits=3.0,
            semester="1",
            csv_path=str(csv_path),
        )
        result_queue.put(("OK", student_id))
    except Exception as exc:
        result_queue.put(("ERROR", str(exc)))


class TestAuditTransactionLifecycle(unittest.TestCase):
    """Test standard execution and before/after tracking in AuditTransaction."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)
        self.csv_path = self.dir_path / "grades.csv"
        self.log_path = audit_log_path_for(self.csv_path)
        self.journal_path = pending_journal_path_for(self.csv_path)

        # Initialize CSV with canonical header
        self.csv_path.write_text(
            "student_id,subject,score,max_score,date,credits,semester\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_successful_transaction_writes_dataset_and_appends_audit(self):
        record = {
            "student_id": "S101",
            "subject": "Math",
            "score": "90.0",
            "max_score": "100.0",
            "date": "2026-02-01",
            "credits": "3.0",
            "semester": "1",
        }
        changes = {"before": None, "after": record}

        with AuditTransaction(
            self.csv_path,
            operation="add",
            records=[record],
            changes=changes,
            actor="instructor",
            reason="HW1 score",
        ) as txn:
            txn.write_dataset([
                ["S101", "Math", "90.0", "100.0", "2026-02-01", "3.0", "1"]
            ])

        # Verify dataset was written
        self.assertTrue(self.csv_path.exists())
        self.assertIn("S101", self.csv_path.read_text(encoding="utf-8"))

        # Verify journal was cleared
        self.assertFalse(self.journal_path.exists())

        # Verify audit log was created and contains the event
        self.assertTrue(self.log_path.exists())
        v = verify_log(self.log_path)
        self.assertTrue(v["valid"])
        self.assertEqual(v["events_checked"], 1)

        event = v["events"][0]
        self.assertEqual(event["sequence"], 1)
        self.assertEqual(event["operation"], "add")
        self.assertEqual(event["actor"], "instructor")
        self.assertEqual(event["reason"], "HW1 score")
        self.assertEqual(event["changes"]["after"]["score"], "90.0")

    def test_failed_mutation_before_write_leaves_no_audit_event(self):
        """If an exception occurs inside the transaction block before writing, no event is logged."""
        record = {"student_id": "S102"}
        try:
            with AuditTransaction(
                self.csv_path,
                operation="add",
                records=[record],
            ) as txn:
                raise ValueError("Simulated validation error before write")
        except ValueError:
            pass

        # No audit event was written
        self.assertFalse(self.log_path.exists())
        # No dangling journal file remains
        self.assertFalse(self.journal_path.exists())

    def test_prevent_double_commit(self):
        """Calling write_dataset or write_bytes multiple times raises AuditTransactionError."""
        with AuditTransaction(self.csv_path, operation="add") as txn:
            txn.write_dataset([])
            with self.assertRaises(AuditTransactionError):
                txn.write_dataset([])

    def test_transaction_refuses_when_audit_log_corrupted(self):
        """A new transaction must refuse to commit when the audit log chain is corrupted."""
        # Append a corrupted event to the log file
        bad_event = build_event(1, "add", dataset_id_for(self.csv_path), [{"student_id": "S1"}])
        bad_event["hash"] = "0" * 64
        append_event(self.log_path, bad_event)

        with self.assertRaises(AuditIntegrityError):
            with AuditTransaction(self.csv_path, operation="add") as txn:
                txn.write_dataset([])


class TestAuditCrashRecovery(unittest.TestCase):
    """Test write-ahead journal crash recovery across various failure states."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)
        self.csv_path = self.dir_path / "grades.csv"
        self.log_path = audit_log_path_for(self.csv_path)
        self.journal_path = pending_journal_path_for(self.csv_path)

        self.initial_rows = [["S1", "Math", "80.0", "100.0", "2026-01-01", "3.0", "1"]]
        _atomic_write_rows(self.csv_path, self.initial_rows)
        self.initial_digest = _sha256_file(self.csv_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_recovery_when_dataset_replaced_before_finalize(self):
        """Crash scenario: dataset replaced, but process crashed before appending event to log."""
        new_rows = self.initial_rows + [["S2", "Physics", "90.0", "100.0", "2026-01-02", "3.0", "1"]]
        new_data = _csv_rows_bytes(new_rows)
        new_digest = _sha256_bytes(new_data)

        # 1. Dataset was replaced
        _atomic_write_bytes(self.csv_path, new_data)

        # 2. Journal reflects this mutation in flight
        event = build_event(
            sequence=1,
            operation="add",
            dataset_id=dataset_id_for(self.csv_path),
            records=[{"student_id": "S2"}],
            changes={"after": {"score": "90.0"}},
            event_id="crash-event-id-1",
        )
        journal = {
            "journal_version": JOURNAL_VERSION,
            "dataset_id": dataset_id_for(self.csv_path),
            "operation": "add",
            "dataset_path": str(self.csv_path),
            "log_path": str(self.log_path),
            "dataset_sha256_before": self.initial_digest,
            "dataset_sha256_after": new_digest,
            "event": event,
        }
        _write_json_atomic(self.journal_path, journal)

        # Before recovery: log does not have event, journal exists
        self.assertTrue(self.journal_path.exists())
        self.assertFalse(self.log_path.exists())

        # Recover
        result = recover_pending(self.csv_path, self.log_path)
        self.assertTrue(result["recovered"])
        self.assertEqual(result["action"], "committed")
        self.assertEqual(result["event_id"], "crash-event-id-1")

        # Journal is removed
        self.assertFalse(self.journal_path.exists())

        # Event is now finalized in the log
        self.assertTrue(self.log_path.exists())
        v = verify_log(self.log_path)
        self.assertTrue(v["valid"])
        self.assertEqual(v["events_checked"], 1)
        self.assertEqual(v["events"][0]["event_id"], "crash-event-id-1")

    def test_repeated_recovery_idempotency(self):
        """Running recovery again after it already committed does not duplicate events."""
        new_rows = self.initial_rows + [["S2", "Physics", "90.0", "100.0", "2026-01-02", "3.0", "1"]]
        new_data = _csv_rows_bytes(new_rows)
        new_digest = _sha256_bytes(new_data)
        _atomic_write_bytes(self.csv_path, new_data)

        event = build_event(1, "add", dataset_id_for(self.csv_path), [{"student_id": "S2"}], event_id="id-idempotent")
        journal = {
            "journal_version": JOURNAL_VERSION,
            "dataset_id": dataset_id_for(self.csv_path),
            "operation": "add",
            "dataset_path": str(self.csv_path),
            "log_path": str(self.log_path),
            "dataset_sha256_before": self.initial_digest,
            "dataset_sha256_after": new_digest,
            "event": event,
        }
        _write_json_atomic(self.journal_path, journal)

        # First recovery
        res1 = recover_pending(self.csv_path, self.log_path)
        self.assertTrue(res1["recovered"])
        self.assertEqual(res1["action"], "committed")

        # Second recovery without journal
        res2 = recover_pending(self.csv_path, self.log_path)
        self.assertFalse(res2["recovered"])
        self.assertEqual(res2["action"], "none")

        # Third scenario: Journal is somehow still present (e.g. crash right after appending event before unlink)
        _write_json_atomic(self.journal_path, journal)
        res3 = recover_pending(self.csv_path, self.log_path)
        self.assertTrue(res3["recovered"])
        self.assertEqual(res3["action"], "already_committed")

        # Events count in log is strictly 1, never duplicated
        v = verify_log(self.log_path)
        self.assertTrue(v["valid"])
        self.assertEqual(v["events_checked"], 1)

    def test_recovery_rollback_when_dataset_not_replaced(self):
        """Crash scenario: mutation crashed before dataset replacement (dataset matches 'before' hash)."""
        event = build_event(1, "add", dataset_id_for(self.csv_path), [{"student_id": "S2"}])
        journal = {
            "journal_version": JOURNAL_VERSION,
            "dataset_id": dataset_id_for(self.csv_path),
            "operation": "add",
            "dataset_path": str(self.csv_path),
            "log_path": str(self.log_path),
            "dataset_sha256_before": self.initial_digest,
            "dataset_sha256_after": "f" * 64,  # Future hash that was never committed
            "event": event,
        }
        _write_json_atomic(self.journal_path, journal)

        # Recovery sees dataset matches before hash; rolls back journal
        result = recover_pending(self.csv_path, self.log_path)
        self.assertTrue(result["recovered"])
        self.assertEqual(result["action"], "rolled_back")
        self.assertIsNone(result["event_id"])

        # Journal is cleared, no audit event was created
        self.assertFalse(self.journal_path.exists())
        self.assertFalse(self.log_path.exists())

    def test_ambiguous_transaction_fails_closed(self):
        """If dataset matches neither before nor after hash, recovery raises AmbiguousTransactionError."""
        event = build_event(1, "add", dataset_id_for(self.csv_path), [{"student_id": "S2"}])
        journal = {
            "journal_version": JOURNAL_VERSION,
            "dataset_id": dataset_id_for(self.csv_path),
            "operation": "add",
            "dataset_path": str(self.csv_path),
            "log_path": str(self.log_path),
            "dataset_sha256_before": "0" * 64,
            "dataset_sha256_after": "1" * 64,
            "event": event,
        }
        _write_json_atomic(self.journal_path, journal)

        with self.assertRaises(AmbiguousTransactionError):
            recover_pending(self.csv_path, self.log_path)

        # Journal is preserved for operator investigation
        self.assertTrue(self.journal_path.exists())


class TestAuditConcurrency(unittest.TestCase):
    """Test multi-process concurrency ensuring audit log linearity and consistency."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.csv_path = Path(self.temp_dir.name) / "grades.csv"
        self.log_path = audit_log_path_for(self.csv_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_concurrent_mutations_form_valid_audit_chain(self):
        """Multiple independent processes adding grades concurrently must produce a linear, valid audit log."""
        ctx = multiprocessing.get_context("spawn")
        num_workers = 3
        barrier = ctx.Barrier(num_workers)
        result_queue = ctx.Queue()

        workers = [
            ctx.Process(
                target=_concurrent_add_worker,
                args=(self.csv_path, f"S{i}", float(80 + i), barrier, result_queue),
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
            self.assertEqual(status, "OK", f"Concurrent add failed: {val}")

        # Audit log must exist, be completely valid, and have exactly 3 ordered events
        self.assertTrue(self.log_path.exists())
        verification = verify_log(self.log_path)
        self.assertTrue(verification["valid"])
        self.assertIsNone(verification["first_invalid"])
        self.assertEqual(verification["events_checked"], num_workers)
        self.assertEqual(verification["last_sequence"], num_workers)

        # Sequences must be strictly 1, 2, 3
        sequences = [e["sequence"] for e in verification["events"]]
        self.assertEqual(sequences, [1, 2, 3])


if __name__ == "__main__":
    unittest.main()

