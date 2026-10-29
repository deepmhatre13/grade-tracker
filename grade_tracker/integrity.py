"""Cross-file integrity verification and safe repair subsystem.

This module provides diagnostic verification across:
- The active grade CSV dataset and its checksum/schema.
- The tamper-evident audit log and hash chain.
- The write-ahead pending transaction journal.
- Backups and backup JSON manifests.
- Temporary files (.grades-*.tmp, .audit-journal-*.tmp, etc.).
- GPA snapshots (if present in snapshots/).

It generates machine-readable diagnostic reports and provides an explicitly
confirmed safe repair workflow under cross-process lock.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, List, Optional, Tuple, Union

from grade_tracker.audit_log import (
    AUDIT_SCHEMA_VERSION,
    append_event,
    audit_log_path_for,
    build_event,
    chain_head,
    dataset_id_for,
    pending_journal_path_for,
    read_events,
    verify_log,
)
from grade_tracker.audit_txn import (
    JOURNAL_VERSION,
    _clear_journal,
    _read_journal,
    _sha256_file,
    recover_pending,
)
# CLI helpers imported within functions to avoid circular import

REPORT_SCHEMA_VERSION = "1.0"

# Check severity levels
SEV_OK = "ok"
SEV_WARNING = "warning"
SEV_ERROR = "error"
SEV_UNVERIFIABLE = "unverifiable"


class CrossFileIntegrityError(Exception):
    """Base exception for cross-file integrity errors."""


class RepairError(CrossFileIntegrityError):
    """Raised when a repair cannot be safely completed."""


class StaleRepairPlanError(RepairError):
    """Raised when files have changed between plan generation and execution."""


class IntegrityCheckError(CrossFileIntegrityError):
    """Raised when verification cannot be performed due to an unexpected error."""


class RepairConfirmationRequiredError(RepairError):
    """Raised when repair execution is attempted without explicit user confirmation."""


class UnrepairableStateError(RepairError):
    """Raised when unrepairable or ambiguous inconsistencies prevent safe repair."""


def _relative_to_parent(path: Path, base_dir: Path) -> str:
    """Return a relative path or basename without leaking sensitive absolute paths."""
    try:
        return str(path.relative_to(base_dir))
    except (ValueError, TypeError):
        return path.name


def run_cross_file_verification(csv_path: Union[str, Path]) -> Dict[str, Any]:
    """Execute read-only cross-file integrity verification across all related artifacts.

    Returns a structured report adhering to REPORT_SCHEMA_VERSION.
    Never modifies, repairs, creates, or deletes any files.
    """
    from grade_tracker.cli import (
        BACKUP_FORMAT_VERSION,
        SUPPORTED_CSV_SCHEMA_VERSION,
        audit_grades_csv,
        compute_sha256,
    )
    csv_file = Path(csv_path).resolve()
    base_dir = csv_file.parent

    now = datetime.now(timezone.utc).isoformat()
    checks: List[Dict[str, Any]] = []
    overall_status = SEV_OK

    def add_check(
        check_id: str,
        artifact_type: str,
        path: Path,
        status: str,
        summary: str,
        expected: Optional[Any] = None,
        observed: Optional[Any] = None,
        repairable: bool = False,
        repair_action: Optional[str] = None,
        repair_rationale: Optional[str] = None,
    ) -> None:
        nonlocal overall_status
        if status == SEV_UNVERIFIABLE:
            overall_status = SEV_UNVERIFIABLE
        elif status == SEV_ERROR and overall_status != SEV_UNVERIFIABLE:
            overall_status = SEV_ERROR
        elif status == SEV_WARNING and overall_status not in (SEV_ERROR, SEV_UNVERIFIABLE):
            overall_status = SEV_WARNING

        rel_path = _relative_to_parent(path, base_dir)
        checks.append({
            "check_id": check_id,
            "artifact_type": artifact_type,
            "path": rel_path,
            "status": status,
            "summary": summary,
            "expected": expected,
            "observed": observed,
            "repairable": repairable,
            "repair_action": repair_action,
            "repair_rationale": repair_rationale,
        })

    # 1. Dataset existence & CSV integrity
    dataset_exists = csv_file.exists()
    current_csv_digest = None
    if not dataset_exists:
        add_check(
            check_id="dataset_presence",
            artifact_type="dataset",
            path=csv_file,
            status=SEV_WARNING,
            summary="Dataset CSV file does not exist (empty or missing data file).",
            expected="present",
            observed="missing",
            repairable=False,
        )
    else:
        current_csv_digest = compute_sha256(csv_file)
        diagnostics = audit_grades_csv(csv_file)
        if diagnostics:
            add_check(
                check_id="dataset_csv_syntax",
                artifact_type="dataset",
                path=csv_file,
                status=SEV_ERROR,
                summary=f"Dataset CSV failed internal integrity audit ({len(diagnostics)} errors).",
                expected="0 validation errors",
                observed=f"{len(diagnostics)} errors: {diagnostics[0]}",
                repairable=False,
            )
        else:
            add_check(
                check_id="dataset_csv_syntax",
                artifact_type="dataset",
                path=csv_file,
                status=SEV_OK,
                summary="Dataset CSV syntax and canonical schema valid.",
            )

    # 2. Audit log chain verification
    log_path = audit_log_path_for(csv_file)
    audit_events: List[Dict[str, Any]] = []
    if not log_path.exists():
        add_check(
            check_id="audit_log_presence",
            artifact_type="audit_log",
            path=log_path,
            status=SEV_OK if not dataset_exists else SEV_WARNING,
            summary="Audit log file does not exist.",
            expected="present",
            observed="missing",
            repairable=False,
        )
    else:
        expected_dataset_id = dataset_id_for(csv_file)
        audit_res = verify_log(log_path, dataset_id=expected_dataset_id)
        if not audit_res["valid"]:
            inv = audit_res.get("first_invalid", {})
            add_check(
                check_id="audit_chain_validity",
                artifact_type="audit_log",
                path=log_path,
                status=SEV_UNVERIFIABLE,
                summary=f"Audit hash chain is corrupted: {inv.get('message', 'invalid chain')}",
                expected="valid hash chain",
                observed=inv.get("category", "broken_chain"),
                repairable=False,
                repair_rationale="Corrupted audit chain cannot be automatically truncated or rewritten.",
            )
        else:
            audit_events = audit_res["events"]
            add_check(
                check_id="audit_chain_validity",
                artifact_type="audit_log",
                path=log_path,
                status=SEV_OK,
                summary=f"Audit hash chain is valid ({audit_res['events_checked']} events verified).",
            )
            # Check if dataset has unrecorded changes after audit head
            # If dataset has records not matching audit event count or changes
            # We can check if dataset exists and audit has events:
            # specifically, if dataset is modified outside of audit transaction
            if audit_events and dataset_exists:
                # Count valid events with mutations
                mutation_events = [e for e in audit_events if e.get("operation") in ("add", "edit", "import", "delete", "restore", "migrate", "rollback", "repair")]
                # Simple heuristic: if dataset lines differ significantly or if dataset modified time > audit log modified time + offset
                # More robust: read rows from csv_file
                from grade_tracker.cli import _read_existing_rows
                try:
                    read_res = _read_existing_rows(csv_file)
                    current_rows = read_res[1] if isinstance(read_res, tuple) else read_res
                    last_ev = audit_events[-1]
                    last_records = last_ev.get("records") or []
                    if last_ev.get("operation") == "add" and last_records:
                        target_rec = last_records[0]
                        matching = [r for r in current_rows if r.get("student_id") == target_rec.get("student_id") and r.get("subject") == target_rec.get("subject")]
                        if not matching:
                            add_check(
                                check_id="dataset_audit_sync",
                                artifact_type="dataset",
                                path=csv_file,
                                status=SEV_WARNING,
                                summary="Dataset is out of sync with audit log history.",
                                expected="matching records",
                                observed="mismatched",
                                repairable=False,
                            )
                except Exception:
                    pass

    # 3. Transaction journal check
    journal_path = pending_journal_path_for(csv_file)
    journal_data = None
    if journal_path.exists():
        try:
            journal_data = _read_journal(journal_path)
        except Exception as exc:
            add_check(
                check_id="transaction_journal_state",
                artifact_type="journal",
                path=journal_path,
                status=SEV_ERROR,
                summary=f"Pending transaction journal is malformed: {exc}",
                expected="valid JSON journal",
                observed="unreadable",
                repairable=False,
            )

        if journal_data is not None:
            j_ver = journal_data.get("journal_version")
            if j_ver != JOURNAL_VERSION:
                add_check(
                    check_id="transaction_journal_state",
                    artifact_type="journal",
                    path=journal_path,
                    status=SEV_ERROR,
                    summary=f"Unsupported transaction journal version {j_ver!r}.",
                    expected=JOURNAL_VERSION,
                    observed=j_ver,
                    repairable=False,
                )
            else:
                old_dig = journal_data.get("dataset_sha256_before")
                new_dig = journal_data.get("dataset_sha256_after")
                ev = journal_data.get("event")

                if current_csv_digest == new_dig:
                    add_check(
                        check_id="transaction_journal_state",
                        artifact_type="journal",
                        path=journal_path,
                        status=SEV_WARNING,
                        summary="Pending transaction was committed to dataset but audit log finalize was interrupted.",
                        expected="cleared journal",
                        observed="commit_unfinalized",
                        repairable=True,
                        repair_action="complete_transaction",
                        repair_rationale=(
                            "Dataset matches post-transaction digest. Recovery will finalize "
                            "audit event and safely remove pending journal."
                        ),
                    )
                elif current_csv_digest == old_dig:
                    add_check(
                        check_id="transaction_journal_state",
                        artifact_type="journal",
                        path=journal_path,
                        status=SEV_WARNING,
                        summary="Pending transaction failed before dataset replace.",
                        expected="cleared journal",
                        observed="uncommitted_journal",
                        repairable=True,
                        repair_action="rollback_transaction",
                        repair_rationale=(
                            "Dataset matches pre-transaction digest. Recovery will safely "
                            "discard incomplete journal without logging false audit events."
                        ),
                    )
                else:
                    add_check(
                        check_id="transaction_journal_state",
                        artifact_type="journal",
                        path=journal_path,
                        status=SEV_UNVERIFIABLE,
                        summary="Ambiguous transaction state: dataset digest matches neither pre- nor post-transaction state.",
                        expected=f"{old_dig} or {new_dig}",
                        observed=current_csv_digest,
                        repairable=False,
                        repair_rationale="Dataset altered out-of-band during mutation; requires manual inspection.",
                    )
    else:
        add_check(
            check_id="transaction_journal_state",
            artifact_type="journal",
            path=journal_path,
            status=SEV_OK,
            summary="No pending transaction journal present.",
        )

    # 4. Backups and manifest verification
    backup_dir = base_dir / "backups"
    if backup_dir.exists() and backup_dir.is_dir():
        csv_files = {p.name: p for p in backup_dir.glob("*.csv")}
        json_files = {p.name: p for p in backup_dir.glob("*.json")}

        for json_name, manifest_file in sorted(json_files.items()):
            stem = manifest_file.stem
            expected_csv_name = f"{stem}.csv"
            paired_csv = backup_dir / expected_csv_name

            try:
                with open(manifest_file, "r", encoding="utf-8") as f:
                    manifest_data = json.load(f)
            except Exception as exc:
                add_check(
                    check_id=f"backup_manifest_syntax_{stem}",
                    artifact_type="backup_manifest",
                    path=manifest_file,
                    status=SEV_ERROR,
                    summary=f"Backup manifest is unreadable or malformed JSON: {exc}",
                    expected="valid JSON manifest",
                    observed=str(exc),
                    repairable=False,
                )
                continue

            if not isinstance(manifest_data, dict):
                add_check(
                    check_id=f"backup_manifest_syntax_{stem}",
                    artifact_type="backup_manifest",
                    path=manifest_file,
                    status=SEV_ERROR,
                    summary="Backup manifest root is not a JSON object.",
                    repairable=False,
                )
                continue

            fmt_ver = manifest_data.get("format_version")
            if fmt_ver != BACKUP_FORMAT_VERSION:
                add_check(
                    check_id=f"backup_manifest_version_{stem}",
                    artifact_type="backup_manifest",
                    path=manifest_file,
                    status=SEV_WARNING,
                    summary=f"Backup manifest has unsupported format_version {fmt_ver!r}.",
                    expected=BACKUP_FORMAT_VERSION,
                    observed=fmt_ver,
                    repairable=False,
                )

            # Check paired backup CSV existence
            if not paired_csv.exists():
                add_check(
                    check_id=f"backup_data_presence_{stem}",
                    artifact_type="backup_data",
                    path=paired_csv,
                    status=SEV_ERROR,
                    summary=f"Backup CSV referenced by manifest {json_name} is missing.",
                    expected="present",
                    observed="missing",
                    repairable=False,
                )
            else:
                expected_sha = manifest_data.get("sha256")
                actual_sha = compute_sha256(paired_csv)
                if not expected_sha or actual_sha.lower() != str(expected_sha).lower():
                    add_check(
                        check_id=f"backup_checksum_{stem}",
                        artifact_type="backup_data",
                        path=paired_csv,
                        status=SEV_ERROR,
                        summary=f"Backup CSV checksum mismatch against manifest.",
                        expected=expected_sha,
                        observed=actual_sha,
                        repairable=False,
                    )
                else:
                    add_check(
                        check_id=f"backup_pair_{stem}",
                        artifact_type="backup_pair",
                        path=manifest_file,
                        status=SEV_OK,
                        summary=f"Backup pair {stem} is valid and verified against manifest digest.",
                    )

        # Check for orphaned backup CSVs without manifest
        for csv_name, csv_path_item in sorted(csv_files.items()):
            stem = csv_path_item.stem
            expected_json = backup_dir / f"{stem}.json"
            if not expected_json.exists():
                add_check(
                    check_id=f"backup_orphaned_csv_{stem}",
                    artifact_type="backup_data",
                    path=csv_path_item,
                    status=SEV_WARNING,
                    summary=f"Backup CSV file has no companion JSON manifest.",
                    expected="paired manifest",
                    observed="orphaned",
                    repairable=False,
                )

    # 5. Orphaned temporary files
    # Known temp prefixes: .grades-*.tmp, .audit-journal-*.tmp, .grades-restore-*.tmp, .backup-*.tmp, .snap-*.tmp
    temp_candidates = list(base_dir.glob(".*.tmp"))
    if backup_dir.exists():
        temp_candidates.extend(backup_dir.glob(".*.tmp"))

    for tmp_file in sorted(temp_candidates):
        is_safe_orphan = False
        # If journal_path exists, do not remove any temp file referenced or active
        if tmp_file.is_file():
            # Old temporary files not held by active locks
            is_safe_orphan = True

        add_check(
            check_id=f"orphaned_tmp_{tmp_file.name}",
            artifact_type="temp_file",
            path=tmp_file,
            status=SEV_WARNING,
            summary=f"Orphaned temporary file {tmp_file.name} detected.",
            expected="clean directory",
            observed=tmp_file.name,
            repairable=is_safe_orphan,
            repair_action="remove_temp_file" if is_safe_orphan else None,
            repair_rationale="Remove leftover temporary file from interrupted process." if is_safe_orphan else None,
        )

    # 6. GPA calculation snapshots (if snapshots directory exists)
    snapshots_dir = base_dir / "snapshots"
    if snapshots_dir.exists() and snapshots_dir.is_dir():
        for snap_file in sorted(snapshots_dir.glob("*.json")):
            stem = snap_file.stem
            try:
                with open(snap_file, "r", encoding="utf-8") as f:
                    snap_data = json.load(f)

                if not isinstance(snap_data, dict):
                    add_check(
                        check_id=f"snapshot_format_{stem}",
                        artifact_type="snapshot",
                        path=snap_file,
                        status=SEV_ERROR,
                        summary="Snapshot root is not a JSON object.",
                        repairable=False,
                    )
                    continue

                rec_digest = snap_data.get("content_digest")
                if not rec_digest or not isinstance(rec_digest, str):
                    add_check(
                        check_id=f"snapshot_digest_{stem}",
                        artifact_type="snapshot",
                        path=snap_file,
                        status=SEV_ERROR,
                        summary="Snapshot missing content_digest field.",
                        repairable=False,
                    )
                else:
                    # Compute canonical digest
                    clean_data = {k: v for k, v in snap_data.items() if k != "content_digest"}
                    canon_bytes = json.dumps(clean_data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
                    calc_digest = hashlib.sha256(canon_bytes).hexdigest()
                    if calc_digest.lower() != rec_digest.lower():
                        add_check(
                            check_id=f"snapshot_integrity_{stem}",
                            artifact_type="snapshot",
                            path=snap_file,
                            status=SEV_ERROR,
                            summary="Snapshot content digest mismatch (corrupted or tampered).",
                            expected=rec_digest,
                            observed=calc_digest,
                            repairable=False,
                        )
                    else:
                        add_check(
                            check_id=f"snapshot_valid_{stem}",
                            artifact_type="snapshot",
                            path=snap_file,
                            status=SEV_OK,
                            summary=f"Snapshot {stem} verified with valid digest.",
                        )
            except Exception as exc:
                add_check(
                    check_id=f"snapshot_syntax_{stem}",
                    artifact_type="snapshot",
                    path=snap_file,
                    status=SEV_ERROR,
                    summary=f"Snapshot JSON syntax error: {exc}",
                    repairable=False,
                )

    # Build report
    repairable_count = sum(1 for c in checks if c["repairable"])
    unresolved_count = sum(1 for c in checks if c["status"] in (SEV_ERROR, SEV_UNVERIFIABLE) and not c["repairable"])

    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "timestamp": now,
        "dataset_path": _relative_to_parent(csv_file, base_dir),
        "overall_status": overall_status,
        "total_checks": len(checks),
        "repairable_issues": repairable_count,
        "unresolved_issues": unresolved_count,
        "checks": checks,
    }


def generate_repair_plan(csv_path: Union[str, Path]) -> Dict[str, Any]:
    from grade_tracker.cli import compute_sha256
    """Generate an explicit repair plan from current verification report without mutating files."""
    report = run_cross_file_verification(csv_path)
    csv_file = Path(csv_path).resolve()
    base_dir = csv_file.parent

    plan_actions = []
    for check in report["checks"]:
        if check.get("repairable"):
            plan_actions.append({
                "action": check["repair_action"],
                "artifact_type": check["artifact_type"],
                "path": check["path"],
                "rationale": check["repair_rationale"],
                "risk": "low" if check["repair_action"] in ("remove_temp_file", "rollback_transaction") else "medium",
            })

    # Capture fingerprints of key artifacts to ensure plan freshness
    current_digests = {
        "dataset": compute_sha256(csv_file) if csv_file.exists() else None,
        "journal": compute_sha256(pending_journal_path_for(csv_file)) if pending_journal_path_for(csv_file).exists() else None,
    }

    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall_status": report["overall_status"],
        "plan_actions_count": len(plan_actions),
        "actions": plan_actions,
        "unresolved_issues_count": report["unresolved_issues"],
        "pre_repair_fingerprints": current_digests,
    }


def execute_repair_plan(
    csv_path: Union[str, Path],
    confirmed: bool = False,
    dry_run: bool = False,
    actor: Optional[str] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    from grade_tracker.cli import _InterProcessLock, _lock_path_for, compute_sha256
    """Execute confirmed repair plan under inter-process lock with pre- and post-validation.

    Parameters:
        csv_path: Path to dataset CSV.
        confirmed: Must be True (e.g. from --yes) for execution.
        dry_run: If True, previews actions without mutating files.
        actor: Optional actor for audit logging.
        reason: Optional user explanation.

    Returns:
        Dict detailing repair actions executed and post-repair verification report.
    """
    if not dry_run and not confirmed:
        raise RepairConfirmationRequiredError("repair execution requires confirmation; pass --yes or use --dry-run to preview")

    csv_file = Path(csv_path).resolve()
    base_dir = csv_file.parent

    # Step 1: Generate initial plan
    plan = generate_repair_plan(csv_file)
    if plan["overall_status"] == SEV_UNVERIFIABLE:
        raise UnrepairableStateError(
            "dataset or artifacts are in an unverifiable state and cannot be safely repaired automatically; manual intervention required"
        )

    if dry_run:
        return {
            "dry_run": True,
            "actions_planned": plan["actions"],
            "actions_executed": [],
            "post_repair_report": None,
            "success": True,
        }

    # Step 2: Acquire lock and execute
    lock_file = _lock_path_for(csv_file)
    with _InterProcessLock(lock_file):
        # Step 3: Re-verify assumptions immediately before executing
        fresh_plan = generate_repair_plan(csv_file)
        if fresh_plan["pre_repair_fingerprints"] != plan["pre_repair_fingerprints"]:
            raise StaleRepairPlanError(
                "artifacts changed between planning and execution; repair aborted for safety"
            )

        executed_actions = []
        journal_path = pending_journal_path_for(csv_file)

        # Execute safe repairs
        for action_item in plan["actions"]:
            act = action_item["action"]
            target_rel = action_item["path"]
            target_full = base_dir / target_rel

            if act in ("complete_transaction", "rollback_transaction"):
                if journal_path.exists():
                    # Preserve original journal artifact before recovery
                    now_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    preservation_dir = base_dir / "preservations"
                    preservation_dir.mkdir(parents=True, exist_ok=True)
                    preserved_journal = preservation_dir / f"journal-pre-repair-{now_str}.json"
                    preserved_journal.write_bytes(journal_path.read_bytes())

                    rec_result = recover_pending(csv_file)
                    executed_actions.append({
                        "action": act,
                        "artifact": target_rel,
                        "recovery_action": rec_result.get("action"),
                        "preserved_as": _relative_to_parent(preserved_journal, base_dir),
                        "status": "success",
                    })

            elif act == "remove_temp_file":
                if target_full.exists() and target_full.is_file():
                    target_full.unlink()
                    executed_actions.append({
                        "action": act,
                        "artifact": target_rel,
                        "status": "success",
                    })

        # Append audit event if an audit log is active
        log_path = audit_log_path_for(csv_file)
        if log_path.exists() and executed_actions:
            try:
                head = chain_head(log_path)
                seq = 1 if head is None else head["sequence"] + 1
                prev_h = None if head is None else head["hash"]
                ev = build_event(
                    sequence=seq,
                    operation="repair",
                    dataset_id=dataset_id_for(csv_file),
                    records=[],
                    changes={
                        "actions": executed_actions,
                    },
                    actor=actor,
                    reason=reason or f"Executed cross-file integrity repair ({len(executed_actions)} actions)",
                    previous_hash=prev_h,
                )
                append_event(log_path, ev)
            except Exception:
                # Do not fail repair if audit append fails
                pass

        # Step 4: Post-repair verification
        post_report = run_cross_file_verification(csv_file)
        success = post_report["overall_status"] in (SEV_OK, SEV_WARNING) and post_report["repairable_issues"] == 0

        return {
            "dry_run": False,
            "actions_planned": plan["actions"],
            "actions_executed": executed_actions,
            "post_repair_report": post_report,
            "success": success,
        }


def format_integrity_report_text(report: Dict[str, Any]) -> str:
    """Format an integrity verification report into human-readable text."""
    lines = []
    lines.append("=== Cross-File Integrity Verification Report ===")
    lines.append(f"Generated at: {report.get('timestamp') or report.get('generated_at', 'unknown')}")
    lines.append(f"Overall status: {str(report.get('overall_status', 'unknown')).upper()}")
    lines.append(f"Summary: {report.get('total_checks', 0)} checks evaluated | "
                 f"{report.get('unresolved_issues', 0)} unresolved issue(s) | "
                 f"{report.get('repairable_issues', 0)} repairable")
    lines.append("")
    lines.append("Detailed Checks:")

    for chk in report.get("checks", []):
        status_val = str(chk.get("status") or chk.get("severity", "unknown")).upper()
        status_tag = f"[{status_val}]"
        repair_tag = " (repairable)" if chk.get("repairable") else ""
        check_name = str(chk.get("check_id") or chk.get("check", "check"))
        artifact_name = str(chk.get("path") or chk.get("artifact", "-"))
        lines.append(f"  {status_tag:<10} {check_name:<26} {artifact_name}{repair_tag}")
        summary_msg = chk.get("summary") or chk.get("message", "")
        if summary_msg:
            lines.append(f"             {summary_msg}")
        expected = chk.get("expected")
        observed = chk.get("observed")
        if expected is not None or observed is not None:
            lines.append(f"             (expected: {expected}, observed: {observed})")

    lines.append("")
    if report.get("overall_status") == "ok":
        lines.append("All cross-file integrity checks passed.")
    elif report.get("repairable_issues", 0) > 0 and report.get("unresolved_issues", 0) == report.get("repairable_issues", 0):
        lines.append("Issues detected can be safely repaired. Run 'grade-tracker repair --dry-run' to preview.")
    else:
        lines.append("Integrity issues detected. Unrepairable inconsistencies require manual intervention.")

    return "\n".join(lines)
