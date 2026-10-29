import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import io

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
    JOURNAL_VERSION,
    _clear_journal,
    _read_journal,
    _write_json_atomic,
)
from grade_tracker.cli import (
    BACKUP_FORMAT_VERSION,
    SUPPORTED_CSV_SCHEMA_VERSION,
    _InterProcessLock,
    _lock_path_for,
    create_backup,
    main,
    save_grade,
)
from grade_tracker.integrity import (
    REPORT_SCHEMA_VERSION,
    SEV_ERROR,
    SEV_OK,
    SEV_UNVERIFIABLE,
    SEV_WARNING,
    CrossFileIntegrityError,
    IntegrityCheckError,
    RepairConfirmationRequiredError,
    RepairError,
    StaleRepairPlanError,
    UnrepairableStateError,
    execute_repair_plan,
    format_integrity_report_text,
    generate_repair_plan,
    run_cross_file_verification,
)


def _init_dataset_with_audit(csv_path: Path):
    """Helper to initialize a valid grades.csv and corresponding audit event."""
    save_grade("S101", "Math", 90.0, 100.0, "2026-03-01", 3.0, 1, csv_path=csv_path)


class TestCrossFileIntegrityVerification(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.csv_path = self.base_dir / "grades.csv"
        _init_dataset_with_audit(self.csv_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_clean_baseline_passes(self):
        create_backup(csv_path=self.csv_path)
        report = run_cross_file_verification(self.csv_path)
        self.assertEqual(report["report_schema_version"], REPORT_SCHEMA_VERSION)
        self.assertEqual(report["overall_status"], SEV_OK)
        self.assertEqual(report["unresolved_issues"], 0)
        self.assertEqual(report["repairable_issues"], 0)
        self.assertGreater(report["total_checks"], 0)

    def test_missing_dataset_file(self):
        self.csv_path.unlink()
        report = run_cross_file_verification(self.csv_path)
        self.assertEqual(report["overall_status"], SEV_WARNING)
        check_ids = [c["check_id"] for c in report["checks"]]
        self.assertIn("dataset_presence", check_ids)

    def test_corrupted_dataset_csv_syntax(self):
        with open(self.csv_path, "w", encoding="utf-8") as f:
            f.write("student_id,subject,score\n")  # Missing required columns
            f.write("S101,Math\n")

        report = run_cross_file_verification(self.csv_path)
        self.assertEqual(report["overall_status"], SEV_ERROR)
        syntax_check = next(c for c in report["checks"] if c["check_id"] == "dataset_csv_syntax")
        self.assertEqual(syntax_check["status"], SEV_ERROR)
        self.assertFalse(syntax_check["repairable"])

    def test_missing_audit_log(self):
        audit_path = audit_log_path_for(self.csv_path)
        if audit_path.exists():
            audit_path.unlink()

        report = run_cross_file_verification(self.csv_path)
        self.assertEqual(report["overall_status"], SEV_WARNING)
        audit_check = next(c for c in report["checks"] if c["check_id"] == "audit_log_presence")
        self.assertEqual(audit_check["status"], SEV_WARNING)
        self.assertFalse(audit_check["repairable"])

    def test_corrupted_audit_hash_chain(self):
        audit_path = audit_log_path_for(self.csv_path)
        # Directly tamper raw bytes of audit log line
        raw = audit_path.read_text(encoding="utf-8")
        tampered = raw.replace('"score":"90.0"', '"score":"99.0"').replace('"score": "90.0"', '"score": "99.0"')
        self.assertNotEqual(raw, tampered)
        audit_path.write_text(tampered, encoding="utf-8")

        report = run_cross_file_verification(self.csv_path)
        self.assertEqual(report["overall_status"], SEV_UNVERIFIABLE)
        chain_check = next(c for c in report["checks"] if c["check_id"] == "audit_chain_validity")
        self.assertEqual(chain_check["status"], SEV_UNVERIFIABLE)
        self.assertFalse(chain_check["repairable"])

    def test_dataset_checksum_mismatch_with_audit_head(self):
        # Directly modify grades.csv without writing to audit log: remove record
        with open(self.csv_path, "w", encoding="utf-8") as f:
            f.write("student_id,subject,score,max_score,date,credits,semester\n")

        report = run_cross_file_verification(self.csv_path)
        self.assertEqual(report["overall_status"], SEV_WARNING)
        sync_check = next(c for c in report["checks"] if c["check_id"] == "dataset_audit_sync")
        self.assertEqual(sync_check["status"], SEV_WARNING)
        self.assertFalse(sync_check["repairable"])

    def test_backup_manifest_checksum_mismatch(self):
        backup_res = create_backup(csv_path=self.csv_path)
        backup_csv = Path(backup_res["backup_csv"])
        with open(backup_csv, "a", encoding="utf-8") as f:
            f.write("corrupted extra data\n")

        report = run_cross_file_verification(self.csv_path)
        self.assertEqual(report["overall_status"], SEV_ERROR)
        mismatch_check = next(c for c in report["checks"] if "backup_checksum_" in c["check_id"])
        self.assertEqual(mismatch_check["status"], SEV_ERROR)
        self.assertFalse(mismatch_check["repairable"])

    def test_missing_backup_csv_for_manifest(self):
        backup_res = create_backup(csv_path=self.csv_path)
        backup_csv = Path(backup_res["backup_csv"])
        backup_csv.unlink()

        report = run_cross_file_verification(self.csv_path)
        self.assertEqual(report["overall_status"], SEV_ERROR)
        missing_csv_check = next(c for c in report["checks"] if "backup_data_presence_" in c["check_id"])
        self.assertEqual(missing_csv_check["status"], SEV_ERROR)

    def test_orphaned_backup_csv(self):
        backup_res = create_backup(csv_path=self.csv_path)
        manifest_json = Path(backup_res["manifest"])
        manifest_json.unlink()

        report = run_cross_file_verification(self.csv_path)
        self.assertEqual(report["overall_status"], SEV_WARNING)
        orphaned_check = next(c for c in report["checks"] if "backup_orphaned_csv_" in c["check_id"])
        self.assertEqual(orphaned_check["status"], SEV_WARNING)

    def test_orphaned_temp_files_detected(self):
        tmp_file = self.base_dir / ".grades-calc-12345.tmp"
        tmp_file.write_text("temporary leftover")

        report = run_cross_file_verification(self.csv_path)
        self.assertEqual(report["overall_status"], SEV_WARNING)
        tmp_check = next(c for c in report["checks"] if "orphaned_tmp_" in c["check_id"])
        self.assertEqual(tmp_check["status"], SEV_WARNING)
        self.assertTrue(tmp_check["repairable"])

    def test_gpa_snapshot_integrity(self):
        snapshots_dir = self.base_dir / "snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        snap_file = snapshots_dir / "snapshot-2026-03-01.json"

        snap_content = {"student_id": "S101", "sgpa": 9.0}
        canon_bytes = json.dumps({"data": snap_content, "snapshot_version": "1.0"}, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        digest = hashlib.sha256(canon_bytes).hexdigest()
        snap_data = {
            "snapshot_version": "1.0",
            "content_digest": digest,
            "data": snap_content,
        }
        snap_file.write_text(json.dumps(snap_data))

        report = run_cross_file_verification(self.csv_path)
        self.assertEqual(report["overall_status"], SEV_OK)

        # Corrupt snapshot content_digest
        snap_data["content_digest"] = "bad" * 16
        snap_file.write_text(json.dumps(snap_data))

        report_corrupt = run_cross_file_verification(self.csv_path)
        self.assertEqual(report_corrupt["overall_status"], SEV_ERROR)
        snap_check = next(c for c in report_corrupt["checks"] if "snapshot_integrity_" in c["check_id"])
        self.assertEqual(snap_check["status"], SEV_ERROR)
        self.assertFalse(snap_check["repairable"])


class TestSafeRepairEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.csv_path = self.base_dir / "grades.csv"
        _init_dataset_with_audit(self.csv_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_repair_requires_confirmation(self):
        tmp_file = self.base_dir / ".grades-restore-999.tmp"
        tmp_file.write_text("temp")

        with self.assertRaises(RepairConfirmationRequiredError):
            execute_repair_plan(self.csv_path, confirmed=False, dry_run=False)

    def test_repair_dry_run_leaves_files_untouched(self):
        tmp_file = self.base_dir / ".grades-restore-999.tmp"
        tmp_file.write_text("temp")

        res = execute_repair_plan(self.csv_path, dry_run=True)
        self.assertTrue(res["dry_run"])
        self.assertEqual(len(res["actions_planned"]), 1)
        self.assertEqual(len(res["actions_executed"]), 0)
        self.assertTrue(tmp_file.exists())

    def test_repair_removes_orphaned_temp_file(self):
        tmp_file = self.base_dir / ".grades-restore-999.tmp"
        tmp_file.write_text("temp")

        res = execute_repair_plan(self.csv_path, confirmed=True, dry_run=False)
        self.assertFalse(res["dry_run"])
        self.assertEqual(len(res["actions_executed"]), 1)
        self.assertFalse(tmp_file.exists())
        self.assertTrue(res["success"])

    def test_repair_pending_transaction_committed_state(self):
        csv_before_bytes = self.csv_path.read_bytes()
        sha_before = hashlib.sha256(csv_before_bytes).hexdigest()

        new_row = "S202,Science,85.0,100.0,2026-03-02,3.0,1\n".encode("utf-8")
        csv_after_bytes = csv_before_bytes + new_row
        self.csv_path.write_bytes(csv_after_bytes)
        sha_after = hashlib.sha256(csv_after_bytes).hexdigest()

        # Build prepared event
        entries = read_events(audit_log_path_for(self.csv_path))
        head_hash = entries[-1][1]["hash"]
        event = build_event(
            sequence=2,
            operation="add",
            dataset_id=dataset_id_for(self.csv_path),
            records=[{"student_id": "S202", "subject": "Science"}],
            changes={},
            actor="tester",
            reason="interrupted mutation",
            previous_hash=head_hash,
        )

        journal_path = pending_journal_path_for(self.csv_path)
        journal_data = {
            "journal_version": JOURNAL_VERSION,
            "dataset_sha256_before": sha_before,
            "dataset_sha256_after": sha_after,
            "event": event,
        }
        journal_path.write_text(json.dumps(journal_data))

        diag = run_cross_file_verification(self.csv_path)
        self.assertEqual(diag["overall_status"], SEV_WARNING)
        self.assertEqual(diag["repairable_issues"], 1)

        plan = generate_repair_plan(self.csv_path)
        self.assertEqual(len(plan["actions"]), 1)
        self.assertEqual(plan["actions"][0]["action"], "complete_transaction")

        res = execute_repair_plan(self.csv_path, confirmed=True, dry_run=False)
        self.assertTrue(res["success"])
        self.assertFalse(journal_path.exists())

        preservations = list((self.base_dir / "preservations").glob("journal-pre-repair-*.json"))
        self.assertEqual(len(preservations), 1)

    def test_repair_pending_transaction_uncommitted_state(self):
        csv_before_bytes = self.csv_path.read_bytes()
        sha_before = hashlib.sha256(csv_before_bytes).hexdigest()
        sha_after = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

        event = build_event(
            sequence=2,
            operation="add",
            dataset_id=dataset_id_for(self.csv_path),
            records=[{"student_id": "S303", "subject": "History"}],
            changes={},
            actor="tester",
            reason="uncommitted mutation",
            previous_hash="3c24e4bf6bac8fcf607bf97bf7f1f67236b73fa18182352f262ac43b06d67683",
        )

        journal_path = pending_journal_path_for(self.csv_path)
        journal_data = {
            "journal_version": JOURNAL_VERSION,
            "dataset_sha256_before": sha_before,
            "dataset_sha256_after": sha_after,
            "event": event,
        }
        journal_path.write_text(json.dumps(journal_data))

        plan = generate_repair_plan(self.csv_path)
        self.assertEqual(plan["actions"][0]["action"], "rollback_transaction")

        res = execute_repair_plan(self.csv_path, confirmed=True, dry_run=False)
        self.assertTrue(res["success"])
        self.assertFalse(journal_path.exists())

        preservations = list((self.base_dir / "preservations").glob("journal-pre-repair-*.json"))
        self.assertEqual(len(preservations), 1)

    def test_ambiguous_transaction_is_unrepairable(self):
        journal_path = pending_journal_path_for(self.csv_path)
        event = build_event(
            sequence=2,
            operation="add",
            dataset_id=dataset_id_for(self.csv_path),
            records=[{"student_id": "S404", "subject": "Art"}],
            changes={},
            actor="tester",
            reason="ambiguous",
            previous_hash="3c24e4bf6bac8fcf607bf97bf7f1f67236b73fa18182352f262ac43b06d67683",
        )
        journal_data = {
            "journal_version": JOURNAL_VERSION,
            "dataset_sha256_before": "1111111111111111111111111111111111111111111111111111111111111111",
            "dataset_sha256_after": "2222222222222222222222222222222222222222222222222222222222222222",
            "event": event,
        }
        journal_path.write_text(json.dumps(journal_data))

        diag = run_cross_file_verification(self.csv_path)
        self.assertEqual(diag["overall_status"], SEV_UNVERIFIABLE)
        self.assertEqual(diag["repairable_issues"], 0)

        with self.assertRaises(UnrepairableStateError):
            execute_repair_plan(self.csv_path, confirmed=True, dry_run=False)


class TestCLIIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.csv_path = self.base_dir / "grades.csv"
        _init_dataset_with_audit(self.csv_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cli_check_command_clean_exit_zero(self):
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            with mock.patch("sys.argv", ["grade-tracker", "--data", str(self.csv_path), "check"]):
                try:
                    main()
                    exit_code = 0
                except SystemExit as exc:
                    exit_code = exc.code
        self.assertEqual(exit_code, 0)
        self.assertIn("Cross-File Integrity Verification Report", out.getvalue())
        self.assertIn("OVERALL STATUS: OK", out.getvalue().upper())

    def test_cli_check_command_json_format(self):
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            with mock.patch("sys.argv", ["grade-tracker", "--data", str(self.csv_path), "check", "--format", "json"]):
                try:
                    main()
                    exit_code = 0
                except SystemExit as exc:
                    exit_code = exc.code
        self.assertEqual(exit_code, 0)
        parsed = json.loads(out.getvalue())
        self.assertEqual(parsed["overall_status"], "ok")
        self.assertEqual(parsed["report_schema_version"], "1.0")

    def test_cli_check_command_fails_with_exit_one_on_issue(self):
        (self.base_dir / ".grades-orphan.tmp").write_text("orphan")

        out = io.StringIO()
        err = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            with mock.patch("sys.argv", ["grade-tracker", "--data", str(self.csv_path), "check"]):
                try:
                    main()
                    exit_code = 0
                except SystemExit as exc:
                    exit_code = exc.code
        self.assertEqual(exit_code, 1)
        self.assertIn("WARNING", out.getvalue().upper())

    def test_cli_repair_dry_run(self):
        (self.base_dir / ".grades-orphan.tmp").write_text("orphan")

        out = io.StringIO()
        err = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            with mock.patch("sys.argv", ["grade-tracker", "--data", str(self.csv_path), "repair", "--dry-run"]):
                try:
                    main()
                    exit_code = 0
                except SystemExit as exc:
                    exit_code = exc.code
        self.assertEqual(exit_code, 0)
        self.assertIn("Integrity Repair Preview (Dry Run)", out.getvalue())
        self.assertTrue((self.base_dir / ".grades-orphan.tmp").exists())

    def test_cli_repair_requires_yes(self):
        (self.base_dir / ".grades-orphan.tmp").write_text("orphan")

        out = io.StringIO()
        err = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            with mock.patch("sys.argv", ["grade-tracker", "--data", str(self.csv_path), "repair"]):
                try:
                    main()
                    exit_code = 0
                except SystemExit as exc:
                    exit_code = exc.code
        self.assertEqual(exit_code, 2)
        self.assertIn("confirmation", err.getvalue().lower())

    def test_cli_repair_with_yes_executes_successfully(self):
        (self.base_dir / ".grades-orphan.tmp").write_text("orphan")

        out = io.StringIO()
        err = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            with mock.patch("sys.argv", ["grade-tracker", "--data", str(self.csv_path), "repair", "--yes"]):
                try:
                    main()
                    exit_code = 0
                except SystemExit as exc:
                    exit_code = exc.code
        self.assertEqual(exit_code, 0)
        self.assertIn("Integrity Repair Completed", out.getvalue())
        self.assertFalse((self.base_dir / ".grades-orphan.tmp").exists())


if __name__ == "__main__":
    unittest.main()
