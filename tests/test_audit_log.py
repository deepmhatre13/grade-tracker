"""Tests for tamper-evident audit log creation, hashing, and verification (Issue #56).

Covers:
- Audit event creation, deterministic hashing, canonical bytes serialization.
- Valid hash-chain verification with sequence increments and hash linking.
- Detection of modified, deleted, duplicated, or reordered events.
- Detection of sequence gaps, duplicate sequences, and duplicate event IDs.
- Malformed entries (non-JSON, blank lines, missing required fields).
- Unsupported schema versions.
- Dataset ID consistency and mismatch detection.
- Filtering events by student_id, operation, and timestamp range.
- Chain head retrieval on valid and corrupted logs.
"""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from grade_tracker.audit_log import (
    AUDIT_SCHEMA_VERSION,
    GENESIS_PREV_HASH,
    OPERATIONS,
    AuditIntegrityError,
    append_event,
    audit_log_path_for,
    build_event,
    canonical_bytes,
    chain_head,
    compute_event_hash,
    dataset_id_for,
    event_exists,
    filter_events,
    parse_event,
    pending_journal_path_for,
    read_events,
    serialize_event,
    validate_event,
    verify_log,
)


class TestAuditEventCreationAndHashing(unittest.TestCase):
    """Test audit event building, field ordering, and deterministic hash calculation."""

    def test_build_event_genesis(self):
        records = [
            {
                "student_id": "S101",
                "semester": "1",
                "subject": "Math",
                "score": "95.0",
                "max_score": "100.0",
                "date": "2026-01-15",
                "credits": "3.0",
            }
        ]
        changes = {"before": None, "after": records[0]}
        event = build_event(
            sequence=1,
            operation="add",
            dataset_id="grades:1234567890abcdef",
            records=records,
            changes=changes,
            actor="admin",
            reason="Initial grade entry",
            timestamp="2026-01-15T10:00:00.000000Z",
            event_id="test-event-uuid-1",
        )
        self.assertEqual(event["schema_version"], AUDIT_SCHEMA_VERSION)
        self.assertEqual(event["sequence"], 1)
        self.assertEqual(event["operation"], "add")
        self.assertEqual(event["previous_hash"], GENESIS_PREV_HASH)
        self.assertEqual(event["event_id"], "test-event-uuid-1")
        self.assertEqual(event["actor"], "admin")
        self.assertEqual(event["reason"], "Initial grade entry")
        self.assertTrue(isinstance(event["hash"], str))
        self.assertEqual(len(event["hash"]), 64)

    def test_deterministic_canonical_hashing(self):
        """The same event payload must produce the exact same canonical bytes and hash."""
        records = [{"student_id": "S101", "subject": "Math"}]
        changes = {"before": None, "after": {"score": "90"}}

        # Event built with dict keys in arbitrary order
        event1 = build_event(
            sequence=1,
            operation="add",
            dataset_id="grades:test",
            records=records,
            changes=changes,
            timestamp="2026-01-01T00:00:00.000000Z",
            event_id="fixed-id-1",
        )
        event2 = build_event(
            sequence=1,
            operation="add",
            dataset_id="grades:test",
            records=records,
            changes=changes,
            timestamp="2026-01-01T00:00:00.000000Z",
            event_id="fixed-id-1",
        )
        self.assertEqual(canonical_bytes(event1), canonical_bytes(event2))
        self.assertEqual(compute_event_hash(event1), compute_event_hash(event2))
        self.assertEqual(event1["hash"], event2["hash"])

    def test_hash_covers_all_hashed_fields(self):
        """Modifying any field (except 'hash') changes the computed event hash."""
        base_event = build_event(
            sequence=1,
            operation="add",
            dataset_id="grades:test",
            records=[{"student_id": "S1"}],
            changes={"after": {"score": "80"}},
            actor="user1",
            reason="initial",
            timestamp="2026-01-01T00:00:00.000000Z",
            event_id="id-1",
        )
        original_hash = base_event["hash"]

        mutations = [
            ("sequence", 2),
            ("operation", "edit"),
            ("dataset_id", "grades:other"),
            ("records", [{"student_id": "S2"}]),
            ("changes", {"after": {"score": "85"}}),
            ("actor", "user2"),
            ("reason", "different reason"),
            ("previous_hash", "f" * 64),
            ("timestamp", "2026-01-01T00:00:01.000000Z"),
            ("event_id", "id-2"),
        ]
        for field, new_val in mutations:
            modified = dict(base_event)
            modified[field] = new_val
            new_hash = compute_event_hash(modified)
            self.assertNotEqual(
                original_hash,
                new_hash,
                f"Hash did not change when field '{field}' was modified",
            )

    def test_unsupported_operation_raises(self):
        with self.assertRaises(Exception):
            build_event(
                sequence=1,
                operation="unsupported_op",
                dataset_id="grades:test",
            )


class TestAuditLogVerification(unittest.TestCase):
    """Test verification of audit log hash chains and tamper detection."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log_path = Path(self.temp_dir.name) / "grades.audit.jsonl"
        self.dataset_id = "grades:test-dataset"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_sample_chain(self, count=3):
        events = []
        prev_hash = GENESIS_PREV_HASH
        for seq in range(1, count + 1):
            event = build_event(
                sequence=seq,
                operation="add" if seq == 1 else "edit",
                dataset_id=self.dataset_id,
                records=[{"student_id": f"S{seq}"}],
                changes={"before": None, "after": {"score": str(80 + seq)}},
                previous_hash=prev_hash,
                timestamp=f"2026-01-0{seq}T00:00:00.000000Z",
                event_id=f"event-uuid-{seq}",
            )
            append_event(self.log_path, event)
            events.append(event)
            prev_hash = event["hash"]
        return events

    def test_valid_chain_verifies_cleanly(self):
        self._create_sample_chain(4)
        result = verify_log(self.log_path, dataset_id=self.dataset_id)
        self.assertTrue(result["valid"])
        self.assertIsNone(result["first_invalid"])
        self.assertEqual(result["events_checked"], 4)
        self.assertEqual(result["last_sequence"], 4)
        self.assertEqual(result["dataset_id"], self.dataset_id)

    def test_empty_log_is_valid(self):
        result = verify_log(self.log_path)
        self.assertTrue(result["valid"])
        self.assertIsNone(result["first_invalid"])
        self.assertEqual(result["events_checked"], 0)
        self.assertEqual(result["last_sequence"], 0)

    def test_detect_modified_event_content(self):
        """Tampering with an event's field invalidates the hash check."""
        self._create_sample_chain(3)
        entries = read_events(self.log_path)
        # Modify event 2's actor without updating hash
        tampered_event = dict(entries[1][1])
        tampered_event["actor"] = "attacker"
        lines = [serialize_event(entries[0][1]), serialize_event(tampered_event), serialize_event(entries[2][1])]
        self.log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = verify_log(self.log_path)
        self.assertFalse(result["valid"])
        invalid = result["first_invalid"]
        self.assertEqual(invalid["index"], 1)
        self.assertEqual(invalid["category"], "invalid_event")
        self.assertIn("hash mismatch", invalid["message"])

    def test_detect_recomputed_hash_breaks_chain(self):
        """If an attacker modifies event 2 and recomputes its hash, event 3's link breaks."""
        self._create_sample_chain(3)
        entries = read_events(self.log_path)
        tampered_event = dict(entries[1][1])
        tampered_event["actor"] = "attacker"
        tampered_event["hash"] = compute_event_hash(tampered_event)
        lines = [serialize_event(entries[0][1]), serialize_event(tampered_event), serialize_event(entries[2][1])]
        self.log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = verify_log(self.log_path)
        self.assertFalse(result["valid"])
        invalid = result["first_invalid"]
        self.assertEqual(invalid["index"], 2)
        self.assertEqual(invalid["category"], "broken_link")

    def test_detect_deleted_event(self):
        """Deleting an event creates a broken link or sequence gap."""
        self._create_sample_chain(3)
        entries = read_events(self.log_path)
        # Drop event 1 (sequence 1)
        lines = [serialize_event(entries[1][1]), serialize_event(entries[2][1])]
        self.log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = verify_log(self.log_path)
        self.assertFalse(result["valid"])
        invalid = result["first_invalid"]
        self.assertEqual(invalid["index"], 0)
        self.assertEqual(invalid["category"], "broken_link")

    def test_detect_reordered_events(self):
        """Reordering events in the log breaks hash chaining."""
        self._create_sample_chain(3)
        entries = read_events(self.log_path)
        # Swap events 1 and 2
        lines = [serialize_event(entries[1][1]), serialize_event(entries[0][1]), serialize_event(entries[2][1])]
        self.log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = verify_log(self.log_path)
        self.assertFalse(result["valid"])
        invalid = result["first_invalid"]
        self.assertEqual(invalid["category"], "broken_link")

    def test_detect_duplicate_event(self):
        """Duplicating an event creates duplicate IDs / broken links."""
        self._create_sample_chain(2)
        entries = read_events(self.log_path)
        lines = [serialize_event(entries[0][1]), serialize_event(entries[0][1])]
        self.log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = verify_log(self.log_path)
        self.assertFalse(result["valid"])
        self.assertEqual(result["first_invalid"]["category"], "broken_link")

    def test_detect_duplicate_event_id_with_valid_hash(self):
        """An event reusing an earlier event_id is flagged duplicate_event_id."""
        ev1 = build_event(1, "add", self.dataset_id, [{"student_id": "S1"}], event_id="duplicate-id")
        ev2 = build_event(2, "edit", self.dataset_id, [{"student_id": "S2"}], previous_hash=ev1["hash"], event_id="duplicate-id")
        self.log_path.write_text(serialize_event(ev1) + "\n" + serialize_event(ev2) + "\n", encoding="utf-8")

        result = verify_log(self.log_path)
        self.assertFalse(result["valid"])
        self.assertEqual(result["first_invalid"]["category"], "duplicate_event_id")

    def test_detect_sequence_gap(self):
        ev1 = build_event(1, "add", self.dataset_id, [{"student_id": "S1"}])
        ev2 = build_event(3, "edit", self.dataset_id, [{"student_id": "S2"}], previous_hash=ev1["hash"])
        self.log_path.write_text(serialize_event(ev1) + "\n" + serialize_event(ev2) + "\n", encoding="utf-8")

        result = verify_log(self.log_path)
        self.assertFalse(result["valid"])
        self.assertEqual(result["first_invalid"]["category"], "sequence_gap")

    def test_detect_dataset_mismatch(self):
        ev1 = build_event(1, "add", self.dataset_id, [{"student_id": "S1"}])
        ev2 = build_event(2, "edit", "grades:different-dataset", [{"student_id": "S2"}], previous_hash=ev1["hash"])
        self.log_path.write_text(serialize_event(ev1) + "\n" + serialize_event(ev2) + "\n", encoding="utf-8")

        result = verify_log(self.log_path, dataset_id=self.dataset_id)
        self.assertFalse(result["valid"])
        self.assertEqual(result["first_invalid"]["category"], "dataset_mismatch")

    def test_detect_malformed_json_entry(self):
        self.log_path.write_text('{"schema_version": "1.0", bad json\n', encoding="utf-8")
        result = verify_log(self.log_path)
        self.assertFalse(result["valid"])
        self.assertEqual(result["first_invalid"]["category"], "malformed_entry")

    def test_detect_blank_line(self):
        ev1 = build_event(1, "add", self.dataset_id, [{"student_id": "S1"}])
        self.log_path.write_text("\n" + serialize_event(ev1) + "\n", encoding="utf-8")
        result = verify_log(self.log_path)
        self.assertFalse(result["valid"])
        self.assertEqual(result["first_invalid"]["category"], "malformed_entry")

    def test_detect_unsupported_schema_version(self):
        ev1 = build_event(1, "add", self.dataset_id, [{"student_id": "S1"}])
        ev1["schema_version"] = "99.0"
        ev1["hash"] = compute_event_hash(ev1)
        self.log_path.write_text(serialize_event(ev1) + "\n", encoding="utf-8")

        result = verify_log(self.log_path)
        self.assertFalse(result["valid"])
        self.assertEqual(result["first_invalid"]["category"], "unsupported_schema")


class TestChainHeadAndHelpers(unittest.TestCase):
    """Test chain_head retrieval, event_exists, and filter_events."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log_path = Path(self.temp_dir.name) / "test.audit.jsonl"
        self.dataset_id = "grades:test-helpers"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_chain_head_empty_log(self):
        self.assertIsNone(chain_head(self.log_path))

    def test_chain_head_returns_last_event(self):
        ev1 = build_event(1, "add", self.dataset_id, [{"student_id": "S1"}], event_id="id-1")
        append_event(self.log_path, ev1)
        ev2 = build_event(2, "edit", self.dataset_id, [{"student_id": "S1"}], previous_hash=ev1["hash"], event_id="id-2")
        append_event(self.log_path, ev2)

        head = chain_head(self.log_path)
        self.assertIsNotNone(head)
        self.assertEqual(head["sequence"], 2)
        self.assertEqual(head["event_id"], "id-2")

    def test_chain_head_raises_on_corrupted_log(self):
        ev1 = build_event(1, "add", self.dataset_id, [{"student_id": "S1"}])
        ev1["hash"] = "0" * 64
        append_event(self.log_path, ev1)

        with self.assertRaises(AuditIntegrityError) as ctx:
            chain_head(self.log_path)
        self.assertEqual(ctx.exception.category, "invalid_event")

    def test_event_exists(self):
        ev1 = build_event(1, "add", self.dataset_id, [{"student_id": "S1"}], event_id="unique-123")
        append_event(self.log_path, ev1)
        self.assertTrue(event_exists(self.log_path, "unique-123"))
        self.assertFalse(event_exists(self.log_path, "non-existent"))

    def test_filter_events(self):
        ev1 = build_event(
            sequence=1,
            operation="add",
            dataset_id=self.dataset_id,
            records=[{"student_id": "S1"}],
            timestamp="2026-01-10T12:00:00.000000Z",
        )
        ev2 = build_event(
            sequence=2,
            operation="edit",
            dataset_id=self.dataset_id,
            records=[{"student_id": "S2"}],
            previous_hash=ev1["hash"],
            timestamp="2026-01-15T12:00:00.000000Z",
        )
        ev3 = build_event(
            sequence=3,
            operation="add",
            dataset_id=self.dataset_id,
            records=[{"student_id": "S1"}],
            previous_hash=ev2["hash"],
            timestamp="2026-01-20T12:00:00.000000Z",
        )
        events = [ev1, ev2, ev3]

        # Filter by student_id
        s1_events = filter_events(events, student_id="S1")
        self.assertEqual(len(s1_events), 2)
        self.assertEqual([e["sequence"] for e in s1_events], [1, 3])

        # Filter by operation
        edit_events = filter_events(events, operation="edit")
        self.assertEqual(len(edit_events), 1)
        self.assertEqual(edit_events[0]["sequence"], 2)

        # Filter by date range (inclusive)
        range_events = filter_events(events, since="2026-01-12", until="2026-01-18")
        self.assertEqual(len(range_events), 1)
        self.assertEqual(range_events[0]["sequence"], 2)


class TestAuditPathsAndDatasetId(unittest.TestCase):
    """Test path derivation and stable dataset identifier derivation."""

    def test_dataset_id_stability(self):
        path1 = Path("data/grades.csv")
        path2 = Path("./data/../data/grades.csv")
        id1 = dataset_id_for(path1)
        id2 = dataset_id_for(path2)
        self.assertEqual(id1, id2)
        self.assertTrue(id1.startswith("grades:"))
        self.assertEqual(len(id1), 23)  # "grades:" (7) + 16 hex chars

    def test_audit_log_path_derivation(self):
        csv_path = Path("/tmp/grades.csv")
        log_path = audit_log_path_for(csv_path)
        self.assertEqual(log_path.name, "grades.audit.jsonl")
        self.assertEqual(log_path.parent, csv_path.resolve().parent)

    def test_pending_journal_path_derivation(self):
        csv_path = Path("/tmp/grades.csv")
        journal_path = pending_journal_path_for(csv_path)
        self.assertEqual(journal_path.name, "grades.audit-pending.json")
        self.assertEqual(journal_path.parent, csv_path.resolve().parent)


if __name__ == "__main__":
    unittest.main()

