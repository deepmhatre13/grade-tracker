"""Reproducible GPA calculation snapshots for Grade Tracker.

This module provides versioned snapshots of SGPA and CGPA calculations,
capturing exact immutable input grade records, complete effective grading policy
configurations, calculation metadata, algorithm versions, and content integrity
hashes. Previously computed results can be independently verified and reproduced
even if current records, active grading policies, or future calculation logic change.
"""

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, List, Optional, Tuple, Union

from grade_tracker.calc import (
    calculate_gpa_breakdown,
    cgpa,
    letter_grade,
    percentage,
    sgpa,
)
from grade_tracker.policies import (
    Policy,
    resolve_policy,
)

SNAPSHOT_SCHEMA_VERSION = "1.0"
SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS = ("1.0",)

ALGORITHM_VERSION = "1.0"
SUPPORTED_ALGORITHM_VERSIONS = ("1.0",)

SUPPORTED_CALCULATION_TYPES = ("sgpa", "cgpa")

DIGEST_EXCLUDED_FIELDS = ("content_digest",)


class SnapshotError(Exception):
    """Base exception for snapshot errors."""


class SnapshotNotFoundError(SnapshotError):
    """Raised when a requested snapshot does not exist."""


class SnapshotExistsError(SnapshotError):
    """Raised when attempting to create a snapshot with an ID that already exists."""


class SnapshotIntegrityError(SnapshotError):
    """Raised when snapshot content has been tampered with or corrupted."""


class UnsupportedVersionError(SnapshotError):
    """Raised when snapshot schema, algorithm, or policy version is unsupported."""


class SnapshotCalculationError(SnapshotError):
    """Raised when snapshot inputs or parameters are invalid for calculation."""


def snapshot_dir_for(csv_path: Union[str, Path]) -> Path:
    """Return the snapshots directory path associated with csv_path."""
    p = Path(csv_path).resolve()
    return p.parent / "snapshots"


def snapshot_path_for(csv_path: Union[str, Path], snapshot_id: str) -> Path:
    """Return the file path for a specific snapshot ID."""
    s_id = str(snapshot_id).strip()
    return snapshot_dir_for(csv_path) / f"{s_id}.json"


def canonical_json_bytes(payload: Dict[str, Any], exclude_fields: Tuple[str, ...] = ()) -> bytes:
    """Serialize dictionary to canonical JSON bytes with sorted keys and compact separators."""
    data = {k: v for k, v in payload.items() if k not in exclude_fields}
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def compute_snapshot_digest(snapshot_data: Dict[str, Any]) -> str:
    """Compute SHA-256 hex digest of canonical snapshot bytes excluding content_digest."""
    canonical_bytes = canonical_json_bytes(snapshot_data, exclude_fields=DIGEST_EXCLUDED_FIELDS)
    return hashlib.sha256(canonical_bytes).hexdigest()


def reconstruct_policy_from_snapshot(policy_dict: Dict[str, Any]) -> Policy:
    """Reconstruct an immutable Policy instance from snapshot's preserved policy dictionary."""
    if not isinstance(policy_dict, dict):
        raise UnsupportedVersionError("snapshot policy configuration is missing or malformed")

    raw_thresholds = policy_dict.get("thresholds")
    if not isinstance(raw_thresholds, list) or not raw_thresholds:
        raise UnsupportedVersionError("snapshot policy thresholds are missing or malformed")

    thresholds_list = []
    for item in raw_thresholds:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            thresholds_list.append((float(item[0]), str(item[1])))
        elif isinstance(item, dict) and "percentage" in item and "letter" in item:
            thresholds_list.append((float(item["percentage"]), str(item["letter"])))
        else:
            raise UnsupportedVersionError(f"invalid threshold entry in snapshot policy: {item!r}")

    grade_points = policy_dict.get("grade_points")
    if not isinstance(grade_points, dict) or not grade_points:
        raise UnsupportedVersionError("snapshot policy grade_points are missing or malformed")

    points_dict = {str(k): float(v) for k, v in grade_points.items()}

    try:
        return Policy(
            identifier=str(policy_dict.get("identifier", "snapshot_policy")),
            version=str(policy_dict.get("version", "1")),
            thresholds=tuple(thresholds_list),
            grade_points=points_dict,
            gpa_rounding=policy_dict.get("gpa_rounding", 2),
            gpa_rounding_mode=policy_dict.get("gpa_rounding_mode", "python"),
            percentage_rounding=policy_dict.get("percentage_rounding"),
            failing_letter=str(policy_dict.get("failing_letter", "F")),
            description=str(policy_dict.get("description", "")),
        )
    except Exception as exc:
        raise UnsupportedVersionError(f"failed to reconstruct snapshot policy: {exc}") from exc


def normalize_input_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure record contains standardized keys and types for snapshot storage."""
    student_id = str(record.get("student_id", "")).strip()
    subject = str(record.get("subject", "")).strip()
    semester = str(record.get("semester", "1")).strip()
    date_val = str(record.get("date", "")).strip()

    try:
        score = float(record["score"])
        max_score = float(record["max_score"])
    except (KeyError, ValueError, TypeError) as exc:
        raise SnapshotCalculationError(f"invalid numeric score/max_score in record: {record!r}") from exc

    raw_credits = record.get("credits", 3.0)
    try:
        credits_val = float(raw_credits) if raw_credits is not None and str(raw_credits).strip() != "" else 3.0
    except (ValueError, TypeError):
        credits_val = 3.0

    return {
        "student_id": student_id,
        "subject": subject,
        "score": score,
        "max_score": max_score,
        "date": date_val,
        "credits": credits_val,
        "semester": semester,
    }


def create_snapshot(
    csv_path: Union[str, Path],
    calculation_type: str,
    student_id: str,
    semester: Optional[Union[str, int]] = None,
    policy: Optional[Any] = None,
    snapshot_id: Optional[str] = None,
    description: Optional[str] = None,
    record_audit: bool = True,
) -> Dict[str, Any]:
    """Create a new GPA snapshot under inter-process lock and persist it atomically.

    Parameters:
        csv_path: Path to grades CSV.
        calculation_type: 'sgpa' or 'cgpa'.
        student_id: Student identifier.
        semester: Semester identifier (required for 'sgpa', optional for 'cgpa').
        policy: Policy instance, name, or None for default.
        snapshot_id: Optional unique snapshot ID (generated if None).
        description: Optional notes describing snapshot purpose.
        record_audit: Whether to append a 'snapshot' event to the audit log if active.

    Returns:
        The complete persisted snapshot dictionary.
    """
    calc_type = str(calculation_type).strip().lower()
    if calc_type not in SUPPORTED_CALCULATION_TYPES:
        raise SnapshotCalculationError(
            f"unsupported calculation type {calculation_type!r}; "
            f"must be one of {', '.join(SUPPORTED_CALCULATION_TYPES)}"
        )

    s_id = str(student_id).strip()
    if not s_id:
        raise SnapshotCalculationError("student_id must not be empty")

    sem_str = str(semester).strip() if semester is not None else None
    if calc_type == "sgpa" and not sem_str:
        raise SnapshotCalculationError("semester is required for SGPA calculation snapshot")

    csv_path = Path(csv_path).resolve()
    from grade_tracker.cli import _InterProcessLock, _lock_path_for, load_grades

    lock_file = _lock_path_for(csv_path)
    with _InterProcessLock(lock_file):
        # Resolve effective grading policy
        effective_policy = resolve_policy(policy)

        # Load live grades from CSV
        all_grades = load_grades(csv_path)

        # Filter relevant records for student and optional semester
        student_grades = [g for g in all_grades if str(g.get("student_id", "")).strip() == s_id]
        if not student_grades:
            raise SnapshotCalculationError(f"no grade records found for student {s_id!r}")

        if calc_type == "sgpa":
            input_records = [
                g for g in student_grades if str(g.get("semester", "")).strip() == sem_str
            ]
            if not input_records:
                raise SnapshotCalculationError(
                    f"no grade records found for student {s_id!r} in semester {sem_str!r}"
                )
        else:
            input_records = student_grades
            if sem_str is not None:
                # Optional semester filtering on CGPA if ever requested
                input_records = [
                    g for g in input_records if str(g.get("semester", "")).strip() == sem_str
                ]

        normalized_inputs = [normalize_input_record(g) for g in input_records]

        # Run canonical calculation breakdown
        breakdown = calculate_gpa_breakdown(normalized_inputs, policy=effective_policy)
        calculated_gpa = breakdown["gpa"]
        total_points = breakdown["total_points"]
        total_credits = breakdown["total_credits"]

        # Generate or validate snapshot ID
        now = datetime.now(timezone.utc)
        timestamp_str = now.strftime("%Y%m%dT%H%M%SZ")
        timestamp_iso = now.isoformat()

        if snapshot_id is None:
            scope = f"sem{sem_str}" if sem_str is not None else "cum"
            counter = 0
            while True:
                suffix = f"-{counter}" if counter > 0 else ""
                candidate_id = f"snap-{s_id}-{calc_type}-{scope}-{timestamp_str}{suffix}"
                target_path = snapshot_path_for(csv_path, candidate_id)
                if not target_path.exists():
                    snap_id = candidate_id
                    break
                counter += 1
        else:
            snap_id = str(snapshot_id).strip()
            if not snap_id:
                raise SnapshotError("snapshot_id must not be empty")
            target_path = snapshot_path_for(csv_path, snap_id)
            if target_path.exists():
                raise SnapshotExistsError(f"snapshot with ID {snap_id!r} already exists at {target_path}")

        policy_config = effective_policy.to_dict()

        snapshot_payload = {
            "snapshot_id": snap_id,
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "created_at": timestamp_iso,
            "student_id": s_id,
            "calculation_type": calc_type,
            "semester": sem_str,
            "input_records": normalized_inputs,
            "policy": policy_config,
            "result": {
                "gpa": calculated_gpa,
                "total_points": total_points,
                "total_credits": total_credits,
                "precision": effective_policy.gpa_rounding,
                "rounding_mode": effective_policy.gpa_rounding_mode,
            },
            "description": description or "",
        }

        # Compute content digest
        digest = compute_snapshot_digest(snapshot_payload)
        snapshot_payload["content_digest"] = digest

        # Atomically write snapshot file
        snap_dir = snapshot_dir_for(csv_path)
        snap_dir.mkdir(parents=True, exist_ok=True)

        final_bytes = (json.dumps(snapshot_payload, indent=2) + "\n").encode("utf-8")
        tmp_name = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(snap_dir), prefix=f".snap-{snap_id}-", suffix=".tmp"
            )
            with os.fdopen(fd, "wb") as f:
                f.write(final_bytes)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, target_path)
            tmp_name = None
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

        # Optionally append audit event to audit log
        if record_audit:
            try:
                from grade_tracker.audit_log import (
                    append_event,
                    audit_log_path_for,
                    build_event,
                    chain_head,
                    dataset_id_for,
                )

                log_path = audit_log_path_for(csv_path)
                if log_path.exists():
                    head = chain_head(log_path)
                    seq = 1 if head is None else head["sequence"] + 1
                    prev_hash = None if head is None else head["hash"]
                    audit_records = [
                        {
                            "student_id": s_id,
                            "calculation_type": calc_type,
                            "semester": sem_str,
                            "snapshot_id": snap_id,
                            "gpa": calculated_gpa,
                        }
                    ]
                    changes = {
                        "snapshot_id": snap_id,
                        "calculation_type": calc_type,
                        "gpa": calculated_gpa,
                        "content_digest": digest,
                    }
                    ev = build_event(
                        sequence=seq,
                        operation="snapshot",
                        dataset_id=dataset_id_for(csv_path),
                        records=audit_records,
                        changes=changes,
                        reason=f"Created GPA snapshot {snap_id}",
                        previous_hash=prev_hash,
                    )
                    append_event(log_path, ev)
            except Exception:
                # Do not fail snapshot creation if audit log is disabled or fails
                pass

        return snapshot_payload


def load_raw_snapshot(csv_path: Union[str, Path], snapshot_id: str) -> Dict[str, Any]:
    """Load and return the raw JSON dict of a snapshot without verifying recalculation.

    Raises:
        SnapshotNotFoundError: If snapshot file does not exist.
        SnapshotIntegrityError: If JSON is malformed.
    """
    path = snapshot_path_for(csv_path, snapshot_id)
    if not path.exists():
        raise SnapshotNotFoundError(f"snapshot '{snapshot_id}' not found at {path}")

    try:
        with open(path, mode="r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise SnapshotIntegrityError(f"snapshot file '{snapshot_id}' is malformed: {exc}") from exc

    if not isinstance(data, dict):
        raise SnapshotIntegrityError(f"snapshot '{snapshot_id}' root is not a JSON object")

    return data


def inspect_snapshot(csv_path: Union[str, Path], snapshot_id: str) -> Dict[str, Any]:
    """Inspect snapshot metadata and verify content digest integrity (read-only).

    Does not recalculate GPA, but verifies SHA-256 content digest matches payload.

    Returns:
        Dict with snapshot details and integrity status.
    """
    data = load_raw_snapshot(csv_path, snapshot_id)

    # Check schema version
    schema_ver = str(data.get("schema_version", ""))
    schema_supported = schema_ver in SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS

    # Check algorithm version
    algo_ver = str(data.get("algorithm_version", ""))
    algo_supported = algo_ver in SUPPORTED_ALGORITHM_VERSIONS

    # Check content digest
    recorded_digest = data.get("content_digest")
    if not recorded_digest or not isinstance(recorded_digest, str):
        digest_valid = False
        computed_digest = None
    else:
        computed_digest = compute_snapshot_digest(data)
        digest_valid = computed_digest.lower() == recorded_digest.lower()

    policy_info = data.get("policy") or {}
    result_info = data.get("result") or {}
    inputs = data.get("input_records") or []

    return {
        "snapshot_id": data.get("snapshot_id", snapshot_id),
        "schema_version": schema_ver,
        "schema_supported": schema_supported,
        "algorithm_version": algo_ver,
        "algorithm_supported": algo_supported,
        "created_at": data.get("created_at"),
        "student_id": data.get("student_id"),
        "calculation_type": data.get("calculation_type"),
        "semester": data.get("semester"),
        "input_record_count": len(inputs),
        "policy_name": policy_info.get("name"),
        "policy_identifier": policy_info.get("identifier"),
        "policy_version": policy_info.get("version"),
        "recorded_gpa": result_info.get("gpa"),
        "recorded_total_points": result_info.get("total_points"),
        "recorded_total_credits": result_info.get("total_credits"),
        "content_digest": recorded_digest,
        "digest_valid": digest_valid,
        "description": data.get("description", ""),
    }


def reproduce_snapshot(csv_path: Union[str, Path], snapshot_id: str) -> Dict[str, Any]:
    """Reproduce GPA calculation using ONLY preserved historical inputs and policy.

    Guarantees:
    - Never uses live grade records or active policy.
    - Validates schema and algorithm versions.
    - Validates content digest integrity before recalculating.
    - Reconstructs preserved policy and executes canonical calculate_gpa_breakdown.
    - Compares reproduced result with recorded result.

    Returns:
        Dict detailing the reproduction result, match status, and intermediate breakdown.
    """
    data = load_raw_snapshot(csv_path, snapshot_id)

    # 1. Verify schema version
    schema_ver = str(data.get("schema_version", ""))
    if schema_ver not in SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS:
        raise UnsupportedVersionError(
            f"unsupported snapshot schema version {schema_ver!r} "
            f"(supported: {', '.join(SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS)})"
        )

    # 2. Verify algorithm version
    algo_ver = str(data.get("algorithm_version", ""))
    if algo_ver not in SUPPORTED_ALGORITHM_VERSIONS:
        raise UnsupportedVersionError(
            f"unsupported calculation algorithm version {algo_ver!r} "
            f"(supported: {', '.join(SUPPORTED_ALGORITHM_VERSIONS)})"
        )

    # 3. Verify content digest integrity
    recorded_digest = data.get("content_digest")
    if not recorded_digest or not isinstance(recorded_digest, str):
        raise SnapshotIntegrityError(f"snapshot '{snapshot_id}' is missing content_digest")

    computed_digest = compute_snapshot_digest(data)
    if computed_digest.lower() != recorded_digest.lower():
        raise SnapshotIntegrityError(
            f"snapshot '{snapshot_id}' content digest mismatch: "
            f"expected {recorded_digest}, got {computed_digest} (data corrupted or tampered)"
        )

    # 4. Extract preserved inputs
    inputs = data.get("input_records")
    if not isinstance(inputs, list):
        raise SnapshotIntegrityError(f"snapshot '{snapshot_id}' input_records must be a list")

    if not inputs:
        raise SnapshotCalculationError(f"snapshot '{snapshot_id}' has empty input_records")

    normalized_inputs = []
    for idx, rec in enumerate(inputs):
        if not isinstance(rec, dict):
            raise SnapshotIntegrityError(f"snapshot input record at index {idx} is not an object")
        normalized_inputs.append(normalize_input_record(rec))

    # 5. Reconstruct preserved policy
    policy_dict = data.get("policy")
    if not isinstance(policy_dict, dict):
        raise UnsupportedVersionError(f"snapshot '{snapshot_id}' is missing policy configuration")

    preserved_policy = reconstruct_policy_from_snapshot(policy_dict)

    # 6. Recalculate using canonical path with preserved inputs and policy
    breakdown = calculate_gpa_breakdown(normalized_inputs, policy=preserved_policy)

    reproduced_gpa = breakdown["gpa"]
    reproduced_points = breakdown["total_points"]
    reproduced_credits = breakdown["total_credits"]

    # 7. Compare with recorded result
    result_dict = data.get("result") or {}
    recorded_gpa = result_dict.get("gpa")
    recorded_points = result_dict.get("total_points")
    recorded_credits = result_dict.get("total_credits")

    gpa_matches = (
        (reproduced_gpa is None and recorded_gpa is None)
        or (
            reproduced_gpa is not None
            and recorded_gpa is not None
            and abs(float(reproduced_gpa) - float(recorded_gpa)) < 1e-9
        )
    )

    points_matches = (
        recorded_points is not None
        and abs(float(reproduced_points) - float(recorded_points)) < 1e-9
    )

    credits_matches = (
        recorded_credits is not None
        and abs(float(reproduced_credits) - float(recorded_credits)) < 1e-9
    )

    is_reproducible = gpa_matches and points_matches and credits_matches

    return {
        "snapshot_id": data.get("snapshot_id", snapshot_id),
        "calculation_type": data.get("calculation_type"),
        "student_id": data.get("student_id"),
        "semester": data.get("semester"),
        "reproduced": is_reproducible,
        "recorded_gpa": recorded_gpa,
        "reproduced_gpa": reproduced_gpa,
        "recorded_points": recorded_points,
        "reproduced_points": reproduced_points,
        "recorded_credits": recorded_credits,
        "reproduced_credits": reproduced_credits,
        "policy_name": preserved_policy.name,
        "algorithm_version": algo_ver,
        "schema_version": schema_ver,
        "records_count": len(normalized_inputs),
        "breakdown": breakdown,
    }


def list_snapshots(csv_path: Union[str, Path]) -> List[Dict[str, Any]]:
    """Discover and list all available snapshots for csv_path.

    Returns:
        Sorted list of summary dictionaries.
    """
    s_dir = snapshot_dir_for(csv_path)
    if not s_dir.exists():
        return []

    results = []
    for f in sorted(s_dir.glob("*.json")):
        snapshot_id = f.stem
        try:
            info = inspect_snapshot(csv_path, snapshot_id)
            results.append(info)
        except Exception:
            results.append({
                "snapshot_id": snapshot_id,
                "schema_version": "unknown",
                "schema_supported": False,
                "algorithm_version": "unknown",
                "algorithm_supported": False,
                "created_at": None,
                "student_id": "corrupted",
                "calculation_type": "unknown",
                "semester": None,
                "input_record_count": 0,
                "policy_name": "unknown",
                "recorded_gpa": None,
                "digest_valid": False,
                "description": "Corrupted or unreadable snapshot file",
            })

    return results

