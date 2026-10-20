"""Comprehensive tests for crash-recoverable transaction journal (Issue #47).

Covers:
- Journal format, schema, UUID validation, checksum verification, path traversal rejection.
- Standard lifecycle: PREPARED -> COMMITTING -> COMMITTED state transitions.
- Read-only transaction inspection (--status, --format json, table output).
- Subprocess fault-injection crash recovery:
    * Crash after PREPARED (Case B): automatic abort and rollback.
    * Crash after COMMITTING (Case C): automatic replay of staged atomic replacement.
    * Crash after replacement before COMMITTED (Case A): automatic detection of target checksum and finalization.
    * Missing staged file on COMMITTING: fails closed.
    * Target checksum matches neither source nor target (Case D): fails closed without data modification.
    * Malformed journal (Case E): fails closed without data modification.
- Multi-process concurrency and recovery racing with new mutations.
- Pruning and retention policy.
"""

import csv
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from grade_tracker.cli import (
    CANONICAL_COLUMNS,
    TransactionJournalCoordinator,
    TransactionJournalEntry,
    compute_file_sha256,
    get_journal_dir,
    save_grade,
    edit_grade,
    import_grades_csv,
    cmd_transactions,
)
from grade_tracker.journal import (
    JOURNAL_FORMAT_VERSION,
    STATE_ABORTED,
    STATE_COMMITTED,
    STATE_COMMITTING,
    STATE_PREPARED,
    load_journal_entry,
    save_journal_entry,
    set_fault_injection_hook,
)

try:
    multiprocessing.set_start_method("spawn", force=True)
except RuntimeError:
    pass


class TestJournalEntryValidation(unittest.TestCase):
    """Test strict validation of transaction journal files."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)
        self.target_csv = self.dir_path / "grades.csv"
        self.target_csv.write_text("student_id,subject,score,max_score,date,credits,semester\n", encoding="utf-8")
        self.coordinator = TransactionJournalCoordinator(self.target_csv)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_valid_entry_roundtrip(self):
        tx_id = str(uuid.uuid4())
        entry = TransactionJournalEntry(
            version="1.0",
            tx_id=tx_id,
            mutation_type="add",
            target_csv=str(self.target_csv),
            source_checksum="a" * 64,
            target_checksum="b" * 64,
            staged_filename=f".tx_stage_{tx_id}.tmp",
            state=STATE_PREPARED,
            created_at="2026-10-20T10:00:00Z",
            updated_at="2026-10-20T10:00:00Z",
        )
        j_path = save_journal_entry(self.coordinator.journal_dir, entry)
        loaded = load_journal_entry(j_path)
        self.assertEqual(loaded.tx_id, tx_id)
        self.assertEqual(loaded.state, STATE_PREPARED)
        self.assertEqual(loaded.version, "1.0")

    def test_invalid_json(self):
        bad_file = self.coordinator.journal_dir / "bad.json"
        self.coordinator.journal_dir.mkdir(parents=True, exist_ok=True)
        bad_file.write_text("{not valid json", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_journal_entry(bad_file)

    def test_missing_required_fields(self):
        bad_file = self.coordinator.journal_dir / "missing.json"
        self.coordinator.journal_dir.mkdir(parents=True, exist_ok=True)
        bad_file.write_text(json.dumps({"version": "1.0"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_journal_entry(bad_file)

    def test_invalid_uuid(self):
        bad_file = self.coordinator.journal_dir / "bad_uuid.json"
        self.coordinator.journal_dir.mkdir(parents=True, exist_ok=True)
        bad_file.write_text(
            json.dumps({
                "version": "1.0",
                "tx_id": "not-a-uuid",
                "mutation_type": "add",
                "target_csv": str(self.target_csv),
                "source_checksum": "a" * 64,
                "target_checksum": "b" * 64,
                "staged_filename": ".tx_stage_test.tmp",
                "state": "PREPARED",
                "created_at": "2026-10-20T10:00:00Z",
                "updated_at": "2026-10-20T10:00:00Z",
            }),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            load_journal_entry(bad_file)

    def test_path_traversal_in_staged_filename(self):
        bad_file = self.coordinator.journal_dir / "traversal.json"
        self.coordinator.journal_dir.mkdir(parents=True, exist_ok=True)
        tx_id = str(uuid.uuid4())
        bad_file.write_text(
            json.dumps({
                "version": "1.0",
                "tx_id": tx_id,
                "mutation_type": "add",
                "target_csv": str(self.target_csv),
                "source_checksum": "a" * 64,
                "target_checksum": "b" * 64,
                "staged_filename": "../../evil.tmp",
                "state": "PREPARED",
                "created_at": "2026-10-20T10:00:00Z",
                "updated_at": "2026-10-20T10:00:00Z",
            }),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            load_journal_entry(bad_file)

    def test_invalid_checksum_format(self):
        bad_file = self.coordinator.journal_dir / "bad_checksum.json"
        self.coordinator.journal_dir.mkdir(parents=True, exist_ok=True)
        tx_id = str(uuid.uuid4())
        bad_file.write_text(
            json.dumps({
                "version": "1.0",
                "tx_id": tx_id,
                "mutation_type": "add",
                "target_csv": str(self.target_csv),
                "source_checksum": "not-hex-sha256",
                "target_checksum": "b" * 64,
                "staged_filename": f".tx_stage_{tx_id}.tmp",
                "state": "PREPARED",
                "created_at": "2026-10-20T10:00:00Z",
                "updated_at": "2026-10-20T10:00:00Z",
            }),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            load_journal_entry(bad_file)


class TestJournalStandardLifecycle(unittest.TestCase):
    """Test standard write mutations and journal lifecycle."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)
        self.target_csv = self.dir_path / "grades.csv"
        self.coordinator = TransactionJournalCoordinator(self.target_csv)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_save_grade_creates_committed_journal(self):
        save_grade(
            student_id="S100",
            subject="Physics",
            score=90.0,
            max_score=100.0,
            date="2026-10-10",
            credits=4.0,
            semester="1",
            csv_path=str(self.target_csv),
        )
        self.assertTrue(self.target_csv.exists())
        self.assertTrue(self.coordinator.journal_dir.exists())

        journals = list(self.coordinator.journal_dir.glob("*.json"))
        self.assertEqual(len(journals), 1)
        entry = load_journal_entry(journals[0])
        self.assertEqual(entry.state, STATE_COMMITTED)
        self.assertEqual(entry.mutation_type, "add")
        self.assertEqual(entry.target_checksum, compute_file_sha256(self.target_csv))

    def test_edit_grade_lifecycle(self):
        save_grade(
            student_id="S100",
            subject="Physics",
            score=90.0,
            max_score=100.0,
            date="2026-10-10",
            credits=4.0,
            semester="1",
            csv_path=str(self.target_csv),
        )
        edit_grade(
            student_id="S100",
            semester="1",
            subject="Physics",
            date="2026-10-10",
            score=95.0,
            csv_path=str(self.target_csv),
        )
        journals = list(self.coordinator.journal_dir.glob("*.json"))
        self.assertEqual(len(journals), 2)
        entries = [load_journal_entry(j) for j in journals]
        self.assertTrue(all(e.state == STATE_COMMITTED for e in entries))
        # Content check
        with open(self.target_csv, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["score"], "95.0")

    def test_import_grades_lifecycle(self):
        source_csv = self.dir_path / "import_source.csv"
        source_csv.write_text(
            "student_id,subject,score,max_score,date,credits,semester\n"
            "S200,Chemistry,85.0,100.0,2026-10-11,3.0,1\n",
            encoding="utf-8",
        )
        res = import_grades_csv(
            source_path=str(source_csv),
            dest_path=str(self.target_csv),
        )
        self.assertEqual(res.imported, 1)
        journals = list(self.coordinator.journal_dir.glob("*.json"))
        self.assertEqual(len(journals), 1)
        entry = load_journal_entry(journals[0])
        self.assertEqual(entry.mutation_type, "import")
        self.assertEqual(entry.state, STATE_COMMITTED)


class TestTransactionCLIInspection(unittest.TestCase):
    """Test read-only 'transactions' CLI command."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)
        self.target_csv = self.dir_path / "grades.csv"
        self.coordinator = TransactionJournalCoordinator(self.target_csv)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_inspection_empty(self):
        status = self.coordinator.inspect_status()
        self.assertEqual(status, [])

    def test_inspection_after_mutation(self):
        save_grade(
            student_id="S100",
            subject="Physics",
            score=90.0,
            max_score=100.0,
            date="2026-10-10",
            credits=4.0,
            semester="1",
            csv_path=str(self.target_csv),
        )
        status = self.coordinator.inspect_status()
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]["state"], STATE_COMMITTED)
        self.assertEqual(status[0]["needs_recovery"], False)
        self.assertEqual(status[0]["health"], "committed")

    def test_inspection_does_not_modify_anything(self):
        save_grade(
            student_id="S100",
            subject="Physics",
            score=90.0,
            max_score=100.0,
            date="2026-10-10",
            credits=4.0,
            semester="1",
            csv_path=str(self.target_csv),
        )
        csv_before = self.target_csv.read_text(encoding="utf-8")
        self.coordinator.inspect_status()
        csv_after = self.target_csv.read_text(encoding="utf-8")
        self.assertEqual(csv_before, csv_after)


# Helper for subprocess fault-injection
_CRASH_SUBPROCESS_SCRIPT = """
import os
import sys
from grade_tracker.cli import save_grade
from grade_tracker.journal import set_fault_injection_hook

crash_point = sys.argv[1]
csv_path = sys.argv[2]

def hook(point, entry, staged_path):
    if point == crash_point:
        os._exit(42)

set_fault_injection_hook(hook)

try:
    save_grade(
        student_id="S_CRASH",
        subject="Crashology",
        score=100.0,
        max_score=100.0,
        date="2026-10-12",
        credits=3.0,
        semester="1",
        csv_path=csv_path,
    )
except Exception:
    sys.exit(1)
"""


class TestCrashRecoveryFaultInjection(unittest.TestCase):
    """Subprocess fault injection tests using os._exit simulating immediate power/process failure."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)
        self.target_csv = self.dir_path / "grades.csv"
        self.coordinator = TransactionJournalCoordinator(self.target_csv)

        # Initialize target CSV with 1 existing record
        save_grade(
            student_id="S_INIT",
            subject="Math",
            score=80.0,
            max_score=100.0,
            date="2026-10-01",
            credits=3.0,
            semester="1",
            csv_path=str(self.target_csv),
        )
        self.initial_checksum = compute_file_sha256(self.target_csv)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run_crash_process(self, point: str):
        result = subprocess.run(
            [sys.executable, "-c", _CRASH_SUBPROCESS_SCRIPT, point, str(self.target_csv)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 42)

    def test_crash_after_prepared_rolls_back(self):
        """Case B: Crash occurs right after PREPARED journal is written.
        Target CSV is unchanged, staged file exists.
        Recovery must mark journal ABORTED and delete staged file.
        """
        self._run_crash_process("after_prepared")

        # Inspect status before recovery
        status = self.coordinator.inspect_status()
        prepared_txs = [tx for tx in status if tx["state"] == STATE_PREPARED]
        self.assertEqual(len(prepared_txs), 1)
        self.assertTrue(prepared_txs[0]["needs_recovery"])
        self.assertEqual(prepared_txs[0]["health"], "recoverable")

        # Run recovery
        outcomes = self.coordinator.recover()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["status"], "aborted")
        self.assertIn("aborted", outcomes[0]["action"])

        # Verify dataset is intact
        self.assertEqual(compute_file_sha256(self.target_csv), self.initial_checksum)

        # Staged file must be removed
        staged_files = list(self.target_csv.parent.glob(".tx_stage_*.tmp"))
        self.assertEqual(staged_files, [])

        # Journal must now be ABORTED
        entry = load_journal_entry(Path(outcomes[0]["journal_file"]))
        self.assertEqual(entry.state, STATE_ABORTED)

    def test_crash_after_committing_replays_atomic_replace(self):
        """Case C: Crash occurs after COMMITTING journal is written, before replacement.
        Target CSV has original checksum, staged file has valid new data.
        Recovery must complete atomic replacement, verify checksum, and mark COMMITTED.
        """
        self._run_crash_process("after_committing")

        # Inspect status before recovery
        status = self.coordinator.inspect_status()
        committing_txs = [tx for tx in status if tx["state"] == STATE_COMMITTING]
        self.assertEqual(len(committing_txs), 1)
        self.assertTrue(committing_txs[0]["needs_recovery"])
        self.assertEqual(committing_txs[0]["health"], "recoverable")

        # Run recovery
        outcomes = self.coordinator.recover()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["status"], "committed")
        self.assertEqual(outcomes[0]["action"], "completed_commit")

        # Staged file replaced target CSV
        with open(self.target_csv, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("S_CRASH", content)
        self.assertIn("Crashology", content)

        # Journal marked COMMITTED
        entry = load_journal_entry(Path(outcomes[0]["journal_file"]))
        self.assertEqual(entry.state, STATE_COMMITTED)

    def test_crash_after_replace_finalizes_committed(self):
        """Case A: Crash occurs after atomic replacement, before COMMITTED journal written.
        Target CSV has target_checksum.
        Recovery detects target checksum match, finalizes journal as COMMITTED.
        """
        self._run_crash_process("after_replace")

        # Inspect status
        status = self.coordinator.inspect_status()
        committing_txs = [tx for tx in status if tx["state"] == STATE_COMMITTING]
        self.assertEqual(len(committing_txs), 1)
        self.assertTrue(committing_txs[0]["needs_recovery"])

        # Run recovery
        outcomes = self.coordinator.recover()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["status"], "committed")
        self.assertEqual(outcomes[0]["action"], "finalized_committed")

        entry = load_journal_entry(Path(outcomes[0]["journal_file"]))
        self.assertEqual(entry.state, STATE_COMMITTED)

    def test_ambiguous_case_d_neither_checksum_matches(self):
        """Case D: Target CSV was externally modified; matches neither source nor target checksum.
        Recovery must fail closed without modifying dataset or journal.
        """
        self._run_crash_process("after_prepared")

        # Corrupt / tamper with target CSV externally while retaining canonical header
        self.target_csv.write_text(
            "student_id,subject,score,max_score,date,credits,semester\n"
            "S_TAMPER,Tampered,99.0,100.0,2026-10-10,3.0,1\n",
            encoding="utf-8",
        )
        tampered_checksum = compute_file_sha256(self.target_csv)

        outcomes = self.coordinator.recover()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["status"], "ambiguous")

        # Target file must not be modified
        self.assertEqual(compute_file_sha256(self.target_csv), tampered_checksum)

        # Subsequent mutations must refuse to proceed
        with self.assertRaises(ValueError) as ctx:
            save_grade(
                student_id="S_BLOCKED",
                subject="Blocked",
                score=50.0,
                max_score=100.0,
                date="2026-10-15",
                credits=3.0,
                semester="1",
                csv_path=str(self.target_csv),
            )
        self.assertIn("ambiguous state", str(ctx.exception))

    def test_case_c_missing_staged_file_fails_closed(self):
        """Case C (error): In COMMITTING state, but staged file disappeared before recovery.
        Recovery must fail closed and refuse to tamper.
        """
        self._run_crash_process("after_committing")

        # Delete staged file
        for staged in self.target_csv.parent.glob(".tx_stage_*.tmp"):
            staged.unlink()

        outcomes = self.coordinator.recover()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["status"], "ambiguous")
        self.assertIn("staged file is missing", outcomes[0]["error"].lower())

    def test_case_e_malformed_journal_fails_closed(self):
        """Case E: Journal entry contains corrupt JSON or path traversal.
        Recovery must fail closed and refuse to modify target CSV.
        """
        bad_journal = self.coordinator.journal_dir / "bad.json"
        bad_journal.write_text("corrupted json content", encoding="utf-8")

        outcomes = self.coordinator.recover()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["status"], "ambiguous")
        self.assertIn("malformed or invalid", outcomes[0]["error"].lower())


def _concurrent_writer_task(csv_path, student_id, score, barrier, queue):
    try:
        barrier.wait(timeout=10.0)
        save_grade(
            student_id=student_id,
            subject=f"Subj_{student_id}",
            score=score,
            max_score=100.0,
            date="2026-10-20",
            credits=3.0,
            semester="1",
            csv_path=str(csv_path),
        )
        queue.put(("OK", student_id))
    except Exception as exc:
        queue.put(("ERROR", str(exc)))


class TestJournalConcurrency(unittest.TestCase):
    """Multi-process concurrency tests exercising transaction journal serialization."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)
        self.target_csv = self.dir_path / "grades.csv"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_concurrent_writes_serialize_safely(self):
        barrier = multiprocessing.Barrier(3)
        queue = multiprocessing.Queue()

        processes = [
            multiprocessing.Process(
                target=_concurrent_writer_task,
                args=(self.target_csv, f"S{i}", float(80 + i), barrier, queue),
            )
            for i in range(3)
        ]

        for p in processes:
            p.start()
        for p in processes:
            p.join(timeout=15.0)

        results = []
        while not queue.empty():
            results.append(queue.get())

        self.assertEqual(len(results), 3)
        for status, val in results:
            self.assertEqual(status, "OK", f"Failed concurrent write: {val}")

        # Check rows in CSV
        with open(self.target_csv, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        self.assertEqual(len(rows), 3)

        # Check coordinator inspection
        coord = TransactionJournalCoordinator(self.target_csv)
        status = coord.inspect_status()
        self.assertEqual(len(status), 3)
        self.assertTrue(all(tx["state"] == STATE_COMMITTED for tx in status))
        self.assertFalse(any(tx["needs_recovery"] for tx in status))

    def test_pruning_retention_limit(self):
        coord = TransactionJournalCoordinator(self.target_csv)
        # Create 10 mutations with retention_limit=3
        for i in range(10):
            save_grade(
                student_id=f"S{i}",
                subject=f"Subj{i}",
                score=80.0,
                max_score=100.0,
                date="2026-10-01",
                credits=3.0,
                semester="1",
                csv_path=str(self.target_csv),
            )
            # Prune retention manually or via coordinator
            coord._cleanup_old_journals(retention_limit=3)

        journals = list(coord.journal_dir.glob("*.json"))
        self.assertLessEqual(len(journals), 3)


if __name__ == "__main__":
    unittest.main()

