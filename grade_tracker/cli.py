"""Command Line Interface for Grade Tracker."""

import argparse
import csv
import io
import json
import math
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path

from grade_tracker.calc import (
    assessment_key as calc_assessment_key,
    assessment_record_key as calc_assessment_record_key,
    calculate_target_sgpa,
    cgpa,
    explain_student_gpa,
    letter_grade,
    percentage,
    sgpa,
    subject_average,
    validate_grade,
)
from grade_tracker.audit_log import (
    OPERATIONS as AUDIT_OPERATIONS,
    audit_log_path_for,
    dataset_id_for,
    filter_events,
    verify_log,
)
from grade_tracker.audit_txn import (
    AuditTransaction,
    AuditTransactionError,
    recover_pending,
)
from grade_tracker.rollback import (
    RollbackError,
    RollbackIntegrityError,
    RollbackConflictError,
    rollback_mutation,
)
from grade_tracker.snapshot import (
    SnapshotError,
    SnapshotNotFoundError,
    SnapshotExistsError,
    SnapshotIntegrityError,
    UnsupportedVersionError,
    SnapshotCalculationError,
    create_snapshot,
    inspect_snapshot,
    reproduce_snapshot,
    list_snapshots,
)
from grade_tracker.export import build_html
from grade_tracker.policies import (
    UnknownPolicyError,
    get_default_registry,
    resolve_policy,
)

DEFAULT_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "grades.csv"
BACKUP_FORMAT_VERSION = "1.0"
SUPPORTED_CSV_SCHEMA_VERSION = "1.0"
CANONICAL_COLUMNS = [
    "student_id",
    "subject",
    "score",
    "max_score",
    "date",
    "credits",
    "semester",
]

# Output modes for read-only commands.  "human" preserves the original text
# output; "json" emits one JSON document on stdout and sends diagnostics to
# stderr.
HUMAN_FORMAT = "human"
JSON_FORMAT = "json"




def _format_name(args, default=HUMAN_FORMAT):
    """Return the requested output format, defaulting to human-readable text.

    Older commands do not have a ``format`` attribute when invoked directly in
    tests, so the helper accepts both the argparse namespace and objects that
    simply omit the option.
    """
    return getattr(args, "format", default) or default


def _emit_json(payload):
    """Write exactly one machine-readable JSON document to stdout.

    The payload is serialized with :mod:`json` rather than assembled manually.
    ``sort_keys=True`` and a fixed separator keep output deterministic for
    scripts and tests; ``ensure_ascii=False`` preserves readable subject names
    while remaining valid UTF-8 JSON.
    """
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _json_input_error(exc):
    """Convert a CSV/Unicode/OSError input failure into a JSON-ready payload."""
    return {
        "command": None,
        "error": {
            "code": "input_error",
            "message": str(exc),
        },
    }


def _load_grades_for_json(csv_path, command):
    """Load grades for JSON mode, refusing malformed input instead of hiding it.

    ``load_grades`` intentionally skips legacy/malformed rows for the original
    human-readable commands. That behavior is unsafe for machine consumers,
    because a malformed CSV could look like a valid empty dataset. JSON mode
    therefore raises a caller-visible error for malformed visible data rows.

    A file that does not exist is treated as an empty dataset, matching the
    existing loader contract. ``validate --format json`` reports the missing
    file with its normal exit code 1.
    """
    path = Path(csv_path)
    if not path.exists():
        return []

    grades = []
    try:
        with open(path, mode="r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = reader.fieldnames
            required = ("student_id", "subject", "score", "max_score", "date")
            if fieldnames is None:
                raise ValueError(f"{path}: file is empty (no CSV header)")
            normalized_fieldnames = [
                fieldname.lstrip("\ufeff").strip() if fieldname else fieldname
                for fieldname in fieldnames
            ]
            missing = [name for name in required if name not in normalized_fieldnames]
            if missing:
                raise ValueError(
                    f"{path}: missing required column(s): {', '.join(missing)}"
                )
            if len(set(normalized_fieldnames)) != len(normalized_fieldnames):
                raise ValueError(f"{path}: duplicate CSV column name")
            normalized_rows = [
                dict(zip(normalized_fieldnames, row.values())) for row in reader
            ]

            for row_number, row in enumerate(normalized_rows, start=2):
                if not row:
                    continue
                raw_values = [value for value in row.values() if value is not None]
                if not any(value.strip() for value in raw_values):
                    continue  # Blank physical lines are ignored, as in the loader.
                try:
                    grades.append(
                        {
                            "student_id": row["student_id"].strip(),
                            "subject": row["subject"].strip(),
                            "score": float(row["score"]),
                            "max_score": float(row["max_score"]),
                            "date": row.get("date", "").strip(),
                            "credits": float(row.get("credits", 3.0)),
                            "semester": str(row.get("semester", "1")).strip(),
                        }
                    )
                except (TypeError, ValueError, KeyError) as exc:
                    raise ValueError(
                        f"{path}: row {row_number} cannot be loaded ({exc}); "
                        "run 'validate' for details"
                    ) from exc
    except (UnicodeDecodeError, OSError, csv.Error) as exc:
        raise ValueError(f"{path}: cannot read grades CSV ({exc})") from exc

    return grades


def _grade_json_record(grade, semester=None, policy=None):
    """Serialize one loaded grade record with stable field names and types."""
    semester_value = grade["semester"] if semester is None else semester
    return {
        "student_id": grade["student_id"],
        "subject": grade["subject"],
        "score": grade["score"],
        "max_score": grade["max_score"],
        "date": grade["date"] or None,
        "credits": grade["credits"],
        "semester": semester_value,
        "percentage": round(float(percentage(grade["score"], grade["max_score"])), 1)
        if grade["max_score"] > 0
        else None,
        "letter_grade": letter_grade(
            grade["score"], grade["max_score"], policy=policy
        )
        if grade["max_score"] > 0
        else None,
    }


def _lock_path_for(csv_path):
    """Stable lock file that lives next to the target CSV.

    The lock file is a real file whose identity is fixed for the lifetime of
    the CSV.  Its path is derived from a hash of the CSV's resolved absolute
    path so that two processes pointing at the same CSV always use the same
    lock object, regardless of how the path is written (relative, .., or
    different drive letters).
    """
    resolved = Path(csv_path).resolve()
    # Serialize the resolved path to bytes in a platform-independent way so
    # that two processes referencing the same CSV (with different spellings
    # such as relative vs absolute, or different drive-letter cases) always
    # compute the same lock file.
    raw = resolved.resolve().as_posix().encode("utf-8")
    digest = 0
    for byte in raw:
        digest = (digest * 31 + byte) & 0xFFFFFFFF
    name = "grades-{0:08x}.lock".format(digest)
    return resolved.parent / name


class _InterProcessLock:
    """Cross-process lock backed by a stable lock file.

    Uses the operating system's advisory locking primitives:

    * Windows: ``msvcrt.locking`` on the lock file (shared/exclusive byte
      locking), which works for independent OS processes.
    * Unix: ``fcntl.flock`` on the lock file, which also works across
      independent processes (including fork), and is the standard approach.

    The lock is *advisory*: every writer in this project must acquire it, but
    the operating system honours the lock semantics.  If a future writer
    bypasses this class it will simply not be protected; that is a project
    policy, not a correctness failure of the lock itself.

    ``timeout`` (seconds) bounds how long a blocking acquire may wait.  A
    value of ``None`` blocks indefinitely.  Windows' ``msvcrt.locking`` has no
    native timeout, so the implementation waits with short sleeps and aborts
    only once the wall-clock budget is exhausted, instead of busy-looping
    forever.  ``fcntl.flock`` supports a non-blocking flag plus a retry loop,
    which is also bounded by ``timeout``.
    """

    def __init__(self, path, timeout=None):
        self.path = Path(path)
        self.timeout = timeout
        self._fd = None
        self._file = None

    def _cleanup(self):
        """Close the underlying fd and file, ignoring any error.

        Closing always releases the operating-system lock on both platforms
        (Windows releases ``msvcrt.locking`` bytes on close; Unix releases
        ``fcntl.flock`` on close).  This guarantees a failed acquisition or an
        exception unwinding the lock cannot leave a lock stranded.
        """
        fd, file = self._fd, self._file
        self._fd = None
        self._file = None
        if file is not None:
            try:
                file.close()
            except Exception:
                pass
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass

    def acquire(self, timeout=None):
        if self._file is not None:
            return

        if timeout is None:
            timeout = self.timeout
        if timeout is not None:
            timeout = float(timeout)
        deadline = None if timeout is None else time.monotonic() + timeout

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR)
            self._file = os.fdopen(self._fd, "r+b", buffering=0)
            self._fd = None  # the file object now owns the descriptor
        except Exception:
            self._cleanup()
            raise

        fileno = self._file.fileno()

        if os.name == "nt":
            import errno
            import msvcrt

            while True:
                try:
                    msvcrt.locking(fileno, msvcrt.LK_NBLCK, 1)
                    return
                except (IOError, OSError) as exc:
                    if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) or isinstance(exc, PermissionError):
                        if deadline is not None:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                self._cleanup()
                                raise TimeoutError(
                                    f"could not acquire lock for {self.path} "
                                    f"within {timeout} seconds"
                                ) from exc
                            time.sleep(min(0.005, remaining))
                        else:
                            time.sleep(0.005)
                        continue
                    self._cleanup()
                    raise

        import errno
        import fcntl

        while True:
            try:
                fcntl.flock(fileno, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except (IOError, OSError) as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self._cleanup()
                            raise TimeoutError(
                                f"could not acquire lock for {self.path} "
                                f"within {timeout} seconds"
                            ) from exc
                        time.sleep(min(0.005, remaining))
                    else:
                        time.sleep(0.005)
                    continue
                self._cleanup()
                raise

    def release(self):
        if self._file is None:
            return
        try:
            fileno = self._file.fileno()
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(fileno, msvcrt.LK_UNLCK, 1)
                except (IOError, OSError):
                    pass
            else:
                import fcntl

                try:
                    fcntl.flock(fileno, fcntl.LOCK_UN)
                except (IOError, OSError):
                    pass
        finally:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None
            self._fd = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False



def load_grades(csv_path=DEFAULT_DATA_PATH):
    """Loads grades from CSV file and converts score and max_score to floats.

    ``csv_path`` may be a ``Path`` or a string.  The caller is responsible for
    holding the inter-process lock when mutation is expected; load reads the
    file directly for the duplicate-check transaction.
    """
    path = Path(csv_path)
    if not path.exists():
        return []

    grades = []
    with open(path, mode="r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row or not row.get("student_id"):
                continue
            try:
                grades.append(
                    {
                        "student_id": row["student_id"].strip(),
                        "subject": row["subject"].strip(),
                        "score": float(row["score"]),
                        "max_score": float(row["max_score"]),
                        "date": row.get("date", "").strip(),
                        "credits": float(row.get("credits", 3.0)),
                        "semester": str(row.get("semester", "1")).strip(),
                    }
                )
            except (ValueError, KeyError):
                continue
    return grades
def assessment_key(student_id, semester, subject, date):
    """Persistence-layer wrapper around the authoritative identity.

    The identity itself lives in grade_tracker.calc.assessment_key (single
    source of truth for both persistence and calculation). This wrapper keeps
    the flat (student_id, semester, subject, date) call shape used when
    saving a grade.
    """
    return calc_assessment_key(
        {
            "student_id": student_id,
            "semester": semester,
            "subject": subject,
            "date": date,
        }
    )


def _read_existing_rows(path):
    """Read the raw CSV rows (without the header) from ``path``.

    Returns ``(file_exists, rows)``.  When the file exists, its header is
    validated against the canonical schema; an unexpected schema raises
    ``ValueError`` and the file is left untouched (matching ``save_grade``'s
    documented refusal to rewrite a file it does not understand).

    Raw rows (not ``load_grades`` dicts) are returned so that unrelated
    records — including legacy rows the loader would skip — are preserved
    on rewrite instead of being normalized or dropped.
    """
    path = Path(path)
    if not path.exists():
        return False, []

    with open(path, mode="r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        # Preserve the expected header/column order.  An existing file
        # with an unexpected schema is left untouched rather than
        # silently rewritten into the canonical shape.
        expected_header = [
            "student_id", "subject", "score", "max_score", "date", "credits", "semester"
        ]
        if header is not None and header != expected_header:
            raise ValueError(
                "grades file has an unexpected schema; refusing to rewrite "
                f"{path} (expected header {expected_header!r}, got {header!r})"
            )
        if header is None:
            return True, []
        return True, list(reader)


def _csv_rows_bytes(rows):
    """Return the exact CSV bytes for ``rows`` (data records, no header).

    The encoding matches :func:`_atomic_write_rows` byte-for-byte, so a
    transaction can compute the post-commit digest of the dataset before it
    replaces the file.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(
        ["student_id", "subject", "score", "max_score", "date", "credits", "semester"]
    )
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def _atomic_write_bytes(path, data):
    """Replace ``path`` with ``data`` atomically.

    Writes to a temporary file in the same directory, flushes and ``fsync``\\ s
    it, then ``os.replace``\\ s it over the target.  On any failure before
    the replace succeeds the original file is left byte-for-byte unchanged
    and the temporary file is removed.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".grades-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _atomic_write_rows(path, rows):
    """Write ``rows`` (data records, no header) to ``path`` atomically.

    Writes to a temporary file in the same directory, flushes and ``fsync``\\ s
    it, then ``os.replace``\\ s it over the target.  On any failure before the
    replace succeeds the original file is left byte-for-byte unchanged and the
    temporary file is removed.  Callers must hold the inter-process lock.
    """
    _atomic_write_bytes(path, _csv_rows_bytes(rows))


def _audit_record(
    student_id, subject, score, max_score, date, credits, semester
):
    """Return the audit representation of one grade record.

    The audit log stores the academic identity and result of a record, not
    the raw CSV row, so history entries stay stable and readable.
    """
    return {
        "student_id": str(student_id),
        "semester": str(semester),
        "subject": str(subject),
        "date": str(date),
        "score": str(score),
        "max_score": str(max_score),
        "credits": str(credits),
    }


def _audit_record_from_row(row):
    """Return the audit representation of one raw CSV row."""
    header = [
        "student_id", "subject", "score", "max_score", "date", "credits", "semester"
    ]
    values = dict(zip(header, list(row) + [""] * len(header)))
    return _audit_record(
        values["student_id"], values["subject"], values["score"],
        values["max_score"], values["date"], values["credits"], values["semester"],
    )


def _append_row(path, row, operation=None, records=None, changes=None,
                reason=None, actor=None):
    """Append one raw CSV row to ``path`` using atomic persistence.

    Shared by ``save_grade`` and ``edit_grade``; both callers must already
    hold the inter-process lock for ``path``.  The file is created (with the
    canonical header) if it does not exist.

    When ``operation`` is given the write is performed through
    :class:`~grade_tracker.audit_txn.AuditTransaction`, so the grade change
    and its audit event are committed as one recoverable logical
    transaction.  The lock is already held by the caller, so the
    transaction does not acquire it a second time.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _, existing_rows = _read_existing_rows(path)
    new_rows = list(existing_rows) + [row]
    if operation is None:
        _atomic_write_rows(path, new_rows)
        return
    with AuditTransaction(
        path,
        operation=operation,
        records=records or [],
        changes=changes or {},
        reason=reason,
        actor=actor,
        manage_lock=False,
    ) as txn:
        txn.write_dataset(new_rows)


def save_grade(
    student_id,
    subject,
    score,
    max_score,
    date,
    credits,
    semester,
    csv_path=DEFAULT_DATA_PATH,
    allow_duplicate=False,
):
    """Appends a new grade row to the CSV file, unless the assessment exists.

    The read-check-write transaction is protected by a cross-process lock so
    that concurrent `save_grade` calls from separate OS processes cannot
    bypass duplicate detection or corrupt the file.

    Raises:
        ValueError: If the grade data is invalid, if the four-field assessment
            identity is already recorded and ``allow_duplicate`` is false, or
            if an exact row already exists.
            The file is left unchanged in either case.
    """
    validate_grade(score, max_score, date, credits)
    key = assessment_key(student_id, semester, subject, date)
    new_record_key = calc_assessment_record_key({
        "student_id": student_id,
        "semester": semester,
        "subject": subject,
        "score": score,
        "max_score": max_score,
        "date": date,
        "credits": credits,
    })

    lock_file = _lock_path_for(csv_path)
    with _InterProcessLock(lock_file) as _lock:
        # Read the latest persisted records while holding the lock.
        existing = load_grades(csv_path)
        # Duplicate detection is performed under the lock.  The identity is
        # derived from the authoritative calc.assessment_key so that the
        # persistence layer can never drift from the calculation layer.
        for grade in existing:
            existing_key = assessment_key(
                grade["student_id"],
                grade["semester"],
                grade["subject"],
                grade["date"],
            )
            if existing_key != key:
                continue
            if calc_assessment_record_key(grade) == new_record_key:
                raise ValueError(
                    f"assessment already exists with the same grade data for student '{student_id}' "
                    f"in {subject} (semester {semester}) on {date}"
                )
            if not allow_duplicate:
                raise ValueError(
                    f"assessment already exists for student '{student_id}' "
                    f"in {subject} (semester {semester}) on {date}; pass "
                    "--allow-duplicate only when this is a separate assessment"
                )

        # Build the updated CSV contents and write atomically.  ``_append_row``
        # performs the schema check and the temp-file/fsync/replace sequence
        # while this transaction still holds the lock.  The write goes
        # through ``AuditTransaction`` so the grade change and its audit
        # event are committed as one recoverable logical transaction.
        _append_row(
            csv_path,
            [student_id, subject, score, max_score, date, credits, semester],
            operation="add",
            records=[_audit_record(
                student_id, subject, score, max_score, date, credits, semester
            )],
            changes={"before": None, "after": _audit_record(
                student_id, subject, score, max_score, date, credits, semester
            )},
        )


def edit_grade(
    csv_path,
    student_id,
    semester,
    subject,
    date,
    score=None,
    max_score=None,
    credits=None,
    new_student_id=None,
    new_semester=None,
    new_subject=None,
    new_date=None,
    current_score=None,
    current_max_score=None,
    current_credits=None,
):
    """Edit one existing grade row in ``csv_path`` as a single transaction.

    The target record is identified by its current assessment identity
    ``(student_id, semester, subject, date)``.  When same-day assessments
    share that identity, the optional ``current_*`` values narrow the match
    using the record's values before editing. Any change option left as
    ``None`` keeps the record's existing value — omitted options are never
    replaced with defaults.

    The whole update runs as one locked transaction:

    1. Acquire the cross-process lock for the destination CSV.
    2. Read the latest persisted rows while holding the lock.
    3. Resolve the target uniquely by ``grade_tracker.calc.assessment_key``.
    4. Apply the requested changes in memory.
    5. Validate the complete updated record with ``validate_grade``.
    6. Reject the edit if the resulting identity duplicates another record
       (the target itself is excluded, so retakes are unaffected).
    7. Write the complete CSV to a temp file in the same directory, flush and
       ``fsync`` it, then ``os.replace`` it over the original.
    8. Release the lock reliably (context manager), including on exceptions.

    ``edit_grade`` and ``save_grade`` never call each other, and only these
    public entry points acquire the lock, so the transaction never
    re-acquires the lock it already holds.

    Raises:
        ValueError: If the grades file has an unexpected schema, no record
            matches the target, several records match (ambiguous), the update
            fails validation, or the updated identity would duplicate another
            record.  The file is left unchanged in every error case — any
            failure before the atomic replace leaves the original bytes intact
            and removes the temporary file.
    """
    path = Path(csv_path)
    changes = {
        "score": score,
        "max_score": max_score,
        "credits": credits,
        "student_id": new_student_id,
        "semester": new_semester,
        "subject": new_subject,
        "date": new_date,
    }
    if all(value is None for value in changes.values()):
        raise ValueError("no changes requested")

    target_key = assessment_key(student_id, semester, subject, date)

    header = [
        "student_id", "subject", "score", "max_score", "date", "credits", "semester"
    ]
    index_of = {name: position for position, name in enumerate(header)}

    def cell(row, name):
        position = index_of[name]
        if position >= len(row):
            return None
        return row[position]

    def row_key(row):
        return assessment_key(
            cell(row, "student_id") or "",
            cell(row, "semester") or "",
            cell(row, "subject") or "",
            cell(row, "date") or "",
        )

    def numeric_cell(row, name, default):
        value = cell(row, name)
        if value is None or value == "":
            value = default
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def row_record_key(row):
        return calc_assessment_record_key(
            {
                "student_id": cell(row, "student_id") or "",
                "semester": cell(row, "semester") or "1",
                "subject": cell(row, "subject") or "",
                "date": cell(row, "date") or "",
                "score": numeric_cell(row, "score", 0.0),
                "max_score": numeric_cell(row, "max_score", 0.0),
                "credits": numeric_cell(row, "credits", 3.0),
            }
        )

    lock_file = _lock_path_for(csv_path)
    with _InterProcessLock(lock_file) as _lock:
        # Read the latest persisted rows while holding the lock.
        _, rows = _read_existing_rows(path)

        # Resolve the target record by its authoritative assessment identity.
        # Raw rows are matched positionally so unrelated records — including
        # legacy rows load_grades would skip — stay untouched.
        matches = [
            position
            for position, row in enumerate(rows)
            if _row_has_visible_content(row) and row_key(row) == target_key
        ]

        selectors = (
            ("score", current_score, 0.0),
            ("max_score", current_max_score, 0.0),
            ("credits", current_credits, 3.0),
        )
        for name, expected, default in selectors:
            if expected is not None:
                matches = [
                    position for position in matches
                    if numeric_cell(rows[position], name, default) == expected
                ]

        if not matches:
            raise ValueError(
                f"no record found for student '{student_id}' (semester {semester}) "
                f"in {subject} on {date} matching the supplied current values"
            )
        if len(matches) > 1:
            raise ValueError(
                f"ambiguous target: {len(matches)} records match student "
                f"'{student_id}' (semester {semester}) in {subject} on {date}; "
                "use --current-score, --current-max-score, and/or "
                "--current-credits to select one assessment"
            )

        target_position = matches[0]
        target_row = list(rows[target_position])

        def current(name, default):
            value = cell(target_row, name)
            if value is None or value == "":
                return default
            return value

        # Apply the requested changes in memory; omitted fields keep their
        # existing raw values and are never silently defaulted.  Numeric
        # changes are stored as the caller-supplied floats, matching
        # save_grade's serialization of add-command values.
        updated = {
            "student_id": new_student_id if new_student_id is not None else current("student_id", ""),
            "semester": new_semester if new_semester is not None else current("semester", "1"),
            "subject": new_subject if new_subject is not None else current("subject", ""),
            "date": new_date if new_date is not None else current("date", ""),
            "score": score if score is not None else current("score", "0"),
            "max_score": max_score if max_score is not None else current("max_score", "0"),
            "credits": credits if credits is not None else current("credits", "3.0"),
        }

        # Validate the complete updated record with the existing rules.
        validate_grade(
            float(updated["score"]),
            float(updated["max_score"]),
            updated["date"],
            float(updated["credits"]),
        )

        # Same-day assessments may intentionally share the target's existing
        # four-field identity, so value-only edits reject only a complete
        # stored-record duplicate. Moving a record onto a different identity
        # remains conservative and requires that identity to be unused.
        new_identity = assessment_key(
            updated["student_id"], updated["semester"],
            updated["subject"], updated["date"]
        )
        new_key = calc_assessment_record_key(
            {
                **updated,
                "score": float(updated["score"]),
                "max_score": float(updated["max_score"]),
                "credits": float(updated["credits"]),
            }
        )
        for position, row in enumerate(rows):
            if position == target_position or not _row_has_visible_content(row):
                continue
            other_identity = row_key(row)
            is_duplicate = (
                other_identity == new_identity
                if new_identity != target_key
                else row_record_key(row) == new_key
            )
            if is_duplicate:
                raise ValueError(
                    "assessment already exists with the same grade data for student "
                    f"'{updated['student_id']}' in {updated['subject']} "
                    f"(semester {updated['semester']}) on {updated['date']}"
                )

        # Rewrite only the target row; every other raw row is preserved as-is
        # (header, column order, and legacy rows are untouched).
        rows[target_position] = [str(updated[name]) for name in header]

        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _atomic_write_rows(path, rows)
        except ValueError:
            # Schema refusal from a racing rewrite; already a user-facing error.
            raise
        except OSError as exc:
            # Any I/O failure before os.replace completes leaves the original
            # CSV untouched (the temp file is removed by _atomic_write_rows).
            raise ValueError(f"failed to write {path}: {exc}") from exc


def delete_grades(
    csv_path=DEFAULT_DATA_PATH,
    student_id=None,
    semester=None,
    subject=None,
    date=None,
    dry_run=False,
    confirmed=False,
):
    """Delete grade records matching selectors as a single locked transaction.

    At least one selector must be non-None. Selectors combine with AND logic:
    - student_id: exact match against stripped student ID
    - semester: exact match against stripped semester string
    - subject: case-insensitive match (matching subject_average and assessment_key)
    - date: exact match against YYYY-MM-DD date string (validated format)

    Raises:
        ValueError: If selectors are missing/invalid, CSV fails audit/schema,
            confirmation is missing for non-dry-run, or I/O fails.
    Returns:
        tuple[int, list[dict]]: (match_count, matching_records)
    """
    # 1. Validate selectors
    selectors = {
        "student_id": student_id,
        "semester": semester,
        "subject": subject,
        "date": date,
    }
    if all(v is None for v in selectors.values()):
        raise ValueError(
            "at least one selector is required; pass at least one of "
            "--student-id, --semester, --subject, --date"
        )

    norm_student_id = student_id.strip() if student_id is not None else None
    if norm_student_id == "":
        raise ValueError("student_id selector cannot be empty or blank")

    norm_semester = str(semester).strip() if semester is not None else None
    if norm_semester == "":
        raise ValueError("semester selector cannot be empty or blank")

    norm_subject = subject.strip() if subject is not None else None
    if norm_subject == "":
        raise ValueError("subject selector cannot be empty or blank")

    norm_date = date.strip() if date is not None else None
    if norm_date is not None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", norm_date):
            raise ValueError("date selector must use YYYY-MM-DD format")
        try:
            from datetime import date as _dt_date
            _dt_date.fromisoformat(norm_date)
        except ValueError as exc:
            raise ValueError(
                "date selector must be a valid calendar date in YYYY-MM-DD format"
            ) from exc

    if not dry_run and not confirmed:
        raise ValueError(
            "deletion requires confirmation; pass --yes to confirm deletion, "
            "or --dry-run to preview"
        )

    path = Path(csv_path)
    lock_file = _lock_path_for(path)

    with _InterProcessLock(lock_file):
        if not path.exists():
            return 0, []

        diagnostics = audit_grades_csv(path)
        if diagnostics:
            raise ValueError(
                f"grades file failed validation ({len(diagnostics)} problems found); "
                "repair it with 'validate' before deleting"
            )

        file_exists, raw_rows = _read_existing_rows(path)
        if not file_exists or not raw_rows:
            return 0, []

        header = [
            "student_id", "subject", "score", "max_score", "date", "credits", "semester"
        ]
        index_of = {name: pos for pos, name in enumerate(header)}

        def cell(row, name):
            pos = index_of[name]
            return row[pos] if pos < len(row) else None

        def matches_selectors(row):
            if not _row_has_visible_content(row):
                return False
            row_student = (cell(row, "student_id") or "").strip()
            row_semester = (cell(row, "semester") or "").strip()
            row_subject = (cell(row, "subject") or "").strip()
            row_date = (cell(row, "date") or "").strip()

            if norm_student_id is not None and row_student != norm_student_id:
                return False
            if norm_semester is not None and row_semester != norm_semester:
                return False
            if norm_subject is not None and row_subject.casefold() != norm_subject.casefold():
                return False
            if norm_date is not None and row_date != norm_date:
                return False
            return True

        matching_rows = []
        remaining_rows = []
        matching_dicts = []

        for row in raw_rows:
            if matches_selectors(row):
                matching_rows.append(row)
                matching_dicts.append({
                    "student_id": (cell(row, "student_id") or "").strip(),
                    "subject": (cell(row, "subject") or "").strip(),
                    "score": cell(row, "score"),
                    "max_score": cell(row, "max_score"),
                    "date": cell(row, "date"),
                    "credits": cell(row, "credits"),
                    "semester": cell(row, "semester"),
                })
            else:
                remaining_rows.append(row)

        match_count = len(matching_rows)

        if dry_run or match_count == 0:
            return match_count, matching_dicts

        # Destructive execution with matches
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _atomic_write_rows(path, remaining_rows)
        except ValueError:
            raise
        except OSError as exc:
            raise ValueError(f"failed to write {path}: {exc}") from exc

        return match_count, matching_dicts


def _row_has_visible_content(row):
    """True when a parsed CSV record contains at least one non-blank cell."""
    return any(cell.strip() for cell in row)


def _format_value_for_display(raw_value):
    """Render a raw cell in a diagnostic message."""
    text = str(raw_value)
    if not text.strip():
        return "<blank>"
    return repr(text)


def _audit_grades_csv_records(csv_path, collect_records=False):
    """Audit the grades CSV without modifying it, optionally collecting valid records.

    ``csv_path`` may be a ``Path`` or a string. The file is opened read-only
    and is never rewritten, normalised, repaired, or deleted.

    Returns:
        tuple[list[str], list[dict], dict]:
            - diagnostics: list of error strings
            - valid_records: list of dicts with canonical schema keys
              (only populated if collect_records=True and no row-level errors)
            - intra_duplicates: list of dicts with details of in-file duplicates
    """
    path = Path(csv_path)

    if not path.exists():
        return [f"{path}: file not found; nothing to audit"], [], []

    if not path.is_file():
        return [f"{path}: not a regular file"], [], []

    try:
        if path.stat().st_size == 0:
            return [f"{path}: file is empty (no CSV header or records)"], [], []
    except OSError as exc:
        return [f"{path}: cannot read file ({exc})"], [], []

    diagnostics = []
    valid_records = []
    intra_duplicates = []

    try:
        with open(path, mode="r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)

            try:
                header = next(reader, None)
            except UnicodeDecodeError as exc:
                return [f"{path}: file is not valid UTF-8 text ({exc})"], [], []

            if header is None:
                return [f"{path}: file is empty (no CSV header or records)"], [], []

            if not _row_has_visible_content(header):
                return [f"{path}: row 1: CSV header is blank; expected the grades schema"], [], []

            expected_columns = [
                "student_id", "subject", "score", "max_score", "date", "credits", "semester"
            ]
            optional_columns = {"credits", "semester"}
            required_columns = [
                name for name in expected_columns if name not in optional_columns
            ]
            duplicate_columns = sorted(
                {name for name in header if header.count(name) > 1}
            )
            if duplicate_columns:
                diagnostics.append(
                    "row 1: duplicate header column(s): " + ", ".join(duplicate_columns)
                )

            missing_columns = [name for name in required_columns if name not in header]
            if missing_columns:
                diagnostics.append(
                    "row 1: missing required column(s): " + ", ".join(missing_columns)
                )

            unexpected_columns = [name for name in header if name not in expected_columns]
            if unexpected_columns:
                diagnostics.append(
                    "row 1: unexpected column(s): "
                    + ", ".join(repr(name) for name in unexpected_columns)
                )

            index_of = {}
            for position, name in enumerate(header):
                index_of.setdefault(name, position)

            def cell(row, name):
                position = index_of.get(name)
                if position is None or position >= len(row):
                    return None
                return row[position]

            seen_assessments = {}
            row_number = 1

            for raw_row in reader:
                row_number += 1

                if not raw_row:
                    continue

                if not _row_has_visible_content(raw_row):
                    diagnostics.append(
                        f"row {row_number}: malformed record: blank line or empty record"
                    )
                    continue

                if len(raw_row) != len(header):
                    diagnostics.append(
                        f"row {row_number}: malformed record: expected "
                        f"{len(header)} fields, got {len(raw_row)}"
                    )
                    continue

                record = {}
                for name in expected_columns:
                    value = cell(raw_row, name)
                    if value is None:
                        record[name] = None
                    else:
                        record[name] = value.strip()

                if record["student_id"] is None or record["student_id"] == "":
                    diagnostics.append(
                        f"row {row_number}, field student_id: missing or blank student ID"
                    )

                if record["subject"] is None or record["subject"] == "":
                    diagnostics.append(
                        f"row {row_number}, field subject: missing or blank subject"
                    )

                if record["date"] is None or record["date"] == "":
                    diagnostics.append(
                        f"row {row_number}, field date: missing or blank date "
                        "(expected YYYY-MM-DD)"
                    )
                elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}", record["date"]):
                    diagnostics.append(
                        f"row {row_number}, field date: "
                        f"{_format_value_for_display(record['date'])} "
                        "must use YYYY-MM-DD format"
                    )
                else:
                    try:
                        date.fromisoformat(record["date"])
                    except ValueError:
                        diagnostics.append(
                            f"row {row_number}, field date: "
                            f"{_format_value_for_display(record['date'])} "
                            "is not a valid calendar date in YYYY-MM-DD format"
                        )

                if record["semester"] is None or record["semester"] == "":
                    if "semester" in index_of:
                        diagnostics.append(
                            f"row {row_number}, field semester: missing or blank semester"
                        )
                    else:
                        record["semester"] = "1"

                score = None
                if record["score"] is None or record["score"] == "":
                    diagnostics.append(
                        f"row {row_number}, field score: missing or blank score"
                    )
                else:
                    try:
                        score = float(record["score"])
                        if not math.isfinite(score):
                            raise ValueError
                    except ValueError:
                        diagnostics.append(
                            f"row {row_number}, field score: "
                            f"{_format_value_for_display(record['score'])} "
                            "is not a numeric score"
                        )
                        score = None

                max_score = None
                if record["max_score"] is None or record["max_score"] == "":
                    diagnostics.append(
                        f"row {row_number}, field max_score: missing or blank maximum score"
                    )
                else:
                    try:
                        max_score = float(record["max_score"])
                        if not math.isfinite(max_score):
                            raise ValueError
                    except ValueError:
                        diagnostics.append(
                            f"row {row_number}, field max_score: "
                            f"{_format_value_for_display(record['max_score'])} "
                            "is not a numeric maximum score"
                        )
                        max_score = None

                if max_score is not None and max_score <= 0:
                    diagnostics.append(
                        f"row {row_number}, field max_score: maximum score "
                        f"{_format_value_for_display(record['max_score'])} must be greater than 0"
                    )

                if (
                    score is not None
                    and max_score is not None
                    and max_score > 0
                    and not 0 <= score <= max_score
                ):
                    diagnostics.append(
                        f"row {row_number}, field score: score "
                        f"{_format_value_for_display(record['score'])} must be between 0 and "
                        f"max_score {_format_value_for_display(record['max_score'])}"
                    )

                if record["credits"] is not None and record["credits"] != "":
                    try:
                        credits_val = float(record["credits"])
                    except ValueError:
                        diagnostics.append(
                            f"row {row_number}, field credits: "
                            f"{_format_value_for_display(record['credits'])} "
                            "is not a numeric credit value"
                        )
                    else:
                        if not math.isfinite(credits_val) or credits_val < 0:
                            diagnostics.append(
                                f"row {row_number}, field credits: "
                                f"{_format_value_for_display(record['credits'])} "
                                "must be a finite number greater than or equal to 0"
                            )
                else:
                    record["credits"] = "3.0"
                    credits_val = 3.0

                row_has_error = any(
                    message.startswith(f"row {row_number} ")
                    or message.startswith(f"row {row_number},")
                    for message in diagnostics
                )
                if not row_has_error:
                    key = calc_assessment_record_key(
                        {
                            "student_id": record["student_id"],
                            "semester": record["semester"],
                            "subject": record["subject"],
                            "score": score,
                            "max_score": max_score,
                            "date": record["date"],
                            "credits": credits_val,
                        }
                    )
                    if key is not None:
                        first_row = seen_assessments.get(key)
                        if first_row is None:
                            seen_assessments[key] = row_number
                            if collect_records:
                                valid_records.append(
                                    {
                                        "row_number": row_number,
                                        "key": key,
                                        "record": record,
                                    }
                                )
                        else:
                            diag = (
                                f"row {row_number}: duplicate assessment of row {first_row} "
                                f"(student_id {record['student_id']!r}, "
                                f"semester {record['semester']!r}, "
                                f"subject {record['subject']!r}, "
                                f"date {record['date']!r})"
                            )
                            diagnostics.append(diag)
                            intra_duplicates.append(
                                {
                                    "row_number": row_number,
                                    "first_row": first_row,
                                    "key": key,
                                    "record": record,
                                    "diagnostic": diag,
                                }
                            )
    except UnicodeDecodeError as exc:
        return [f"{path}: file is not valid UTF-8 text ({exc})"], [], []
    except OSError as exc:
        return [f"{path}: cannot read file ({exc})"], [], []
    except csv.Error as exc:

        return [f"{path}: malformed CSV ({exc})"]
    except Exception as exc:
        return [f"{path}: unexpected CSV parsing error ({exc})"]

    except csv.Error as exc:
        return [f"{path}: malformed CSV ({exc})"]

        return [f"{path}: malformed CSV ({exc})"], [], []


    return diagnostics, valid_records, intra_duplicates


def audit_grades_csv(csv_path):
    """Audit the grades CSV without modifying it.

    ``csv_path`` may be a ``Path`` or a string. The file is opened read-only
    and is never rewritten, normalised, repaired, or deleted. Diagnostics use
    the CSV record numbers reported by :mod:`csv`: the header is row 1 and the
    first data record is row 2. Records that ``load_grades`` would silently
    skip (blank ``student_id``/subject/credits/semester, unparsable
    ``score``/``max_score``) are flagged here instead of being dropped, so a
    file that parses cleanly can still produce an audit report.

    ``credits`` and ``semester`` may legitimately be omitted by legacy rows:
    the loader falls back to ``3.0`` and ``"1"`` respectively, so only blank
    or non-numeric credit values (and blank semester values) are flagged here.
    ``date`` has no fallback and is always required.

    Returns:
        list[str]: one human-readable diagnostic per detected problem.
    """
    diagnostics, _, _ = _audit_grades_csv_records(csv_path, collect_records=False)
    return diagnostics


class ImportResult:
    """Outcome of a completed (committed) import transaction."""

    def __init__(self, imported, skipped, considered):
        self.imported = imported
        self.skipped = skipped
        self.considered = considered

    def __repr__(self):
        return (
            f"ImportResult(imported={self.imported}, "
            f"skipped={self.skipped}, considered={self.considered})"
        )


def _same_file(source_path, dest_path):
    """Check if source and destination paths resolve to the same filesystem object."""
    src = Path(source_path)
    dst = Path(dest_path)
    try:
        if src.exists() and dst.exists():
            return src.samefile(dst)
    except OSError:
        pass
    return src.resolve() == dst.resolve()


def import_grades_csv(source_path, dest_path=DEFAULT_DATA_PATH, on_conflict="error"):
    """Import valid grade records from source_path CSV into dest_path CSV.

    Parameters:
        source_path: Path to the source CSV to import records from.
        dest_path: Path to the destination grades CSV database.
        on_conflict: Conflict resolution policy ('error' or 'skip'). Default is 'error'.

    Returns:
        ImportResult with counts of imported, skipped, and considered records.

    Raises:
        ValueError: If arguments or records are invalid or conflicts occur under 'error'.
        FileNotFoundError: If the source CSV file does not exist.
        PermissionError, OSError: If filesystem operations fail.
    """
    if on_conflict not in ("error", "skip"):
        raise ValueError(
            f"invalid on_conflict policy {on_conflict!r}; must be 'error' or 'skip'"
        )

    source = Path(source_path)
    dest = Path(dest_path)

    if _same_file(source, dest):
        raise ValueError("source and destination CSV paths must not resolve to the same file")

    diagnostics, valid_records, intra_duplicates = _audit_grades_csv_records(
        source, collect_records=True
    )

    non_dup_errors = [
        d for d in diagnostics
        if not any(dup["diagnostic"] == d for dup in intra_duplicates)
    ]
    if non_dup_errors:
        err_msg = "source CSV failed validation:\n" + "\n".join(f"  {msg}" for msg in non_dup_errors)
        raise ValueError(err_msg)

    # Import retains the conservative four-field conflict policy. Unlike
    # ``add --allow-duplicate``, import has no per-record opt-in proving that
    # two matching identities are intentional rather than corrections.
    import_records = []
    import_duplicates = list(intra_duplicates)
    first_source_row_by_identity = {}
    for item in valid_records:
        rec = item["record"]
        identity = calc_assessment_key({
            "student_id": rec["student_id"],
            "semester": rec["semester"],
            "subject": rec["subject"],
            "date": rec["date"],
        })
        first_row = first_source_row_by_identity.get(identity)
        if first_row is None:
            first_source_row_by_identity[identity] = item["row_number"]
            item["import_key"] = identity
            import_records.append(item)
        else:
            import_duplicates.append({
                "row_number": item["row_number"],
                "first_row": first_row,
                "key": identity,
                "record": rec,
                "diagnostic": "",
            })

    if on_conflict == "error" and import_duplicates:
        dup = import_duplicates[0]
        rec = dup["record"]
        raise ValueError(
            f"source CSV contains duplicate assessment at row {dup['row_number']} "
            f"(duplicates row {dup['first_row']}): student_id={rec['student_id']!r}, "
            f"semester={rec['semester']!r}, subject={rec['subject']!r}, date={rec['date']!r}"
        )

    total_valid_considered = len(import_records) + len(import_duplicates)

    lock_file = _lock_path_for(dest)
    with _InterProcessLock(lock_file) as _lock:
        existing_grades = load_grades(dest)
        existing_keys = set()
        for grade in existing_grades:
            key = calc_assessment_key(grade)
            if key is not None:
                existing_keys.add(key)

        records_to_insert = []
        skipped_dest_conflicts = 0

        for item in import_records:
            key = item["import_key"]
            rec = item["record"]
            row_num = item["row_number"]

            if key in existing_keys:
                if on_conflict == "error":
                    raise ValueError(
                        f"assessment at row {row_num} already exists in destination: "
                        f"student_id={rec['student_id']!r}, semester={rec['semester']!r}, "
                        f"subject={rec['subject']!r}, date={rec['date']!r}"
                    )
                skipped_dest_conflicts += 1
            else:
                records_to_insert.append(rec)
                existing_keys.add(key)

        total_skipped = skipped_dest_conflicts + len(import_duplicates)
        total_imported = len(records_to_insert)

        dest.parent.mkdir(parents=True, exist_ok=True)
        existing_rows = []
        expected_header = [
            "student_id", "subject", "score", "max_score", "date", "credits", "semester"
        ]
        if dest.exists():
            with open(dest, mode="r", encoding="utf-8", newline="") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if header is not None and header != expected_header:
                    raise ValueError(
                        "grades file has an unexpected schema; refusing to rewrite "
                        f"{dest} (expected header {expected_header!r}, got {header!r})"
                    )
                if header is not None:
                    existing_rows = list(reader)

        fd, tmp_name = tempfile.mkstemp(
            dir=str(dest.parent), prefix=".grades-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(expected_header)
                for row in existing_rows:
                    writer.writerow(row)
                for rec in records_to_insert:
                    writer.writerow(
                        [
                            rec["student_id"],
                            rec["subject"],
                            rec["score"],
                            rec["max_score"],
                            rec["date"],
                            rec["credits"],
                            rec["semester"],
                        ]
                    )
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_name, dest)
            tmp_name = None
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    return ImportResult(
        imported=total_imported,
        skipped=total_skipped,
        considered=total_valid_considered,
    )


def cmd_validate(args):
    """Audit the grades CSV and report integrity problems, read-only."""
    diagnostics = audit_grades_csv(args.data)
    as_json = _format_name(args) == JSON_FORMAT

    if diagnostics:
        if as_json:
            _emit_json(
                {
                    "command": "validate",
                    "valid": False,
                    "error_count": len(diagnostics),
                    "errors": diagnostics,
                    "file": str(args.data),
                }
            )
            raise SystemExit(1)

        for message in diagnostics:
            print(f"Error: {message}", file=sys.stderr)
        count = len(diagnostics)
        noun = "problem" if count == 1 else "problems"
        print(f"Validation failed: {count} {noun} found.", file=sys.stderr)
        raise SystemExit(1)

    if as_json:
        _emit_json(
            {
                "command": "validate",
                "valid": True,
                "error_count": 0,
                "errors": [],
                "file": str(args.data),
            }
        )
        raise SystemExit(0)

    print(f"OK: {args.data} passed validation.")
    raise SystemExit(0)


def cmd_import(args):
    """Import grade records from a source CSV file."""
    try:
        result = import_grades_csv(
            source_path=args.source,
            dest_path=args.data,
            on_conflict=args.on_conflict,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except OSError as exc:
        print(f"Error: cannot read or write file: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(
        f"Import completed successfully: {result.imported} record(s) imported, "
        f"{result.skipped} record(s) skipped ({result.considered} valid record(s) considered)."
    )


def cmd_list(args):
    try:
        policy = _resolve_policy_argument(args)
    except UnknownPolicyError as exc:
        if _format_name(args) == JSON_FORMAT:
            _emit_json(_policy_error_payload("list", exc))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)

    if _format_name(args) == JSON_FORMAT:
        try:
            grades = _load_grades_for_json(args.data, "list")
        except ValueError as exc:
            payload = _json_input_error(exc)
            payload["command"] = "list"
            _emit_json(payload)
            raise SystemExit(2) from exc

        _emit_json(
            {
                "command": "list",
                "count": len(grades),
                "policy": resolve_policy(policy).name,
                "grades": [_grade_json_record(g, policy=policy) for g in grades],
            }
        )
        return

    grades = load_grades(args.data)
    if not grades:
        print("No grades found.")
        return

    print(f"{'Student ID':<12} {'Subject':<20} {'Score':<10} {'Max':<8} {'Grade':<6} {'Date':<12}")
    print("-" * 72)
    for g in grades:
        lg = letter_grade(g["score"], g["max_score"], policy=policy)
        print(f"{g['student_id']:<12} {g['subject']:<20} {g['score']:<10.1f} {g['max_score']:<8.1f} {lg:<6} {g['date']:<12}")


def cmd_summary(args):
    try:
        policy = _resolve_policy_argument(args)
    except UnknownPolicyError as exc:
        if _format_name(args) == JSON_FORMAT:
            _emit_json(
                _policy_error_payload(
                    "summary", exc, student_id=args.student_id
                )
            )
        else:
            print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)

    if _format_name(args) == JSON_FORMAT:
        try:
            grades = _load_grades_for_json(args.data, "summary")
        except ValueError as exc:
            payload = _json_input_error(exc)
            payload["command"] = "summary"
            payload["student_id"] = args.student_id
            _emit_json(payload)
            raise SystemExit(2) from exc

        student_grades = [
            g for g in grades if g["student_id"] == args.student_id
        ]
        if not student_grades:
            _emit_json(
                {
                    "command": "summary",
                    "student_id": args.student_id,
                    "count": 0,
                    "cgpa": None,
                    "semesters": [],
                    "grades": [],
                    "policy": resolve_policy(policy).name,
                }
            )
            return

        semesters = sorted({g["semester"] for g in student_grades})
        _emit_json(
            {
                "command": "summary",
                "student_id": args.student_id,
                "count": len(student_grades),
                "cgpa": cgpa(grades, args.student_id, policy=policy),
                "policy": resolve_policy(policy).name,
                "semesters": [
                    {
                        "semester": sem,
                        "sgpa": sgpa(grades, args.student_id, sem, policy=policy),
                        "grades": [
                            _grade_json_record(g, str(sem), policy=policy)
                            for g in student_grades
                            if g["semester"] == sem
                        ],
                    }
                    for sem in semesters
                ],
                "grades": [
                    _grade_json_record(g, policy=policy)
                    for g in student_grades
                ],
            }
        )
        return

    grades = load_grades(args.data)
    student_grades = [g for g in grades if g["student_id"] == args.student_id]
    if not student_grades:
        print(f"No records found for student '{args.student_id}'.")
        return

    student_cgpa = cgpa(grades, args.student_id, policy=policy)
    print(f"Summary for Student: {args.student_id}")
    print(f"Policy: {resolve_policy(policy).name}")
    print(f"CGPA: {student_cgpa}")
    print("-" * 50)

    semesters = sorted(list(set(g["semester"] for g in student_grades)))
    for sem in semesters:
        sem_grades = [g for g in student_grades if g["semester"] == sem]
        sem_sgpa = sgpa(grades, args.student_id, sem, policy=policy)
        print(f"Semester {sem} (SGPA: {sem_sgpa})")
        for g in sem_grades:
            lg = letter_grade(g["score"], g["max_score"], policy=policy)
            # Display-only rounding (1 decimal); the grade above is decided
            # from the unrounded percentage, never from this value.
            pct = float(percentage(g["score"], g["max_score"])) if g["max_score"] > 0 else 0
            print(f"  {g['subject']:<18}: {g['score']:.1f}/{g['max_score']:.1f} ({pct:.1f}%) -> {lg} (Credits: {g['credits']})")


def cmd_explain(args):
    """Explain GPA calculation breakdown for a student."""
    requested_sem = args.semester

    try:
        policy = _resolve_policy_argument(args)
    except UnknownPolicyError as exc:
        if _format_name(args) == JSON_FORMAT:
            _emit_json(
                _policy_error_payload(
                    "explain",
                    exc,
                    student_id=args.student_id,
                    semester=requested_sem,
                )
            )
        else:
            print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)

    if _format_name(args) == JSON_FORMAT:
        try:
            grades = _load_grades_for_json(args.data, "explain")
        except ValueError as exc:
            payload = _json_input_error(exc)
            payload["command"] = "explain"
            payload["student_id"] = args.student_id
            payload["semester"] = requested_sem
            _emit_json(payload)
            raise SystemExit(2) from exc

        explanation = explain_student_gpa(
            grades, args.student_id, semester=requested_sem, policy=policy
        )
        if not explanation["student_exists"]:
            _emit_json(
                {
                    "command": "explain",
                    "student_id": args.student_id,
                    "semester": requested_sem,
                    "student_exists": False,
                    "semester_found": False,
                    "policy": resolve_policy(policy).name,
                    "cgpa": {
                        "numerator": 0.0,
                        "denominator": 0.0,
                        "gpa": None,
                    },
                    "semesters": [],
                }
            )
            return

        if requested_sem is not None and not explanation["semester_found"]:
            _emit_json(
                {
                    "command": "explain",
                    "student_id": args.student_id,
                    "semester": requested_sem,
                    "student_exists": True,
                    "semester_found": False,
                    "policy": resolve_policy(policy).name,
                    "cgpa": {
                        "numerator": explanation["cgpa"]["total_points"],
                        "denominator": explanation["cgpa"]["total_credits"],
                        "gpa": explanation["cgpa"]["gpa"],
                    },
                    "semesters": [],
                }
            )
            return

        semesters_json = []
        for sem, sem_data in explanation["semesters"].items():
            sem_records_json = []
            for r in sem_data["records"]:
                sem_records_json.append(
                    {
                        "student_id": r["student_id"],
                        "subject": r["subject"],
                        "semester": r["semester"],
                        "date": r["date"] or None,
                        "score": r["score"],
                        "max_score": r["max_score"],
                        "credits": r["credits"],
                        "percentage": round(r["percentage"], 1),
                        "letter_grade": r["letter_grade"],
                        "grade_points": r["grade_points"],
                        "weighted_points": r["weighted_points"],
                        "included": r["included"],
                        "exclusion_reason": r["exclusion_reason"],
                    }
                )
            semesters_json.append(
                {
                    "semester": sem,
                    "sgpa": sem_data["gpa"],
                    "numerator": sem_data["total_points"],
                    "denominator": sem_data["total_credits"],
                    "records": sem_records_json,
                }
            )

        _emit_json(
            {
                "command": "explain",
                "student_id": args.student_id,
                "semester": requested_sem,
                "student_exists": True,
                "semester_found": explanation["semester_found"],
                "policy": resolve_policy(policy).name,
                "cgpa": {
                    "numerator": explanation["cgpa"]["total_points"],
                    "denominator": explanation["cgpa"]["total_credits"],
                    "gpa": explanation["cgpa"]["gpa"],
                },
                "semesters": semesters_json,
            }
        )
        return

    grades = load_grades(args.data)
    student_records = [g for g in grades if g.get("student_id") == args.student_id]
    if not student_records:
        print(f"Error: student '{args.student_id}' does not exist.", file=sys.stderr)
        raise SystemExit(1)

    explanation = explain_student_gpa(
        grades, args.student_id, semester=requested_sem, policy=policy
    )
    if requested_sem is not None and not explanation["semester_found"]:
        print(f"No records found for student '{args.student_id}' in semester {requested_sem}.")
        return

    print(f"Explanation for Student: {args.student_id}")
    print(f"Policy: {resolve_policy(policy).name}")
    if requested_sem is not None:
        print(f"Filtered Semester: {requested_sem}")
    print("=" * 60)

    # Per-assessment breakdown by semester
    for sem, sem_data in explanation["semesters"].items():
        print(f"Semester {sem}:")
        print(f"  {'Subject':<18} {'Score':<10} {'Max':<8} {'Pct':<8} {'Grade':<6} {'Pts':<6} {'Cr':<6} {'W.Pts':<8} {'Status'}")
        print("  " + "-" * 75)
        for r in sem_data["records"]:
            pct_str = f"{r['percentage']:.1f}%"
            status_str = "Included" if r["included"] else f"Excluded ({r['exclusion_reason']})"
            print(
                f"  {r['subject']:<18} {r['score']:<10.1f} {r['max_score']:<8.1f} {pct_str:<8} "
                f"{r['letter_grade']:<6} {r['grade_points']:<6.1f} {r['credits']:<6.1f} {r['weighted_points']:<8.1f} {status_str}"
            )

        print("  " + "-" * 75)
        num_str = f"{sem_data['total_points']:.2f}"
        den_str = f"{sem_data['total_credits']:.2f}"
        if sem_data["gpa"] is not None:
            gpa_str = f"{sem_data['gpa']:.2f}"
        else:
            gpa_str = "Unavailable (no valid records)"
        print(f"  Calculation: Numerator = {num_str}, Denominator = {den_str} -> SGPA = {gpa_str}")
        print()

    # Overall CGPA breakdown across all applicable records
    cgpa_data = explanation["cgpa"]
    print("Overall Cumulative (CGPA):")
    cgpa_num_str = f"{cgpa_data['total_points']:.2f}"
    cgpa_den_str = f"{cgpa_data['total_credits']:.2f}"
    if cgpa_data["gpa"] is not None:
        cgpa_str = f"{cgpa_data['gpa']:.2f}"
    else:
        cgpa_str = "Unavailable (no valid records)"
    print(f"  Calculation: Numerator = {cgpa_num_str}, Denominator = {cgpa_den_str} -> CGPA = {cgpa_str}")
    print("=" * 60)


def cmd_target_gpa(args):
    """Calculate required SGPA in upcoming credits to achieve a target CGPA."""
    target_cgpa = args.target
    upcoming_credits = args.credits

    try:
        policy = _resolve_policy_argument(args)
    except UnknownPolicyError as exc:
        if _format_name(args) == JSON_FORMAT:
            _emit_json(
                _policy_error_payload(
                    "target-gpa", exc, student_id=args.student_id
                )
            )
        else:
            print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)

    # Validate target and credits bounds
    if not (0.0 <= target_cgpa <= 10.0):
        if _format_name(args) == JSON_FORMAT:
            _emit_json(
                {
                    "command": "target-gpa",
                    "student_id": args.student_id,
                    "error": {
                        "code": "invalid_argument",
                        "message": "Target CGPA must be between 0.0 and 10.0.",
                    },
                }
            )
        else:
            print("Error: target CGPA must be between 0.0 and 10.0.", file=sys.stderr)
        raise SystemExit(1)

    if upcoming_credits <= 0.0:
        if _format_name(args) == JSON_FORMAT:
            _emit_json(
                {
                    "command": "target-gpa",
                    "student_id": args.student_id,
                    "error": {
                        "code": "invalid_argument",
                        "message": "Upcoming credits must be greater than 0.",
                    },
                }
            )
        else:
            print("Error: upcoming credits must be greater than 0.", file=sys.stderr)
        raise SystemExit(1)

    if _format_name(args) == JSON_FORMAT:
        try:
            grades = _load_grades_for_json(args.data, "target-gpa")
        except ValueError as exc:
            payload = _json_input_error(exc)
            payload["command"] = "target-gpa"
            payload["student_id"] = args.student_id
            _emit_json(payload)
            raise SystemExit(2) from exc

        student_records = [g for g in grades if g.get("student_id") == args.student_id]
        if not student_records:
            _emit_json(
                {
                    "command": "target-gpa",
                    "student_id": args.student_id,
                    "error": {
                        "code": "student_not_found",
                        "message": f"Student '{args.student_id}' does not exist.",
                    },
                }
            )
            raise SystemExit(1)

        res = calculate_target_sgpa(
            grades, args.student_id, target_cgpa, upcoming_credits, policy=policy
        )
        _emit_json(
            {
                "command": "target-gpa",
                "student_id": res["student_id"],
                "current_cgpa": res["current_cgpa"],
                "current_credits": res["current_credits"],
                "target_cgpa": res["target_cgpa"],
                "upcoming_credits": res["upcoming_credits"],
                "total_future_credits": res["total_future_credits"],
                "required_sgpa": res["required_sgpa"],
                "is_achievable": res["is_achievable"],
                "max_possible_cgpa": res["max_possible_cgpa"],
                "policy": resolve_policy(policy).name,
            }
        )
        return

    grades = load_grades(args.data)
    student_records = [g for g in grades if g.get("student_id") == args.student_id]
    if not student_records:
        print(f"Error: student '{args.student_id}' does not exist.", file=sys.stderr)
        raise SystemExit(1)

    res = calculate_target_sgpa(
        grades, args.student_id, target_cgpa, upcoming_credits, policy=policy
    )
    curr_cgpa_str = f"{res['current_cgpa']:.2f}" if res["current_cgpa"] is not None else "N/A"
    print(f"Student: {res['student_id']}")
    print(f"Policy: {resolve_policy(policy).name}")
    print(f"Current CGPA: {curr_cgpa_str} (over {res['current_credits']:.1f} credits)")
    print(f"Target CGPA:  {res['target_cgpa']:.2f}")
    print(f"Upcoming:     {res['upcoming_credits']:.1f} credits")
    print()
    if res["is_achievable"]:
        print("Result: Achievable")
        print(f"Required SGPA: {res['required_sgpa']:.2f}")
    else:
        print("Result: Unachievable in the upcoming semester")
        print(
            f"Maximum achievable CGPA: {res['max_possible_cgpa']:.2f} (with a perfect 10.00 SGPA)"
        )


def cmd_average(args):
    if _format_name(args) == JSON_FORMAT:
        try:
            grades = _load_grades_for_json(args.data, "average")
        except ValueError as exc:
            payload = _json_input_error(exc)
            payload["command"] = "average"
            payload["subject"] = args.subject
            _emit_json(payload)
            raise SystemExit(2) from exc

        avg = subject_average(grades, args.subject)
        _emit_json(
            {
                "command": "average",
                "subject": args.subject,
                "average": avg,
                "count": sum(
                    1
                    for g in grades
                    if g["subject"].strip().lower() == args.subject.strip().lower()
                ),
            }
        )
        return

    grades = load_grades(args.data)
    avg = subject_average(grades, args.subject)
    if avg is None:
        print(f"No records found for subject '{args.subject}'.")
    else:
        print(f"Average for '{args.subject}': {avg}")


def cmd_policies(args):
    """List the registered grading policies."""
    registry = get_default_registry()
    policies = registry.all()
    default_name = resolve_policy(None).name

    if _format_name(args) == JSON_FORMAT:
        _emit_json(
            {
                "command": "policies",
                "default": default_name,
                "policies": [policy.to_dict() for policy in policies],
            }
        )
        return

    print("Registered grading policies:")
    for policy in policies:
        marker = " (default)" if policy.name == default_name else ""
        print(f"  {policy.name}{marker}")
        if policy.description:
            print(f"      {policy.description}")
        thresholds = ", ".join(
            f"{letter}>={percentage:g}" for percentage, letter in policy.thresholds
        )
        print(f"      thresholds: {thresholds}; failing grade: {policy.failing_letter}")
        points = ", ".join(
            f"{letter}={value:g}" for letter, value in sorted(policy.grade_points.items())
        )
        print(f"      grade points: {points}")


def cmd_export(args):
    try:
        policy = _resolve_policy_argument(args)
    except UnknownPolicyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    grades = load_grades(args.data)
    output_file = build_html(grades, args.output, policy=policy)
    print(f"HTML report successfully exported to: {output_file}")


def cmd_add(args):
    try:
        save_grade(
            args.student_id, args.subject, args.score, args.max_score,
            args.date, args.credits, args.semester, args.data,
            allow_duplicate=args.allow_duplicate,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    print(f"Added grade for {args.student_id} in {args.subject}.")


def cmd_edit(args):
    """Edit one existing grade record as a single locked transaction."""
    changes = {
        "score": args.score,
        "max_score": args.max_score,
        "credits": args.credits,
        "student_id": args.new_student_id,
        "semester": args.new_semester,
        "subject": args.new_subject,
        "date": args.new_date,
    }
    if all(value is None for value in changes.values()):
        print(
            "Error: no changes requested; pass at least one of "
            "--score, --max-score, --credits, --student-id, --semester, "
            "--subject, --date",
            file=sys.stderr,
        )
        raise SystemExit(2)

    try:
        edit_grade(
            csv_path=args.data,
            student_id=args.student_id,
            semester=args.semester,
            subject=args.subject,
            date=args.date,
            score=args.score,
            max_score=args.max_score,
            credits=args.credits,
            new_student_id=args.new_student_id,
            new_semester=args.new_semester,
            new_subject=args.new_subject,
            new_date=args.new_date,
            current_score=args.current_score,
            current_max_score=args.current_max_score,
            current_credits=args.current_credits,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    print(
        f"Edited grade for {args.student_id} in {args.subject} "
        f"(semester {args.semester}) on {args.date}."
    )


def cmd_delete(args):
    """Delete grade records matching selectors."""
    try:
        count, records = delete_grades(
            csv_path=args.data,
            student_id=args.student_id,
            semester=args.semester,
            subject=args.subject,
            date=args.date,
            dry_run=args.dry_run,
            confirmed=args.yes,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    noun = "record" if count == 1 else "records"
    if args.dry_run:
        if count == 0:
            print("Dry run: 0 records match the criteria. No changes were made.")
        else:
            print(f"Dry run: {count} {noun} would be deleted (no changes were made):")
            for r in records:
                score_str = f"{float(r['score']):.1f}/{float(r['max_score']):.1f}"
                date_str = r['date'] or "-"
                print(
                    f"  {r['student_id']:<10} {r['subject']:<18} "
                    f"Sem: {r['semester']:<4} Date: {date_str:<12} Score: {score_str}"
                )
    else:
        if count == 0:
            print("0 records matched the criteria. No changes were made.")
        else:
            print(f"Successfully deleted {count} {noun}.")



def _add_policy_argument(parser):
    """Add the shared ``--policy`` option to a command parser.

    The value is a policy name: either a bare identifier (which resolves to
    the highest registered version) or ``identifier@version`` for an exact
    version.  Omitting the option uses the default legacy policy.
    """
    parser.add_argument(
        "--policy",
        default=None,
        help=(
            "Grading policy to apply, as 'identifier' or 'identifier@version' "
            "(default: legacy@1)"
        ),
    )


def _resolve_policy_argument(args):
    """Resolve the ``--policy`` option of ``args`` into a Policy.

    Returns ``None`` when no policy was requested, so callers can keep using
    the default policy without an extra lookup.
    """
    requested = getattr(args, "policy", None)
    if not requested:
        return None
    return resolve_policy(requested)


def _policy_error_payload(command, exc, **extra):
    """Build a JSON-ready payload describing a policy selection error."""
    payload = {
        "command": command,
        "error": {
            "code": "unknown_policy",
            "message": str(exc),
        },
    }
    payload.update(extra)
    return payload


def _add_format_argument(parser):
    """Add the shared ``--format`` option to a read-only command parser."""
    parser.add_argument(
        "--format",
        choices=(HUMAN_FORMAT, JSON_FORMAT),
        default=HUMAN_FORMAT,
        dest="format",
        help="Output format: human (default) or json",
    )

def compute_sha256(path):
    """Compute hex SHA-256 digest of file at path."""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def create_backup(csv_path=DEFAULT_DATA_PATH, backup_dir=None):
    """Create a verified, timestamped backup of csv_path with metadata manifest.

    Coordinated under the inter-process lock of the source CSV.
    The source CSV must exist and pass integrity audit (audit_grades_csv).
    Returns a dict with paths and metadata:
        {
            "backup_csv": Path,
            "manifest": Path,
            "metadata": dict,
        }
    """
    source = Path(csv_path).resolve()
    lock_file = _lock_path_for(source)

    with _InterProcessLock(lock_file):
        if not source.exists():
            raise FileNotFoundError(f"source data file does not exist: {source}")
        if not source.is_file():
            raise ValueError(f"source path is not a regular file: {source}")

        # Check source validity before creating backup
        diagnostics = audit_grades_csv(source)
        if diagnostics:
            raise ValueError(
                f"cannot backup invalid source CSV ({len(diagnostics)} validation problems found)"
            )

        if backup_dir is None:
            dest_dir = source.parent / "backups"
        else:
            dest_dir = Path(backup_dir).resolve()

        dest_dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc)
        base_timestamp = now.strftime("%Y%m%dT%H%M%SZ")

        # Guarantee unique name even if created rapidly
        counter = 0
        while True:
            suffix = f"-{counter}" if counter > 0 else ""
            candidate_base = f"grades-{base_timestamp}{suffix}"
            candidate_csv = dest_dir / f"{candidate_base}.csv"
            candidate_json = dest_dir / f"{candidate_base}.json"
            if not candidate_csv.exists() and not candidate_json.exists():
                backup_csv_path = candidate_csv
                manifest_path = candidate_json
                break
            counter += 1

        # Read source bytes and compute checksum
        with open(source, "rb") as f:
            data_bytes = f.read()

        data_checksum = hashlib.sha256(data_bytes).hexdigest()
        byte_size = len(data_bytes)

        # Count records and extract header
        with open(source, mode="r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            header = next(reader, [])
            record_count = sum(1 for row in reader if _row_has_visible_content(row))

        metadata = {
            "format_version": BACKUP_FORMAT_VERSION,
            "schema_version": SUPPORTED_CSV_SCHEMA_VERSION,
            "created_at": now.isoformat(),
            "source_file": source.name,
            "sha256": data_checksum,
            "bytes": byte_size,
            "records": record_count,
            "header": header,
        }

        # Write to temporary files in dest_dir then atomically replace
        tmp_csv = None
        tmp_json = None
        try:
            fd_csv, tmp_csv = tempfile.mkstemp(
                dir=str(dest_dir), prefix=".backup-csv-", suffix=".tmp"
            )
            with os.fdopen(fd_csv, "wb") as f:
                f.write(data_bytes)
                f.flush()
                os.fsync(f.fileno())

            fd_json, tmp_json = tempfile.mkstemp(
                dir=str(dest_dir), prefix=".backup-meta-", suffix=".tmp"
            )
            with os.fdopen(fd_json, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2)
                f.flush()
                os.fsync(f.fileno())

            # Atomically place both files (CSV first, then manifest)
            os.replace(tmp_csv, backup_csv_path)
            tmp_csv = None
            os.replace(tmp_json, manifest_path)
            tmp_json = None

        finally:
            if tmp_csv is not None:
                try:
                    os.unlink(tmp_csv)
                except OSError:
                    pass
            if tmp_json is not None:
                try:
                    os.unlink(tmp_json)
                except OSError:
                    pass

        return {
            "backup_csv": backup_csv_path,
            "manifest": manifest_path,
            "metadata": metadata,
        }


def restore_backup(backup_path, dest_path=DEFAULT_DATA_PATH):
    """Restore a verified backup to dest_path.

    backup_path may point to either the backup CSV or its companion .json manifest.
    Coordinated under the inter-process lock of dest_path.
    Destination is preserved byte-for-byte if any check or step fails.
    """
    b_path = Path(backup_path).resolve()
    if not b_path.exists():
        raise FileNotFoundError(f"backup path does not exist: {b_path}")

    if b_path.suffix.lower() == ".json":
        manifest_path = b_path
        backup_csv_path = b_path.with_suffix(".csv")
    elif b_path.suffix.lower() == ".csv":
        backup_csv_path = b_path
        manifest_path = b_path.with_suffix(".json")
    else:
        raise ValueError(
            f"unsupported backup file extension: {b_path.suffix} (expected .csv or .json)"
        )

    if not backup_csv_path.exists():
        raise FileNotFoundError(f"backup data file not found: {backup_csv_path}")
    if not backup_csv_path.is_file():
        raise ValueError(f"backup data path is not a regular file: {backup_csv_path}")

    if not manifest_path.exists():
        raise FileNotFoundError(f"backup manifest file not found: {manifest_path}")
    if not manifest_path.is_file():
        raise ValueError(f"backup manifest path is not a regular file: {manifest_path}")

    dest = Path(dest_path).resolve()

    # Prevent restoring over the backup itself or its manifest
    if dest == backup_csv_path or dest == manifest_path:
        raise ValueError("destination cannot be the backup file or its manifest")

    # Read and validate manifest
    try:
        with open(manifest_path, mode="r", encoding="utf-8") as f:
            manifest_data = json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed backup manifest: {exc}") from exc
    except Exception as exc:
        raise ValueError(f"cannot read backup manifest: {exc}") from exc

    if not isinstance(manifest_data, dict):
        raise ValueError("malformed backup manifest: root must be a JSON object")

    format_version = manifest_data.get("format_version")
    if format_version != BACKUP_FORMAT_VERSION:
        raise ValueError(
            f"unsupported backup format version: {format_version!r} "
            f"(supported: {BACKUP_FORMAT_VERSION!r})"
        )

    schema_version = manifest_data.get("schema_version")
    if schema_version != SUPPORTED_CSV_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported backup schema version: {schema_version!r} "
            f"(supported: {SUPPORTED_CSV_SCHEMA_VERSION!r})"
        )

    expected_sha256 = manifest_data.get("sha256")
    if not expected_sha256 or not isinstance(expected_sha256, str):
        raise ValueError("backup manifest missing valid sha256 checksum")

    # Verify SHA-256 checksum of the backup data file
    actual_sha256 = compute_sha256(backup_csv_path)
    if actual_sha256.lower() != expected_sha256.lower():
        raise ValueError(
            f"checksum mismatch: expected {expected_sha256}, got {actual_sha256}"
        )

    # Validate header in manifest if present
    manifest_header = manifest_data.get("header")
    if manifest_header is not None:
        if not isinstance(manifest_header, list):
            raise ValueError("malformed backup manifest: header must be a list")
        # Ensure required canonical columns are present
        required_cols = ["student_id", "subject", "score", "max_score", "date"]
        missing_req = [c for c in required_cols if c not in manifest_header]
        if missing_req:
            raise ValueError(
                f"backup manifest header missing required column(s): {', '.join(missing_req)}"
            )

    # Validate CSV content and records using audit_grades_csv
    diagnostics = audit_grades_csv(backup_csv_path)
    if diagnostics:
        detail = diagnostics[0] if len(diagnostics) == 1 else f"{len(diagnostics)} problems found (first: {diagnostics[0]})"
        raise ValueError(f"backup CSV validation failed: {detail}")

    # Now acquire the lock for destination and re-verify everything before replacing
    dest_lock = _lock_path_for(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    with _InterProcessLock(dest_lock):
        # Re-check collision with backup paths while holding lock
        if dest.resolve() == backup_csv_path.resolve() or dest.resolve() == manifest_path.resolve():
            raise ValueError("destination cannot be the backup file or its manifest")

        # Read backup data bytes
        with open(backup_csv_path, "rb") as f:
            restored_bytes = f.read()

        # Re-verify checksum under lock
        if hashlib.sha256(restored_bytes).hexdigest().lower() != expected_sha256.lower():
            raise ValueError("backup file was modified during restore")

        # Write to temporary file in destination directory
        tmp_name = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(dest.parent), prefix=".grades-restore-", suffix=".tmp"
            )
            with os.fdopen(fd, "wb") as f:
                f.write(restored_bytes)
                f.flush()
                os.fsync(f.fileno())

            # Atomically replace destination
            os.replace(tmp_name, dest)
            tmp_name = None
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    return {
        "restored_path": dest,
        "backup_csv": backup_csv_path,
        "manifest": manifest_path,
        "records": manifest_data.get("records"),
        "sha256": actual_sha256,
    }


def cmd_backup(args):
    """Create a verified, timestamped backup of the current grade data."""
    try:
        result = create_backup(csv_path=args.data)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    meta = result["metadata"]
    print(
        f"Backup created successfully:\n"
        f"  Data: {result['backup_csv']}\n"
        f"  Manifest: {result['manifest']}\n"
        f"  Records: {meta['records']}\n"
        f"  SHA-256: {meta['sha256']}\n"
        f"  Status: verified"
    )
    raise SystemExit(0)


def cmd_restore(args):
    """Restore a verified backup to the destination grade data."""
    try:
        result = restore_backup(backup_path=args.backup_path, dest_path=args.data)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(
        f"Restore completed successfully:\n"
        f"  Source: {result['backup_csv']}\n"
        f"  Destination: {result['restored_path']}\n"
        f"  SHA-256: {result['sha256']}\n"
        f"  Status: verified"
    )
    raise SystemExit(0)


def cmd_rollback(args):
    """Roll back a previous grade mutation identified by its audit event ID."""
    try:
        result = rollback_mutation(
            csv_path=args.data,
            event_id=args.event_id,
            dry_run=args.dry_run,
            confirmed=args.yes,
            reason=getattr(args, "reason", None),
        )
    except (RollbackError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    count = result["records_affected"]
    noun = "record" if count == 1 else "records"
    op = result["operation"]
    event_id = result["event_id"]
    records = result.get("records", [])

    if result.get("dry_run"):
        print(f"Dry run: rollback of {op} mutation (event {event_id}) would affect {count} {noun}:")
        for r in records:
            score_str = f"{float(r.get('score', 0)):.1f}/{float(r.get('max_score', 0)):.1f}"
            date_str = r.get("date") or "-"
            print(
                f"  {r.get('student_id', ''):<10} {r.get('subject', ''):<18} "
                f"Sem: {r.get('semester', '1'):<4} Date: {date_str:<12} Score: {score_str}"
            )
        print("No changes were made.")
    else:
        print(f"Successfully rolled back {op} mutation (event {event_id}): {count} {noun} affected.")


def cmd_snapshot(args):
    """Handle snapshot operations: create, inspect, reproduce, and list."""
    action = getattr(args, "snapshot_action", None)
    if not action:
        print("Error: snapshot subcommand required (create, inspect, reproduce, list)", file=sys.stderr)
        raise SystemExit(2)

    as_json = _format_name(args) == JSON_FORMAT

    if action == "create":
        try:
            policy = _resolve_policy_argument(args)
        except UnknownPolicyError as exc:
            if as_json:
                _emit_json(_policy_error_payload("snapshot create", exc))
            else:
                print(f"Error: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc

        try:
            snap = create_snapshot(
                csv_path=args.data,
                calculation_type=args.type,
                student_id=args.student_id,
                semester=args.semester,
                policy=policy,
                snapshot_id=args.snapshot_id,
                description=args.description,
            )
        except (SnapshotError, ValueError) as exc:
            if as_json:
                _emit_json({
                    "command": "snapshot create",
                    "error": {"code": type(exc).__name__, "message": str(exc)},
                })
            else:
                print(f"Error: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc

        if as_json:
            _emit_json({"command": "snapshot create", "snapshot": snap})
            return

        res = snap["result"]
        gpa_str = f"{res['gpa']:.2f}" if res.get("gpa") is not None else "N/A"
        print(f"Snapshot created successfully: {snap['snapshot_id']}")
        print(f"  Type:        {snap['calculation_type'].upper()}")
        print(f"  Student:     {snap['student_id']}")
        if snap.get("semester"):
            print(f"  Semester:    {snap['semester']}")
        print(f"  GPA:         {gpa_str}")
        print(f"  Policy:      {snap['policy']['name']}")
        print(f"  Records:     {len(snap['input_records'])}")
        print(f"  Digest:      {snap['content_digest']}")

    elif action == "inspect":
        try:
            info = inspect_snapshot(csv_path=args.data, snapshot_id=args.snapshot_id)
        except (SnapshotError, ValueError) as exc:
            if as_json:
                _emit_json({
                    "command": "snapshot inspect",
                    "error": {"code": type(exc).__name__, "message": str(exc)},
                })
            else:
                print(f"Error: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc

        if as_json:
            _emit_json({"command": "snapshot inspect", "snapshot": info})
            return

        gpa_str = f"{info['recorded_gpa']:.2f}" if info.get("recorded_gpa") is not None else "N/A"
        digest_status = "valid" if info.get("digest_valid") else "INVALID (corrupted or tampered)"
        print(f"Snapshot: {info['snapshot_id']}")
        print(f"  Schema:      {info['schema_version']} (supported: {info['schema_supported']})")
        print(f"  Algorithm:   {info['algorithm_version']} (supported: {info['algorithm_supported']})")
        print(f"  Created:     {info['created_at']}")
        print(f"  Type:        {str(info['calculation_type']).upper()}")
        print(f"  Student:     {info['student_id']}")
        if info.get("semester"):
            print(f"  Semester:    {info['semester']}")
        print(f"  Policy:      {info['policy_name']}")
        print(f"  Records:     {info['input_record_count']}")
        print(f"  GPA:         {gpa_str}")
        print(f"  Points:      {info['recorded_total_points']}")
        print(f"  Credits:     {info['recorded_total_credits']}")
        print(f"  Digest:      {info['content_digest']} ({digest_status})")
        if info.get("description"):
            print(f"  Description: {info['description']}")

    elif action == "reproduce":
        try:
            rep = reproduce_snapshot(csv_path=args.data, snapshot_id=args.snapshot_id)
        except (SnapshotError, ValueError) as exc:
            if as_json:
                _emit_json({
                    "command": "snapshot reproduce",
                    "error": {"code": type(exc).__name__, "message": str(exc)},
                })
            else:
                print(f"Error: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc

        if as_json:
            _emit_json({"command": "snapshot reproduce", "reproduction": rep})
            return

        status_str = "MATCH (VERIFIED)" if rep["reproduced"] else "MISMATCH"
        rec_gpa = f"{rep['recorded_gpa']:.2f}" if rep.get("recorded_gpa") is not None else "N/A"
        rep_gpa = f"{rep['reproduced_gpa']:.2f}" if rep.get("reproduced_gpa") is not None else "N/A"

        print(f"Reproduced snapshot: {rep['snapshot_id']}")
        print(f"  Status:          {status_str}")
        print(f"  Recorded GPA:    {rec_gpa}")
        print(f"  Reproduced GPA:  {rep_gpa}")
        print(f"  Policy:          {rep['policy_name']}")
        print(f"  Algorithm:       {rep['algorithm_version']}")
        print(f"  Records Used:    {rep['records_count']}")
        if not rep["reproduced"]:
            print("  Warning: reproduced GPA does not match recorded snapshot result!", file=sys.stderr)
            raise SystemExit(1)

    elif action == "list":
        try:
            items = list_snapshots(csv_path=args.data)
        except (SnapshotError, ValueError) as exc:
            if as_json:
                _emit_json({
                    "command": "snapshot list",
                    "error": {"code": type(exc).__name__, "message": str(exc)},
                })
            else:
                print(f"Error: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc

        if as_json:
            _emit_json({"command": "snapshot list", "count": len(items), "snapshots": items})
            return

        if not items:
            print("No snapshots found.")
            return

        print(f"Found {len(items)} snapshot(s):")
        for item in items:
            gpa_val = f"{item['recorded_gpa']:.2f}" if item.get("recorded_gpa") is not None else "N/A"
            status = "OK" if item.get("digest_valid") else "CORRUPTED"
            sem = f" (sem {item['semester']})" if item.get("semester") else ""
            print(
                f"  {item['snapshot_id']:<36} {str(item['calculation_type']).upper():<5} "
                f"Student: {item['student_id']:<10}{sem:<10} GPA: {gpa_val:<6} [{status}]"
            )



def main():
    parser = argparse.ArgumentParser(description="Grade Tracker CLI")
    parser.add_argument("--data", default=DEFAULT_DATA_PATH, help="Path to grades.csv")
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # list
    p_list = subparsers.add_parser("list", help="List all grades")
    _add_format_argument(p_list)
    _add_policy_argument(p_list)
    p_list.set_defaults(func=cmd_list)

    # summary
    p_summary = subparsers.add_parser("summary", help="Show summary for a student")
    p_summary.add_argument("student_id", help="Student ID")
    _add_format_argument(p_summary)
    _add_policy_argument(p_summary)
    p_summary.set_defaults(func=cmd_summary)

    # explain
    p_explain = subparsers.add_parser("explain", help="Explain GPA calculation breakdown for a student")
    p_explain.add_argument("student_id", help="Student ID")
    p_explain.add_argument("--semester", default=None, help="Filter by semester")
    _add_format_argument(p_explain)
    _add_policy_argument(p_explain)
    p_explain.set_defaults(func=cmd_explain)

    # target-gpa
    p_target = subparsers.add_parser("target-gpa", help="Calculate required SGPA for a target CGPA")
    p_target.add_argument("student_id", help="Student ID")
    p_target.add_argument("--target", type=float, required=True, help="Target CGPA (0.0 to 10.0)")
    p_target.add_argument("--credits", type=float, required=True, help="Upcoming semester credits (> 0)")
    _add_format_argument(p_target)
    p_target.add_argument(
        "--json",
        action="store_const",
        const=JSON_FORMAT,
        dest="format",
        help="Output JSON (shorthand for --format json)",
    )
    _add_policy_argument(p_target)
    p_target.set_defaults(func=cmd_target_gpa)

    # policies
    p_policies = subparsers.add_parser(
        "policies", help="List the registered grading policies"
    )
    _add_format_argument(p_policies)
    p_policies.set_defaults(func=cmd_policies)

    # average
    p_avg = subparsers.add_parser("average", help="Show average for a subject")
    p_avg.add_argument("subject", help="Subject name")
    _add_format_argument(p_avg)
    p_avg.set_defaults(func=cmd_average)

    # export
    p_export = subparsers.add_parser("export", help="Export HTML report")
    p_export.add_argument("--output", default="reports/index.html", help="Output file path")
    _add_policy_argument(p_export)
    p_export.set_defaults(func=cmd_export)

    # add
    p_add = subparsers.add_parser("add", help="Add a new grade entry")
    p_add.add_argument("student_id", help="Student ID")
    p_add.add_argument("subject", help="Subject name")
    p_add.add_argument("score", type=float, help="Score earned")
    p_add.add_argument("max_score", type=float, help="Maximum possible score")
    p_add.add_argument("date", help="Date (YYYY-MM-DD)")
    p_add.add_argument("credits", type=float, help="Credits for the subject")
    p_add.add_argument("semester", help="Semester number")
    p_add.add_argument(
        "--allow-duplicate",
        action="store_true",
        help=(
            "Allow a distinct assessment with the same student, semester, "
            "subject, and date; exact repeated rows are still rejected"
        ),
    )
    p_add.set_defaults(func=cmd_add)


    # import
    p_import = subparsers.add_parser(
        "import", help="Import grade records from a source CSV file"
    )
    p_import.add_argument(
        "source", help="Path to the source CSV file to import"
    )
    p_import.add_argument(
        "--on-conflict",
        choices=("error", "skip"),
        default="error",
        help="Conflict resolution policy for duplicate assessments ('error' or 'skip', default: 'error')",
    )
    p_import.set_defaults(func=cmd_import)

    # edit
    p_edit = subparsers.add_parser(
        "edit",
        help="Edit an existing grade record",
        description=(
            "Edit an existing grade record. The target record is identified "
            "by its current assessment identity: STUDENT_ID, SEMESTER, "
            "SUBJECT, and DATE. Use the --current-* selectors when multiple "
            "same-day assessments share that identity. Omitted change options keep the record's "
            "current value; at least one change option is required. The edit "
            "is applied as a single transaction under the same cross-process "
            "lock and atomic write used by 'add'. If the target does not "
            "exist, or several records match it, the command fails and the "
            "file is left unchanged."
        ),
    )
    p_edit.add_argument("student_id", help="Student ID of the record to edit")
    p_edit.add_argument("semester", help="Semester of the record to edit")
    p_edit.add_argument("subject", help="Subject of the record to edit")
    p_edit.add_argument("date", help="Date of the record to edit (YYYY-MM-DD)")
    p_edit.add_argument(
        "--current-score", type=float, default=None,
        help="Current score used to select one matching same-day assessment",
    )
    p_edit.add_argument(
        "--current-max-score", type=float, default=None,
        help="Current maximum score used to select one matching same-day assessment",
    )
    p_edit.add_argument(
        "--current-credits", type=float, default=None,
        help="Current credits used to select one matching same-day assessment",
    )
    p_edit.add_argument(
        "--score", type=float, default=None,
        help="New score (omit to keep the current value)",
    )
    p_edit.add_argument(
        "--max-score", dest="max_score", type=float, default=None,
        help="New maximum score (omit to keep the current value)",
    )
    p_edit.add_argument(
        "--credits", type=float, default=None,
        help="New credits (omit to keep the current value)",
    )
    p_edit.add_argument(
        "--student-id", dest="new_student_id", default=None,
        help="New student ID (omit to keep the current value)",
    )
    p_edit.add_argument(
        "--semester", dest="new_semester", default=None,
        help="New semester (omit to keep the current value)",
    )
    p_edit.add_argument(
        "--subject", dest="new_subject", default=None,
        help="New subject (omit to keep the current value)",
    )
    p_edit.add_argument(
        "--date", dest="new_date", default=None,
        help="New date, YYYY-MM-DD (omit to keep the current value)",
    )
    p_edit.set_defaults(func=cmd_edit)

    # delete
    p_delete = subparsers.add_parser(
        "delete",
        help="Delete grade records matching selectors",
        description=(
            "Delete grade records matching selectors as a single transaction under "
            "an inter-process lock. At least one selector (--student-id, --semester, "
            "--subject, --date) is required. Selectors combine with AND logic. "
            "Use --dry-run to preview matching records without modifying the file. "
            "Destructive deletion requires explicit confirmation via --yes."
        ),
    )
    p_delete.add_argument(
        "--student-id", dest="student_id", default=None,
        help="Filter records to delete by student ID",
    )
    p_delete.add_argument(
        "--semester", dest="semester", default=None,
        help="Filter records to delete by semester",
    )
    p_delete.add_argument(
        "--subject", dest="subject", default=None,
        help="Filter records to delete by subject (case-insensitive)",
    )
    p_delete.add_argument(
        "--date", dest="date", default=None,
        help="Filter records to delete by date (YYYY-MM-DD)",
    )
    p_delete.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Preview records that would be deleted without modifying the file",
    )
    p_delete.add_argument(
        "--yes", "-y", action="store_true", default=False,
        help="Confirm deletion of matching records",
    )
    p_delete.set_defaults(func=cmd_delete)


    # validate
    p_validate = subparsers.add_parser(
        "validate", help="Audit the grades CSV for integrity problems (read-only)"
    )
    _add_format_argument(p_validate)
    p_validate.set_defaults(func=cmd_validate)

    # backup
    p_backup = subparsers.add_parser(
        "backup", help="Create a verified, timestamped backup of the grade data"
    )
    p_backup.set_defaults(func=cmd_backup)

    # restore
    p_restore = subparsers.add_parser(
        "restore", help="Restore a verified backup to the grade data file"
    )
    p_restore.add_argument("backup_path", help="Path to backup CSV or JSON manifest")
    p_restore.set_defaults(func=cmd_restore)

    # rollback
    p_rollback = subparsers.add_parser(
        "rollback",
        help="Safely roll back a previous grade mutation using audit history",
        description=(
            "Safely roll back a previous mutation (add, edit, delete, or import) "
            "identified by its audit EVENT_ID. Operates under an inter-process lock, "
            "validates the audit chain, checks for conflicts against current records, "
            "and writes a compensating audit transaction. Use --dry-run to preview "
            "the rollback without making changes; pass --yes to confirm execution."
        ),
    )
    p_rollback.add_argument("event_id", help="Audit event ID (UUID) of the mutation to reverse")
    p_rollback.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Preview the rollback operation without modifying data or audit log",
    )
    p_rollback.add_argument(
        "--yes", "-y", action="store_true", default=False,
        help="Confirm execution of the rollback mutation",
    )
    p_rollback.add_argument(
        "--reason", default=None,
        help="Optional reason describing the rollback mutation",
    )
    p_rollback.set_defaults(func=cmd_rollback)

    # snapshot
    p_snapshot = subparsers.add_parser(
        "snapshot",
        help="Manage reproducible GPA calculation snapshots",
        description=(
            "Create, inspect, reproduce, and list reproducible GPA calculation "
            "snapshots. Snapshots preserve exact input grade records, complete "
            "effective policy configurations, calculation metadata, and content digests "
            "allowing historical GPA results to be independently reproduced."
        ),
    )
    snap_subparsers = p_snapshot.add_subparsers(
        dest="snapshot_action", help="Snapshot actions: create, inspect, reproduce, list"
    )

    # snapshot create
    p_snap_create = snap_subparsers.add_parser(
        "create", help="Create a new GPA calculation snapshot"
    )
    p_snap_create.add_argument("type", choices=["sgpa", "cgpa"], help="Calculation type ('sgpa' or 'cgpa')")
    p_snap_create.add_argument("student_id", help="Student ID to calculate GPA for")
    p_snap_create.add_argument("--semester", default=None, help="Semester (required for SGPA, optional for CGPA)")
    p_snap_create.add_argument("--snapshot-id", default=None, help="Optional custom unique snapshot ID")
    p_snap_create.add_argument("--description", default=None, help="Optional descriptive notes")
    _add_policy_argument(p_snap_create)
    _add_format_argument(p_snap_create)
    p_snap_create.set_defaults(func=cmd_snapshot)

    # snapshot inspect
    p_snap_inspect = snap_subparsers.add_parser(
        "inspect", help="Inspect an existing snapshot's metadata and integrity"
    )
    p_snap_inspect.add_argument("snapshot_id", help="Unique snapshot identifier")
    _add_format_argument(p_snap_inspect)
    p_snap_inspect.set_defaults(func=cmd_snapshot)

    # snapshot reproduce
    p_snap_reproduce = snap_subparsers.add_parser(
        "reproduce", help="Reproduce a GPA calculation using preserved historical inputs and policy"
    )
    p_snap_reproduce.add_argument("snapshot_id", help="Unique snapshot identifier")
    _add_format_argument(p_snap_reproduce)
    p_snap_reproduce.set_defaults(func=cmd_snapshot)

    # snapshot list
    p_snap_list = snap_subparsers.add_parser(
        "list", help="List all available GPA snapshots"
    )
    _add_format_argument(p_snap_list)
    p_snap_list.set_defaults(func=cmd_snapshot)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
