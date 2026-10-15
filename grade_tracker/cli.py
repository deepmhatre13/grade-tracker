"""Command Line Interface for Grade Tracker."""

import argparse
import csv
import json
import math
import os
import re
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

from grade_tracker.calc import (
    assessment_key as calc_assessment_key,
    cgpa,
    letter_grade,
    percentage,
    sgpa,
    subject_average,
    validate_grade,
)
from grade_tracker.export import build_html

DEFAULT_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "grades.csv"

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


def _grade_json_record(grade, semester=None):
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
        "letter_grade": letter_grade(grade["score"], grade["max_score"])
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


def _atomic_write_rows(path, rows):
    """Write ``rows`` (data records, no header) to ``path`` atomically.

    Writes to a temporary file in the same directory, flushes and ``fsync``\\ s
    it, then ``os.replace``\\ s it over the target.  On any failure before the
    replace succeeds the original file is left byte-for-byte unchanged and the
    temporary file is removed.  Callers must hold the inter-process lock.
    """
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".grades-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                ["student_id", "subject", "score", "max_score", "date", "credits", "semester"]
            )
            for row in rows:
                writer.writerow(row)
            f.flush()
            os.fsync(f.fileno())

        # Atomically replace the target CSV.
        os.replace(tmp_name, path)
        tmp_name = None
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _append_row(path, row):
    """Append one raw CSV row to ``path`` using atomic persistence.

    Shared by ``save_grade`` and ``edit_grade``; both callers must already
    hold the inter-process lock for ``path``.  The file is created (with the
    canonical header) if it does not exist.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _, existing_rows = _read_existing_rows(path)
    _atomic_write_rows(path, list(existing_rows) + [row])


def save_grade(
    student_id,
    subject,
    score,
    max_score,
    date,
    credits,
    semester,
    csv_path=DEFAULT_DATA_PATH,
):
    """Appends a new grade row to the CSV file, unless the assessment exists.

    The read-check-write transaction is protected by a cross-process lock so
    that concurrent `save_grade` calls from separate OS processes cannot
    bypass duplicate detection or corrupt the file.

    Raises:
        ValueError: If the assessment (student_id + semester + subject + date)
            is already recorded.  The file is left unchanged in that case.
    """
    key = assessment_key(student_id, semester, subject, date)

    lock_file = _lock_path_for(csv_path)
    with _InterProcessLock(lock_file) as _lock:
        # Read the latest persisted records while holding the lock.
        existing = load_grades(csv_path)

        # Duplicate detection is performed under the lock.  The identity is
        # derived from the authoritative calc.assessment_key so that the
        # persistence layer can never drift from the calculation layer.
        for grade in existing:
            if assessment_key(
                grade["student_id"],
                grade["semester"],
                grade["subject"],
                grade["date"],
            ) == key:
                raise ValueError(
                    f"assessment already exists for student '{student_id}' "
                    f"in {subject} (semester {semester}) on {date}"
                )

        # Build the updated CSV contents and write atomically.  ``_append_row``
        # performs the schema check and the temp-file/fsync/replace sequence
        # while this transaction still holds the lock.
        _append_row(
            csv_path,
            [student_id, subject, score, max_score, date, credits, semester],
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
):
    """Edit one existing grade row in ``csv_path`` as a single transaction.

    The target record is identified by its current assessment identity
    ``(student_id, semester, subject, date)``.  Any change option left as
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

        if not matches:
            raise ValueError(
                f"no record found for student '{student_id}' (semester {semester}) "
                f"in {subject} on {date}"
            )
        if len(matches) > 1:
            raise ValueError(
                f"ambiguous target: {len(matches)} records match student "
                f"'{student_id}' (semester {semester}) in {subject} on {date}; "
                "the file contains duplicate assessments — repair it with "
                "'validate' before editing"
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
            float(updated["score"]), float(updated["max_score"]), updated["date"]
        )

        # The resulting identity must not collide with any *other* record;
        # the target itself is excluded so retakes and other legitimate
        # duplicates targeting the same row remain unaffected.
        new_key = assessment_key(
            updated["student_id"], updated["semester"], updated["subject"], updated["date"]
        )
        for position, row in enumerate(rows):
            if position == target_position or not _row_has_visible_content(row):
                continue
            other_key = row_key(row)
            if other_key is not None and other_key == new_key:
                raise ValueError(
                    "assessment already exists for student "
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


def _row_has_visible_content(row):
    """True when a parsed CSV record contains at least one non-blank cell."""
    return any(cell.strip() for cell in row)


def _format_value_for_display(raw_value):
    """Render a raw cell in a diagnostic message."""
    text = str(raw_value)
    if not text.strip():
        return "<blank>"
    return repr(text)


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
    path = Path(csv_path)

    if not path.exists():
        return [f"{path}: file not found; nothing to audit"]

    if not path.is_file():
        return [f"{path}: not a regular file"]

    try:
        if path.stat().st_size == 0:
            return [f"{path}: file is empty (no CSV header or records)"]
    except OSError as exc:
        return [f"{path}: cannot read file ({exc})"]

    diagnostics = []

    try:
        with open(path, mode="r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)

            try:
                header = next(reader, None)
            except UnicodeDecodeError as exc:
                return [f"{path}: file is not valid UTF-8 text ({exc})"]

            if header is None:
                return [f"{path}: file is empty (no CSV header or records)"]

            if not _row_has_visible_content(header):
                return [f"{path}: row 1: CSV header is blank; expected the grades schema"]

            expected_columns = [
                "student_id", "subject", "score", "max_score", "date", "credits", "semester"
            ]
            # credits and semester are optional columns: load_grades falls
            # back to 3.0 credits and semester "1" when they are absent, so
            # legacy files that omit them entirely remain valid.
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
                    # csv.reader never yields truly empty records for blank
                    # lines (they are skipped), so this is only a guard.
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
                    # A missing semester column is a valid legacy schema (the
                    # loader defaults to "1"), but a semester column that is
                    # present yet blank in this row is not loadable as-is.
                    if "semester" in index_of:
                        diagnostics.append(
                            f"row {row_number}, field semester: missing or blank semester"
                        )

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
                        credits = float(record["credits"])
                        if not math.isfinite(credits):
                            raise ValueError
                    except ValueError:
                        diagnostics.append(
                            f"row {row_number}, field credits: "
                            f"{_format_value_for_display(record['credits'])} "
                            "is not a numeric credit value"
                        )
                # An absent credits cell is valid: load_grades falls back to
                # the default of 3.0, so legacy rows without credits are kept.

                row_has_error = any(
                    message.startswith(f"row {row_number} ")
                    or message.startswith(f"row {row_number},")
                    for message in diagnostics
                )
                if not row_has_error:
                    # Only rows whose identity fields are all usable can
                    # produce a duplicate-assessment diagnostic; the identity
                    # itself is the authoritative calc.assessment_key.
                    key = calc_assessment_key(
                        {
                            "student_id": record["student_id"],
                            "semester": record["semester"],
                            "subject": record["subject"],
                            "date": record["date"],
                        }
                    )
                    if key is not None:
                        first_row = seen_assessments.get(key)
                        if first_row is None:
                            seen_assessments[key] = row_number
                        else:
                            diagnostics.append(
                                f"row {row_number}: duplicate assessment of row {first_row} "
                                f"(student_id {record['student_id']!r}, "
                                f"semester {record['semester']!r}, "
                                f"subject {record['subject']!r}, "
                                f"date {record['date']!r})"
                            )
    except UnicodeDecodeError as exc:
        return [f"{path}: file is not valid UTF-8 text ({exc})"]
    except OSError as exc:
        return [f"{path}: cannot read file ({exc})"]
    except csv.Error as exc:
        return [f"{path}: malformed CSV ({exc})"]
    except Exception as exc:
        return [f"{path}: unexpected CSV parsing error ({exc})"]

    except csv.Error as exc:
        return [f"{path}: malformed CSV ({exc})"]

    return diagnostics


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


def cmd_list(args):
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
                "grades": [_grade_json_record(g) for g in grades],
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
        lg = letter_grade(g["score"], g["max_score"])
        print(f"{g['student_id']:<12} {g['subject']:<20} {g['score']:<10.1f} {g['max_score']:<8.1f} {lg:<6} {g['date']:<12}")


def cmd_summary(args):
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
                }
            )
            return

        semesters = sorted({g["semester"] for g in student_grades})
        _emit_json(
            {
                "command": "summary",
                "student_id": args.student_id,
                "count": len(student_grades),
                "cgpa": cgpa(grades, args.student_id),
                "semesters": [
                    {
                        "semester": sem,
                        "sgpa": sgpa(grades, args.student_id, sem),
                        "grades": [
                            _grade_json_record(g, str(sem))
                            for g in student_grades
                            if g["semester"] == sem
                        ],
                    }
                    for sem in semesters
                ],
                "grades": [
                    _grade_json_record(g) for g in student_grades
                ],
            }
        )
        return

    grades = load_grades(args.data)
    student_grades = [g for g in grades if g["student_id"] == args.student_id]
    if not student_grades:
        print(f"No records found for student '{args.student_id}'.")
        return

    student_cgpa = cgpa(grades, args.student_id)
    print(f"Summary for Student: {args.student_id}")
    print(f"CGPA: {student_cgpa}")
    print("-" * 50)

    semesters = sorted(list(set(g["semester"] for g in student_grades)))
    for sem in semesters:
        sem_grades = [g for g in student_grades if g["semester"] == sem]
        sem_sgpa = sgpa(grades, args.student_id, sem)
        print(f"Semester {sem} (SGPA: {sem_sgpa})")
        for g in sem_grades:
            lg = letter_grade(g["score"], g["max_score"])
            # Display-only rounding (1 decimal); the grade above is decided
            # from the unrounded percentage, never from this value.
            pct = float(percentage(g["score"], g["max_score"])) if g["max_score"] > 0 else 0
            print(f"  {g['subject']:<18}: {g['score']:.1f}/{g['max_score']:.1f} ({pct:.1f}%) -> {lg} (Credits: {g['credits']})")


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


def cmd_export(args):
    grades = load_grades(args.data)
    output_file = build_html(grades, args.output)
    print(f"HTML report successfully exported to: {output_file}")


def cmd_add(args):
    try:
        validate_grade(args.score, args.max_score, args.date)
        save_grade(args.student_id, args.subject, args.score, args.max_score, args.date, args.credits, args.semester, args.data)
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
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    print(
        f"Edited grade for {args.student_id} in {args.subject} "
        f"(semester {args.semester}) on {args.date}."
    )


def _add_format_argument(parser):
    """Add the shared ``--format`` option to a read-only command parser."""
    parser.add_argument(
        "--format",
        choices=(HUMAN_FORMAT, JSON_FORMAT),
        default=HUMAN_FORMAT,
        dest="format",
        help="Output format: human (default) or json",
    )


def main():
    parser = argparse.ArgumentParser(description="Grade Tracker CLI")
    parser.add_argument("--data", default=DEFAULT_DATA_PATH, help="Path to grades.csv")
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # list
    p_list = subparsers.add_parser("list", help="List all grades")
    _add_format_argument(p_list)
    p_list.set_defaults(func=cmd_list)

    # summary
    p_summary = subparsers.add_parser("summary", help="Show summary for a student")
    p_summary.add_argument("student_id", help="Student ID")
    _add_format_argument(p_summary)
    p_summary.set_defaults(func=cmd_summary)

    # average
    p_avg = subparsers.add_parser("average", help="Show average for a subject")
    p_avg.add_argument("subject", help="Subject name")
    _add_format_argument(p_avg)
    p_avg.set_defaults(func=cmd_average)

    # export
    p_export = subparsers.add_parser("export", help="Export HTML report")
    p_export.add_argument("--output", default="reports/index.html", help="Output file path")
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
    p_add.set_defaults(func=cmd_add)

    # edit
    p_edit = subparsers.add_parser(
        "edit",
        help="Edit an existing grade record",
        description=(
            "Edit an existing grade record. The target record is identified "
            "by its current assessment identity: STUDENT_ID, SEMESTER, "
            "SUBJECT, and DATE. Omitted change options keep the record's "
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

    # validate
    p_validate = subparsers.add_parser(
        "validate", help="Audit the grades CSV for integrity problems (read-only)"
    )
    _add_format_argument(p_validate)
    p_validate.set_defaults(func=cmd_validate)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
