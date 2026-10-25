"""Tamper-evident audit history for grade mutations.

This module defines a versioned audit event format and an append-only
SHA-256 hash chain that records every committed grade mutation.

Design summary
--------------

* **Event**: a JSON object with a fixed, documented field order.  Every event
  carries a unique ``event_id``, a monotonically increasing ``sequence``
  number, the ``operation`` that produced it, a UTC ``timestamp``, the
  ``dataset_id`` it applies to, the affected ``records``, the ``changes``
  (before/after information), an optional ``actor`` and ``reason``, the
  ``previous_hash`` of the chain and its own ``hash``.
* **Canonical serialization**: :func:`canonical_bytes` emits the event fields
  in the explicit order of :data:`EVENT_FIELD_ORDER` (the ``hash`` field
  itself is excluded).  Each value is serialized with ``sort_keys=True`` and
  compact separators, so nested objects have a deterministic key order.  The
  resulting UTF-8 bytes are hashed with SHA-256.
* **Hash chain**: ``hash(event) = sha256(canonical_bytes(event))`` where the
  canonical bytes include ``previous_hash``.  The first event of a chain
  references :data:`GENESIS_PREV_HASH` (64 zeros).
* **Storage**: one JSON object per line (JSON Lines) in a file that lives
  next to, but is separate from, the canonical grades CSV.

Threat model
------------

The hash chain detects *inconsistent* history: modified event contents,
invalid hashes, deleted/reordered/duplicated events and broken links are all
detected by :func:`verify_log`.  It does **not** independently prove
authenticity against an attacker who can rewrite the whole log and recompute
every hash.  A local chain is evidence of internal consistency, not proof of
authorship.
"""

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

AUDIT_SCHEMA_VERSION = "1.0"
SUPPORTED_SCHEMA_VERSIONS = ("1.0",)

#: The ``previous_hash`` of the first event of every chain.
GENESIS_PREV_HASH = "0" * 64

#: Operation types recorded in the audit log.
OPERATIONS = ("add", "edit", "import", "delete", "restore", "migrate")

#: Explicit, documented field order used for canonical serialization.
#: The ``hash`` field is excluded from the hashed payload.
EVENT_FIELD_ORDER = (
    "schema_version",
    "event_id",
    "sequence",
    "operation",
    "timestamp",
    "dataset_id",
    "records",
    "changes",
    "actor",
    "reason",
    "previous_hash",
    "hash",
)

#: Fields that participate in the event hash.
HASHED_FIELDS = tuple(f for f in EVENT_FIELD_ORDER if f != "hash")

_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class AuditLogError(Exception):
    """Base class for audit log errors."""


class AuditEventError(AuditLogError):
    """Raised when an event is structurally invalid or cannot be built."""


class AuditIntegrityError(AuditLogError):
    """Raised when the audit chain is inconsistent or corrupted.

    The attributes identify the first offending event so callers can report
    an actionable diagnostic instead of a generic failure.
    """

    def __init__(self, message, category=None, index=None, sequence=None,
                 event_id=None):
        super().__init__(message)
        self.category = category
        self.index = index
        self.sequence = sequence
        self.event_id = event_id


def utc_timestamp():
    """Return the current UTC time in the documented timestamp format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def canonical_bytes(event):
    """Serialize ``event`` to the canonical bytes used for hashing.

    The output is a JSON object whose keys appear in :data:`HASHED_FIELDS`
    order.  Nested values are serialized with sorted keys and compact
    separators so the same logical event always produces the same bytes.
    """
    parts = []
    for field in HASHED_FIELDS:
        value = event.get(field)
        parts.append(
            json.dumps(field)
            + ":"
            + json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    return ("{" + ",".join(parts) + "}").encode("utf-8")


def compute_event_hash(event):
    """Return the SHA-256 hex digest of ``event``'s canonical bytes."""
    return hashlib.sha256(canonical_bytes(event)).hexdigest()


def build_event(
    sequence,
    operation,
    dataset_id,
    records=None,
    changes=None,
    actor=None,
    reason=None,
    previous_hash=GENESIS_PREV_HASH,
    event_id=None,
    timestamp=None,
):
    """Build a complete, hashed audit event.

    The event's own ``hash`` covers the canonical bytes, which include
    ``previous_hash``; this is what links the chain together.
    """
    if operation not in OPERATIONS:
        raise AuditEventError(f"unsupported audit operation: {operation!r}")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise AuditEventError(f"invalid audit sequence number: {sequence!r}")
    if not isinstance(dataset_id, str) or not dataset_id:
        raise AuditEventError("audit dataset_id must be a non-empty string")

    event = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "event_id": event_id or uuid.uuid4().hex,
        "sequence": sequence,
        "operation": operation,
        "timestamp": timestamp or utc_timestamp(),
        "dataset_id": dataset_id,
        "records": list(records or []),
        "changes": dict(changes or {}),
        "actor": actor,
        "reason": reason,
        "previous_hash": previous_hash or GENESIS_PREV_HASH,
    }
    event["hash"] = compute_event_hash(event)
    return event



def validate_event(event):
    """Validate the structure and hash of a single event.

    Returns a list of human-readable problems; an empty list means the
    event is well-formed and its hash matches its contents.
    """
    problems = []
    if not isinstance(event, dict):
        return ["event is not a JSON object"]

    for field in HASHED_FIELDS + ("hash",):
        if field not in event:
            problems.append(f"missing required field: {field}")
    if problems:
        return problems

    version = event["schema_version"]
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        problems.append(
            f"unsupported schema version: {version!r} "
            f"(supported: {', '.join(SUPPORTED_SCHEMA_VERSIONS)})"
        )

    if not isinstance(event["event_id"], str) or not event["event_id"]:
        problems.append("event_id must be a non-empty string")

    sequence = event["sequence"]
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        problems.append(f"sequence must be a positive integer, got {sequence!r}")

    if event["operation"] not in OPERATIONS:
        problems.append(
            f"unsupported operation: {event['operation']!r} "
            f"(supported: {', '.join(OPERATIONS)})"
        )

    if not isinstance(event["timestamp"], str) or not _TIMESTAMP_RE.match(
        event["timestamp"]
    ):
        problems.append(
            "timestamp must use the UTC format YYYY-MM-DDThh:mm:ss.ffffffZ, "
            f"got {event['timestamp']!r}"
        )
    else:
        try:
            datetime.strptime(event["timestamp"], "%Y-%m-%dT%H:%M:%S.%fZ")
        except ValueError:
            problems.append(
                f"timestamp is not a valid calendar time: {event['timestamp']!r}"
            )

    if not isinstance(event["dataset_id"], str) or not event["dataset_id"]:
        problems.append("dataset_id must be a non-empty string")

    if not isinstance(event["records"], list):
        problems.append("records must be a list")
    else:
        for index, record in enumerate(event["records"]):
            if not isinstance(record, dict):
                problems.append(f"records[{index}] must be an object")
                break

    if not isinstance(event["changes"], dict):
        problems.append("changes must be an object")

    for optional in ("actor", "reason"):
        if event.get(optional) is not None and not isinstance(event[optional], str):
            problems.append(f"{optional} must be a string or null")

    for field in ("previous_hash", "hash"):
        value = event[field]
        if not isinstance(value, str) or not _HASH_RE.match(value):
            problems.append(f"{field} must be a 64-character lowercase hex digest")

    if not problems:
        expected = compute_event_hash(event)
        if expected != event["hash"]:
            problems.append(
                f"hash mismatch: event records {event['hash']} "
                f"but contents hash to {expected}"
            )

    return problems


def serialize_event(event):
    """Return the single-line JSON representation stored in the log file."""
    ordered = {}
    for field in EVENT_FIELD_ORDER:
        ordered[field] = event.get(field)
    return json.dumps(
        ordered,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def parse_event(line):
    """Parse and validate one log line.

    Returns ``(event, problems)``.  ``event`` is ``None`` when the line
    cannot be parsed at all.
    """
    try:
        event = json.loads(line)
    except json.JSONDecodeError as exc:
        return None, [f"malformed JSON: {exc}"]
    if not isinstance(event, dict):
        return None, ["entry is not a JSON object"]
    problems = validate_event(event)
    return event, problems


def read_events(log_path):
    """Read all entries from ``log_path``.

    Returns a list of ``(index, event_or_None, problems)`` tuples.  A
    missing log file yields an empty list.  Malformed lines are reported,
    never dropped silently.
    """
    path = Path(log_path)
    entries = []
    if not path.exists():
        return entries
    with open(path, mode="r", encoding="utf-8") as handle:
        raw = handle.read()
    if raw == "":
        return entries
    lines = raw.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for index, line in enumerate(lines):
        if line.strip() == "":
            entries.append((index, None, ["blank line in audit log"]))
            continue
        event, problems = parse_event(line)
        entries.append((index, event, problems))
    return entries



def append_event(log_path, event):
    """Append ``event`` to the append-only log file and flush it to disk.

    The caller must hold the dataset lock so that sequence numbers stay
    strictly increasing and the chain stays linear.
    """
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = serialize_event(event) + "\n"
    with open(path, mode="a", encoding="utf-8", newline="") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def event_exists(log_path, event_id):
    """Return True when an event with ``event_id`` is already stored."""
    for _, event, problems in read_events(log_path):
        if event is not None and not problems and event.get("event_id") == event_id:
            return True
    return False


def chain_head(log_path):
    """Return the last event of the chain, or ``None`` when the log is empty.

    Raises :class:`AuditIntegrityError` when the stored log is corrupted, so
    a mutation never appends to a broken chain.
    """
    result = verify_log(log_path)
    if not result["valid"]:
        failure = result["first_invalid"]
        raise AuditIntegrityError(
            "audit log is corrupted and cannot be extended: "
            f"{failure['message']}",
            category=failure["category"],
            index=failure.get("index"),
            sequence=failure.get("sequence"),
            event_id=failure.get("event_id"),
        )
    return result["events"][-1] if result["events"] else None


def verify_log(log_path, dataset_id=None):
    """Verify the hash chain stored in ``log_path``.

    The result is a dictionary with the keys ``valid``, ``events_checked``,
    ``last_sequence``, ``events`` and ``first_invalid``.  ``first_invalid`` is
    ``None`` for a valid chain and otherwise describes the *first* invalid
    event with a ``category`` and an actionable ``message``.
    """
    entries = read_events(log_path)
    events = []
    first_invalid = None
    expected_sequence = 1
    expected_previous = GENESIS_PREV_HASH
    seen_ids = set()
    seen_sequences = set()
    last_sequence = 0
    dataset = dataset_id

    for index, event, problems in entries:
        if first_invalid is not None:
            continue

        if event is None:
            first_invalid = {
                "index": index,
                "sequence": None,
                "event_id": None,
                "category": "malformed_entry",
                "message": f"entry {index} is malformed: {'; '.join(problems)}",
            }
            continue

        if problems:
            category = (
                "unsupported_schema"
                if any("unsupported schema version" in p for p in problems)
                else "invalid_event"
            )
            first_invalid = {
                "index": index,
                "sequence": event.get("sequence"),
                "event_id": event.get("event_id"),
                "category": category,
                "message": f"entry {index} is invalid: {'; '.join(problems)}",
            }
            continue

        if event["previous_hash"] != expected_previous:
            first_invalid = {
                "index": index,
                "sequence": event["sequence"],
                "event_id": event["event_id"],
                "category": "broken_link",
                "message": (
                    f"entry {index} (sequence {event['sequence']}) references "
                    f"previous_hash {event['previous_hash'][:12]}... but the "
                    f"chain expects {expected_previous[:12]}...; the event "
                    "was reordered, deleted, or the chain was rewritten"
                ),
            }
            continue

        if event["sequence"] != expected_sequence:
            category = (
                "duplicate_sequence"
                if event["sequence"] in seen_sequences
                else "sequence_gap"
            )
            first_invalid = {
                "index": index,
                "sequence": event["sequence"],
                "event_id": event["event_id"],
                "category": category,
                "message": (
                    f"entry {index} has sequence {event['sequence']} but the "
                    f"chain expects {expected_sequence}"
                ),
            }
            continue

        if event["event_id"] in seen_ids:
            first_invalid = {
                "index": index,
                "sequence": event["sequence"],
                "event_id": event["event_id"],
                "category": "duplicate_event_id",
                "message": f"entry {index} repeats event_id {event['event_id']}",
            }
            continue

        if dataset is None:
            dataset = event["dataset_id"]
        elif event["dataset_id"] != dataset:
            first_invalid = {
                "index": index,
                "sequence": event["sequence"],
                "event_id": event["event_id"],
                "category": "dataset_mismatch",
                "message": (
                    f"entry {index} refers to dataset {event['dataset_id']!r} "
                    f"but the chain belongs to dataset {dataset!r}"
                ),
            }
            continue

        seen_ids.add(event["event_id"])
        seen_sequences.add(event["sequence"])
        events.append(event)
        last_sequence = event["sequence"]
        expected_sequence = event["sequence"] + 1
        expected_previous = event["hash"]

    return {
        "valid": first_invalid is None,
        "events_checked": len(events),
        "last_sequence": last_sequence,
        "events": events,
        "first_invalid": first_invalid,
        "dataset_id": dataset,
    }


def filter_events(events, student_id=None, operation=None, since=None, until=None):
    """Filter ``events`` by student, operation type and timestamp range.

    ``since``/``until`` are inclusive ISO dates (``YYYY-MM-DD``).  The
    filtering is read-only and never mutates the events.
    """
    selected = []
    for event in events:
        if operation is not None and event.get("operation") != operation:
            continue
        if student_id is not None:
            wanted = str(student_id).strip()
            matched = any(
                str(record.get("student_id", "")).strip() == wanted
                for record in event.get("records", [])
            )
            if not matched:
                continue
        day = event.get("timestamp", "")[:10]
        if since is not None and day < since:
            continue
        if until is not None and day > until:
            continue
        selected.append(event)
    return selected


def audit_log_path_for(csv_path):
    """Return the audit log path that belongs to ``csv_path``.

    The log lives next to the CSV but is a separate file, so the canonical
    grades data and the audit history never share storage.
    """
    path = Path(csv_path).resolve()
    return path.parent / (path.stem + ".audit.jsonl")


def pending_journal_path_for(csv_path):
    """Return the pending-transaction journal path for ``csv_path``."""
    path = Path(csv_path).resolve()
    return path.parent / (path.stem + ".audit-pending.json")


def dataset_id_for(csv_path):
    """Return the stable dataset identifier for ``csv_path``.

    The identifier is derived from the resolved absolute path so that every
    event recorded against the same CSV agrees on the dataset identity.
    """
    raw = Path(csv_path).resolve().as_posix().encode("utf-8")
    return "grades:" + hashlib.sha256(raw).hexdigest()[:16]

