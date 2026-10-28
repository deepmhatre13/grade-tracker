"""Safe rollback of previous grade mutations using tamper-evident audit history.

This module implements the conflict-checking, validation, and reversal logic
for rolling back previous grade mutations (add, edit, delete, import) recorded
in the audit log, committed as a new compensating audit transaction.
"""

import csv
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from grade_tracker.audit_log import (
    AuditIntegrityError,
    audit_log_path_for,
    dataset_id_for,
    read_events,
    verify_log,
)
from grade_tracker.audit_txn import AuditTransaction
from grade_tracker.calc import (
    assessment_key as calc_assessment_key,
    assessment_record_key as calc_assessment_record_key,
    validate_grade,
)

SUPPORTED_ROLLBACK_OPERATIONS = ("add", "edit", "delete", "import")
CANONICAL_COLUMNS = [
    "student_id",
    "subject",
    "score",
    "max_score",
    "date",
    "credits",
    "semester",
]


class RollbackError(ValueError):
    """Raised when a rollback operation cannot be completed safely."""


class RollbackIntegrityError(RollbackError):
    """Raised when the audit log or dataset integrity is compromised."""


class RollbackConflictError(RollbackError):
    """Raised when an affected record has changed since the mutation or conflicts with existing records."""


def _normalize_num_str(val: Any) -> str:
    if val is None:
        return ""
    try:
        f = float(val)
        return f"{f:g}"
    except (ValueError, TypeError):
        return str(val).strip()


def _records_match_state(current_dict: Dict[str, Any], target_dict: Dict[str, Any]) -> bool:
    """Check if the current CSV record matches the state recorded in target_dict."""
    str_fields = ("student_id", "subject", "date", "semester")
    for f in str_fields:
        c_val = str(current_dict.get(f, "")).strip()
        t_val = str(target_dict.get(f, "")).strip()
        if f == "subject":
            if c_val.casefold() != t_val.casefold():
                return False
        else:
            if c_val != t_val:
                return False

    num_fields = ("score", "max_score", "credits")
    for f in num_fields:
        try:
            c_num = float(current_dict.get(f, 0.0))
            t_num = float(target_dict.get(f, 0.0))
            if c_num != t_num:
                return False
        except (ValueError, TypeError):
            if str(current_dict.get(f, "")).strip() != str(target_dict.get(f, "")).strip():
                return False
    return True


def _record_dict_from_row(row: List[str], header: List[str]) -> Dict[str, str]:
    index_of = {name: idx for idx, name in enumerate(header)}
    def cell(name: str, default: str = "") -> str:
        idx = index_of.get(name)
        if idx is not None and idx < len(row):
            val = row[idx]
            return val if val != "" else default
        return default

    return {
        "student_id": cell("student_id"),
        "subject": cell("subject"),
        "score": cell("score", "0"),
        "max_score": cell("max_score", "0"),
        "date": cell("date"),
        "credits": cell("credits", "3.0"),
        "semester": cell("semester", "1"),
    }


def rollback_mutation(
    csv_path: Path,
    event_id: str,
    dry_run: bool = False,
    confirmed: bool = False,
    actor: Optional[str] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Roll back a previous mutation identified by its audit event_id.

    Parameters:
        csv_path: Path to grades.csv
        event_id: UUID of the audit event to reverse
        dry_run: If True, previews changes without modifying CSV or audit log
        confirmed: If True (e.g. from --yes), executes destructive rollback
        actor: Optional actor name for the compensating audit event
        reason: Optional user-provided reason for rollback

    Returns:
        A dictionary summarizing the rollback action and affected records.

    Raises:
        RollbackError, RollbackIntegrityError, RollbackConflictError, ValueError
    """
    if not dry_run and not confirmed:
        raise RollbackError(
            "rollback requires confirmation; pass --yes to execute rollback, "
            "or --dry-run to preview"
        )

    path = Path(csv_path).resolve()
    from grade_tracker.cli import _InterProcessLock, _lock_path_for, _read_existing_rows

    lock_file = _lock_path_for(path)
    with _InterProcessLock(lock_file):
        log_path = audit_log_path_for(path)
        if not log_path.exists():
            raise RollbackIntegrityError(f"audit log not found for {path}: {log_path}")

        expected_dataset = dataset_id_for(path)
        v = verify_log(log_path, dataset_id=expected_dataset)
        if not v["valid"]:
            inv = v.get("first_invalid", {})
            cat = inv.get("category", "unknown")
            msg = inv.get("message", "audit chain verification failed")
            raise RollbackIntegrityError(
                f"cannot perform rollback on corrupted audit chain ({cat}): {msg}"
            )

        events = v["events"]

        # Check for duplicate rollback of this event_id
        for ev in events:
            if ev.get("operation") == "rollback":
                changes = ev.get("changes") or {}
                if changes.get("rolled_back_event_id") == event_id:
                    raise RollbackConflictError(
                        f"mutation event '{event_id}' has already been rolled back "
                        f"by rollback event '{ev.get('event_id')}' (sequence {ev.get('sequence')})"
                    )

        target_event = None
        for ev in events:
            if ev.get("event_id") == event_id:
                target_event = ev
                break

        if target_event is None:
            raise RollbackError(f"audit event '{event_id}' not found in audit history")

        op = target_event.get("operation")
        if op not in SUPPORTED_ROLLBACK_OPERATIONS:
            raise RollbackError(
                f"operation '{op}' recorded in event '{event_id}' cannot be rolled back; "
                f"supported operations: {', '.join(SUPPORTED_ROLLBACK_OPERATIONS)}"
            )

        if not path.exists():
            raise RollbackError(f"grades data file not found at {path}")

        file_exists, rows = _read_existing_rows(path)
        if not file_exists:
            raise RollbackError(f"grades data file not found at {path}")

        header = CANONICAL_COLUMNS
        row_dicts = [_record_dict_from_row(r, header) for r in rows]

        records_info = []
        proposed_rows = list(rows)

        if op == "add":
            after_rec = None
            changes = target_event.get("changes") or {}
            if isinstance(changes.get("after"), dict):
                after_rec = changes["after"]
            elif target_event.get("records") and isinstance(target_event["records"][0], dict):
                after_rec = target_event["records"][0]

            if not after_rec:
                raise RollbackError(
                    f"event '{event_id}' (add) lacks sufficient record details to reverse"
                )

            # Match in current CSV
            matches = [
                idx for idx, rd in enumerate(row_dicts)
                if _records_match_state(rd, after_rec)
            ]
            if not matches:
                target_desc = (
                    f"student '{after_rec.get('student_id')}', subject '{after_rec.get('subject')}', "
                    f"semester {after_rec.get('semester')}, date {after_rec.get('date')}"
                )
                raise RollbackConflictError(
                    f"conflict: added record ({target_desc}) has been modified or deleted "
                    "since the original mutation. Inspect the current dataset."
                )
            if len(matches) > 1:
                target_desc = (
                    f"student '{after_rec.get('student_id')}', subject '{after_rec.get('subject')}', "
                    f"semester {after_rec.get('semester')}, date {after_rec.get('date')}"
                )
                raise RollbackConflictError(
                    f"conflict: multiple identical records ({target_desc}) match the added state; "
                    "cannot ambiguously determine which record to remove."
                )

            remove_idx = matches[0]
            removed_row = proposed_rows.pop(remove_idx)
            records_info.append(after_rec)
            compensating_changes = {
                "rolled_back_event_id": event_id,
                "rolled_back_operation": op,
                "before": after_rec,
                "after": None,
            }

        elif op == "edit":
            changes = target_event.get("changes") or {}
            before_rec = changes.get("before")
            after_rec = changes.get("after")

            if not isinstance(before_rec, dict) or not isinstance(after_rec, dict):
                raise RollbackError(
                    f"event '{event_id}' (edit) lacks complete before-and-after change history; "
                    "cannot reverse safely."
                )

            matches = [
                idx for idx, rd in enumerate(row_dicts)
                if _records_match_state(rd, after_rec)
            ]
            if not matches:
                target_desc = (
                    f"student '{after_rec.get('student_id')}', subject '{after_rec.get('subject')}', "
                    f"semester {after_rec.get('semester')}, date {after_rec.get('date')}"
                )
                raise RollbackConflictError(
                    f"conflict: edited record ({target_desc}) has changed since the original edit. "
                    "Inspect the current dataset."
                )
            if len(matches) > 1:
                target_desc = (
                    f"student '{after_rec.get('student_id')}', subject '{after_rec.get('subject')}', "
                    f"semester {after_rec.get('semester')}, date {after_rec.get('date')}"
                )
                raise RollbackConflictError(
                    f"conflict: multiple records match the edited state ({target_desc}); "
                    "cannot safely determine which record to reverse."
                )

            target_idx = matches[0]

            # Validate that before_rec is valid
            validate_grade(
                float(before_rec["score"]),
                float(before_rec["max_score"]),
                before_rec["date"],
                float(before_rec.get("credits", 3.0)),
            )

            # Check if restoring before_rec would duplicate an existing distinct record
            restored_key = calc_assessment_key(before_rec)
            restored_rec_key = calc_assessment_record_key({
                **before_rec,
                "score": float(before_rec["score"]),
                "max_score": float(before_rec["max_score"]),
                "credits": float(before_rec.get("credits", 3.0)),
            })

            for idx, rd in enumerate(row_dicts):
                if idx == target_idx:
                    continue
                other_key = calc_assessment_key(rd)
                if other_key == restored_key:
                    other_rec_key = calc_assessment_record_key({
                        **rd,
                        "score": float(rd["score"]),
                        "max_score": float(rd["max_score"]),
                        "credits": float(rd.get("credits", 3.0)),
                    })
                    if other_rec_key == restored_rec_key or other_key != calc_assessment_key(after_rec):
                        raise RollbackConflictError(
                            f"conflict: restoring previous values for student '{before_rec.get('student_id')}' "
                            f"in {before_rec.get('subject')} on {before_rec.get('date')} would duplicate "
                            "an existing assessment. Inspect the current dataset."
                        )

            # Reconstruct the row
            new_row = [str(before_rec.get(col, "")) for col in header]
            proposed_rows[target_idx] = new_row
            records_info.append(before_rec)
            compensating_changes = {
                "rolled_back_event_id": event_id,
                "rolled_back_operation": op,
                "before": after_rec,
                "after": before_rec,
            }

        elif op == "delete":
            changes = target_event.get("changes") or {}
            deleted_list = None
            if isinstance(changes.get("before"), list):
                deleted_list = changes["before"]
            elif isinstance(changes.get("before"), dict):
                deleted_list = [changes["before"]]
            elif target_event.get("records"):
                deleted_list = target_event["records"]

            if not deleted_list:
                raise RollbackError(
                    f"event '{event_id}' (delete) lacks deleted record history; cannot restore."
                )

            records_to_restore = []
            for item in deleted_list:
                if not isinstance(item, dict):
                    continue
                validate_grade(
                    float(item["score"]),
                    float(item["max_score"]),
                    item["date"],
                    float(item.get("credits", 3.0)),
                )
                records_to_restore.append(item)

            if not records_to_restore:
                raise RollbackError(
                    f"event '{event_id}' (delete) contains no valid deleted records to restore."
                )

            # Check that restored identities do not already exist
            existing_identities = set()
            for rd in row_dicts:
                k = calc_assessment_key(rd)
                if k:
                    existing_identities.add(k)

            for rec in records_to_restore:
                k = calc_assessment_key(rec)
                if k in existing_identities:
                    raise RollbackConflictError(
                        f"conflict: assessment identity already exists for student '{rec.get('student_id')}' "
                        f"in {rec.get('subject')} (semester {rec.get('semester')}) on {rec.get('date')}; "
                        "cannot restore deleted record without creating duplicate."
                    )
                existing_identities.add(k)
                row = [str(rec.get(col, "")) for col in header]
                proposed_rows.append(row)
                records_info.append(rec)

            compensating_changes = {
                "rolled_back_event_id": event_id,
                "rolled_back_operation": op,
                "before": None,
                "after": records_to_restore,
            }

        elif op == "import":
            changes = target_event.get("changes") or {}
            imported_list = None
            if isinstance(changes.get("after"), list):
                imported_list = changes["after"]
            elif target_event.get("records"):
                imported_list = target_event["records"]

            if not imported_list:
                raise RollbackError(
                    f"event '{event_id}' (import) lacks imported record list; cannot reverse."
                )

            # Match and delete each imported record
            to_remove_indices = []
            current_row_dicts = list(row_dicts)

            for rec in imported_list:
                if not isinstance(rec, dict):
                    continue
                matches = [
                    idx for idx, rd in enumerate(current_row_dicts)
                    if idx not in to_remove_indices and _records_match_state(rd, rec)
                ]
                if not matches:
                    target_desc = (
                        f"student '{rec.get('student_id')}', subject '{rec.get('subject')}', "
                        f"semester {rec.get('semester')}, date {rec.get('date')}"
                    )
                    raise RollbackConflictError(
                        f"conflict: imported record ({target_desc}) has been modified or deleted "
                        "since the import. Inspect the current dataset."
                    )
                if len(matches) > 1:
                    target_desc = (
                        f"student '{rec.get('student_id')}', subject '{rec.get('subject')}', "
                        f"semester {rec.get('semester')}, date {rec.get('date')}"
                    )
                    raise RollbackConflictError(
                        f"conflict: multiple records match imported state ({target_desc}); "
                        "cannot unambiguously reverse import."
                    )
                to_remove_indices.append(matches[0])
                records_info.append(rec)

            # Remove rows in reverse order of index
            for idx in sorted(to_remove_indices, reverse=True):
                proposed_rows.pop(idx)

            compensating_changes = {
                "rolled_back_event_id": event_id,
                "rolled_back_operation": op,
                "before": imported_list,
                "after": None,
            }

        else:
            raise RollbackError(f"unsupported rollback operation '{op}'")

        # Validate complete proposed dataset
        for row in proposed_rows:
            if not row or not any(c.strip() for c in row):
                continue
            rd = _record_dict_from_row(row, header)
            validate_grade(
                float(rd["score"]),
                float(rd["max_score"]),
                rd["date"],
                float(rd.get("credits", 3.0)),
            )

        if dry_run:
            return {
                "dry_run": True,
                "event_id": event_id,
                "operation": op,
                "records_affected": len(records_info),
                "records": records_info,
            }

        # Destructive execution inside AuditTransaction
        rollback_reason = reason or f"Rollback of {op} mutation (event {event_id})"
        with AuditTransaction(
            path,
            operation="rollback",
            records=records_info,
            changes=compensating_changes,
            actor=actor,
            reason=rollback_reason,
            manage_lock=False,
        ) as txn:
            txn.write_dataset(proposed_rows)

        return {
            "dry_run": False,
            "event_id": event_id,
            "operation": op,
            "records_affected": len(records_info),
            "records": records_info,
        }

