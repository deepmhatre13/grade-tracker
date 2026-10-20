"""Comprehensive regression and concurrency tests for optimistic grade concurrency (Issue #46).

Covers:
1. Token determinism across process restarts and platform serialization.
2. Token field sensitivity (modifying any canonical column invalidates token).
3. Value normalization in revision tokens (spaces, floats, credits, semesters).
4. Rejection of malformed tokens.
5. Tokens do not leak raw values directly.
6. Record inspection: list output in both human and JSON modes with --revisions.
7. Conditional edit success with matching revision token.
8. Conditional edit rejection with stale revision token (file byte-for-byte unchanged).
9. Conditional edit rejection with malformed revision token.
10. Unconditional legacy edits remain backward compatible (omit --expected-revision).
11. Fresh token generated after edit allows subsequent conditional edit.
12. Ambiguous target and missing target reporting.
13. Integrity audit and validation failure atomicity.
14. Atomic replacement failure atomicity and temporary file cleanup.
15. Multi-process concurrency: 2 processes race to update same record with initially valid revision token.
    Exactly one succeeds and one receives stale conflict error; no torn updates or duplicates.
16. Multi-process concurrency: 2 processes update distinct records with their own revisions without conflict.
"""

import contextlib
import csv
import io
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

# Force spawn start method for child processes on Windows/Unix
try:
    multiprocessing.set_start_method("spawn", force=True)
except RuntimeError:
    pass

from grade_tracker.cli import (
    DEFAULT_DATA_PATH,
    _InterProcessLock,
    _lock_path_for,
    compute_record_revision_token,
    edit_grade,
    load_grades,
    main,
    validate_revision_token,
)

HEADER = "student_id,subject,score,max_score,date,credits,semester\n"


def _worker_conditional_edit(csv_path, score, expected_revision, result_queue, barrier):
    """Attempt a conditional edit on record S1/1/Mathematics/2026-10-06 from a subprocess."""
    try:
        barrier.wait(timeout=20.0)
        from grade_tracker.cli import edit_grade

        edit_grade(
            csv_path=str(csv_path),
            student_id="S1",
            semester="1",
            subject="Mathematics",
            date="2026-10-06",
            score=score,
            expected_revision=expected_revision,
        )
        result_queue.put(f"OK-{score}")
    except ValueError as exc:
        if "stale revision conflict" in str(exc):
            result_queue.put(f"CONFLICT: {exc}")
        else:
            result_queue.put(f"VALUEERROR: {exc}")
    except Exception as exc:
        result_queue.put(f"ERROR: {exc!r}")


def _worker_distinct_conditional_edits(csv_path, student_id, subject, score, expected_revision, result_queue, barrier):
    """Attempt a conditional edit on a specific student/subject record."""
    try:
        barrier.wait(timeout=20.0)
        from grade_tracker.cli import edit_grade

        edit_grade(
            csv_path=str(csv_path),
            student_id=student_id,
            semester="1",
            subject=subject,
            date="2026-10-06",
            score=score,
            expected_revision=expected_revision,
        )
        result_queue.put(f"OK-{student_id}-{subject}")
    except Exception as exc:
        result_queue.put(f"ERROR-{student_id}: {exc!r}")


class OptimisticConcurrencyTestCase(unittest.TestCase):
    """Base test case with helper methods for test setup and CLI execution."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_path = Path(self.temp_dir.name) / "grades.csv"

    def write_csv(self, rows):
        text = HEADER + "".join(rows)
        self.data_path.write_text(text, encoding="utf-8", newline="")

    def file_bytes(self):
        return self.data_path.read_bytes()

    def read_rows(self):
        if not self.data_path.exists():
            return []
        with self.data_path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def temp_files(self):
        return sorted(
            p.name for p in Path(self.temp_dir.name).iterdir() if p.name.endswith(".tmp")
        )

    def run_cli(self, *args):
        argv = ["grade_tracker", "--data", str(self.data_path), *args]
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            mock.patch("sys.argv", argv),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                main()
                code = 0
            except SystemExit as exc:
                code = exc.code

        return code, stdout.getvalue(), stderr.getvalue()

    def parse_json(self, stdout):
        return json.loads(stdout)


class TestRevisionTokens(unittest.TestCase):
    """Test deterministic token generation, format, sensitivity, and normalization."""

    def test_token_format_matches_documented_sha256(self):
        record = {
            "student_id": "S1",
            "subject": "Mathematics",
            "score": 95.0,
            "max_score": 100.0,
            "date": "2026-10-06",
            "credits": 3.0,
            "semester": "1",
        }
        token = compute_record_revision_token(record)
        self.assertTrue(token.startswith("sha256:"))
        digest = token.split("sha256:")[1]
        self.assertEqual(len(digest), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in digest))
        # Validation helper accepts valid token
        validate_revision_token(token)

    def test_tokens_are_deterministic_and_reproducible(self):
        record1 = {
            "student_id": "S1",
            "subject": "Mathematics",
            "score": 95.0,
            "max_score": 100.0,
            "date": "2026-10-06",
            "credits": 3.0,
            "semester": "1",
        }
        record2 = dict(record1)
        token1 = compute_record_revision_token(record1)
        token2 = compute_record_revision_token(record2)
        self.assertEqual(token1, token2)

    def test_tokens_do_not_embed_raw_field_values(self):
        record = {
            "student_id": "SUPER_SECRET_STUDENT_ID",
            "subject": "TOP_SECRET_SUBJECT",
            "score": 99.5,
            "max_score": 100.0,
            "date": "2026-10-06",
            "credits": 4.0,
            "semester": "2",
        }
        token = compute_record_revision_token(record)
        self.assertNotIn("SUPER_SECRET_STUDENT_ID", token)
        self.assertNotIn("TOP_SECRET_SUBJECT", token)
        self.assertNotIn("99.5", token)

    def test_field_sensitivity_all_canonical_fields(self):
        base = {
            "student_id": "S1",
            "subject": "Mathematics",
            "score": 95.0,
            "max_score": 100.0,
            "date": "2026-10-06",
            "credits": 3.0,
            "semester": "1",
        }
        base_token = compute_record_revision_token(base)

        variations = [
            ("student_id", "S2"),
            ("subject", "Physics"),
            ("score", 94.0),
            ("max_score", 150.0),
            ("date", "2026-10-07"),
            ("credits", 4.0),
            ("semester", "2"),
        ]

        for field, new_val in variations:
            with self.subTest(field=field, new_val=new_val):
                mod = dict(base)
                mod[field] = new_val
                mod_token = compute_record_revision_token(mod)
                self.assertNotEqual(
                    base_token,
                    mod_token,
                    f"Modifying {field} to {new_val} should change revision token",
                )

    def test_field_normalization(self):
        # Stripping leading/trailing spaces from string values
        r1 = {
            "student_id": "S1",
            "subject": "Mathematics",
            "score": 95.0,
            "max_score": 100.0,
            "date": "2026-10-06",
            "credits": 3.0,
            "semester": "1",
        }
        r2 = {
            "student_id": "  S1  ",
            "subject": " Mathematics  ",
            "score": "95",
            "max_score": "100.0",
            "date": " 2026-10-06 ",
            "credits": "3.0",
            "semester": " 1 ",
        }
        self.assertEqual(
            compute_record_revision_token(r1),
            compute_record_revision_token(r2),
        )

    def test_validate_revision_token_rejects_malformed_tokens(self):
        invalid_tokens = [
            "",
            "sha256:",
            "md5:d41d8cd98f00b204e9800998ecf8427e",
            "sha256:" + "a" * 63,  # 63 chars
            "sha256:" + "a" * 65,  # 65 chars
            "sha256:" + "g" * 64,  # non-hex chars
            "sha256:" + "A" * 64,  # uppercase
            None,
            123,
            "random_string",
        ]
        for token in invalid_tokens:
            with self.subTest(token=token):
                with self.assertRaises(ValueError) as cm:
                    validate_revision_token(token)
                self.assertIn("invalid revision token format", str(cm.exception))


class TestTokenInspection(OptimisticConcurrencyTestCase):
    """Test obtaining revision tokens via list command in human and JSON modes."""

    def test_list_human_mode_with_revisions_flag(self):
        self.write_csv([
            "S1,Mathematics,95,100,2026-10-06,3,1\n",
        ])
        code, stdout, stderr = self.run_cli("list", "--revisions")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Revision", stdout)
        self.assertIn("sha256:", stdout)
        self.assertIn("S1", stdout)
        self.assertIn("Mathematics", stdout)

    def test_list_json_mode_with_revisions_flag(self):
        self.write_csv([
            "S1,Mathematics,95,100,2026-10-06,3,1\n",
        ])
        code, stdout, stderr = self.run_cli("list", "--revisions", "--format", "json")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        payload = self.parse_json(stdout)
        self.assertEqual(payload["count"], 1)
        record = payload["grades"][0]
        self.assertIn("revision_token", record)
        self.assertTrue(record["revision_token"].startswith("sha256:"))

        # Verify token matches directly computed token
        expected = compute_record_revision_token({
            "student_id": "S1",
            "subject": "Mathematics",
            "score": 95.0,
            "max_score": 100.0,
            "date": "2026-10-06",
            "credits": 3.0,
            "semester": "1",
        })
        self.assertEqual(record["revision_token"], expected)

    def test_list_json_mode_without_revisions_flag_preserves_contract(self):
        self.write_csv([
            "S1,Mathematics,95,100,2026-10-06,3,1\n",
        ])
        code, stdout, stderr = self.run_cli("list", "--format", "json")
        self.assertEqual(code, 0)
        payload = self.parse_json(stdout)
        record = payload["grades"][0]
        self.assertNotIn("revision_token", record)


class TestConditionalEdit(OptimisticConcurrencyTestCase):
    """Test optimistic concurrency control during edit command execution."""

    def setUp(self):
        super().setUp()
        self.write_csv([
            "S1,Mathematics,50,100,2026-10-06,3,1\n",
            "S1,Physics,80,100,2026-10-06,3,1\n",
        ])
        self.initial_token = compute_record_revision_token({
            "student_id": "S1",
            "subject": "Mathematics",
            "score": 50.0,
            "max_score": 100.0,
            "date": "2026-10-06",
            "credits": 3.0,
            "semester": "1",
        })

    def test_edit_succeeds_with_matching_revision(self):
        code, stdout, stderr = self.run_cli(
            "edit", "S1", "1", "Mathematics", "2026-10-06",
            "--score", "75",
            "--expected-revision", self.initial_token,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Edited grade for S1 in Mathematics", stdout)

        rows = self.read_rows()
        self.assertEqual(rows[0]["score"], "75.0")
        self.assertEqual(rows[1]["score"], "80")

    def test_edit_rejected_with_stale_revision(self):
        stale_token = "sha256:" + "0" * 64
        before = self.file_bytes()

        code, stdout, stderr = self.run_cli(
            "edit", "S1", "1", "Mathematics", "2026-10-06",
            "--score", "75",
            "--expected-revision", stale_token,
        )
        self.assertEqual(code, 2)
        self.assertIn("stale revision conflict", stderr)
        self.assertIn(stale_token, stderr)
        self.assertIn(self.initial_token, stderr)
        self.assertIn("Please inspect the current record with 'list'", stderr)
        self.assertEqual(stdout, "")
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(self.temp_files(), [])

    def test_edit_rejected_with_malformed_revision(self):
        before = self.file_bytes()

        code, stdout, stderr = self.run_cli(
            "edit", "S1", "1", "Mathematics", "2026-10-06",
            "--score", "75",
            "--expected-revision", "invalid-token",
        )
        self.assertEqual(code, 2)
        self.assertIn("invalid revision token format", stderr)
        self.assertEqual(stdout, "")
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(self.temp_files(), [])

    def test_token_becomes_stale_after_intermediate_edit(self):
        # 1. First edit updates score to 75
        code1, _, _ = self.run_cli(
            "edit", "S1", "1", "Mathematics", "2026-10-06",
            "--score", "75",
            "--expected-revision", self.initial_token,
        )
        self.assertEqual(code1, 0)

        # 2. Second attempt using initial_token fails because revision has changed
        code2, _, stderr2 = self.run_cli(
            "edit", "S1", "1", "Mathematics", "2026-10-06",
            "--score", "85",
            "--expected-revision", self.initial_token,
        )
        self.assertEqual(code2, 2)
        self.assertIn("stale revision conflict", stderr2)

        # 3. Computing fresh token allows next edit
        fresh_token = compute_record_revision_token({
            "student_id": "S1",
            "subject": "Mathematics",
            "score": 75.0,
            "max_score": 100.0,
            "date": "2026-10-06",
            "credits": 3.0,
            "semester": "1",
        })
        code3, _, stderr3 = self.run_cli(
            "edit", "S1", "1", "Mathematics", "2026-10-06",
            "--score", "85",
            "--expected-revision", fresh_token,
        )
        self.assertEqual(code3, 0)
        self.assertEqual(stderr3, "")
        self.assertEqual(self.read_rows()[0]["score"], "85.0")

    def test_legacy_edit_without_token_still_succeeds(self):
        # Omitting --expected-revision preserves backward compatibility
        code, stdout, stderr = self.run_cli(
            "edit", "S1", "1", "Mathematics", "2026-10-06",
            "--score", "90",
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(self.read_rows()[0]["score"], "90.0")

    def test_nonexistent_record_rejected_distinctly(self):
        code, stdout, stderr = self.run_cli(
            "edit", "UNKNOWN_STUDENT", "1", "Mathematics", "2026-10-06",
            "--score", "90",
            "--expected-revision", self.initial_token,
        )
        self.assertEqual(code, 2)
        self.assertIn("no record found for student 'UNKNOWN_STUDENT'", stderr)
        self.assertNotIn("stale revision conflict", stderr)

    def test_ambiguous_target_rejected_without_modifying_file(self):
        self.write_csv([
            "S1,Mathematics,50,100,2026-10-06,3,1\n",
            "S1,Mathematics,60,100,2026-10-06,3,1\n",
        ])
        before = self.file_bytes()

        code, stdout, stderr = self.run_cli(
            "edit", "S1", "1", "Mathematics", "2026-10-06",
            "--score", "90",
            "--expected-revision", self.initial_token,
        )
        self.assertEqual(code, 2)
        self.assertIn("ambiguous target", stderr)
        self.assertEqual(self.file_bytes(), before)


class TestFailureAtomicity(OptimisticConcurrencyTestCase):
    """Verify byte-for-byte preservation and temp-file cleanup on errors."""

    def test_validation_failure_preserves_file(self):
        self.write_csv(["S1,Mathematics,50,100,2026-10-06,3,1\n"])
        before = self.file_bytes()
        token = compute_record_revision_token({
            "student_id": "S1",
            "subject": "Mathematics",
            "score": 50.0,
            "max_score": 100.0,
            "date": "2026-10-06",
            "credits": 3.0,
            "semester": "1",
        })

        code, _, stderr = self.run_cli(
            "edit", "S1", "1", "Mathematics", "2026-10-06",
            "--score", "200",  # Invalid score > max_score
            "--expected-revision", token,
        )
        self.assertEqual(code, 2)
        self.assertIn("score must be between 0 and max_score", stderr)
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(self.temp_files(), [])

    def test_simulated_replace_failure_preserves_original(self):
        self.write_csv(["S1,Mathematics,50,100,2026-10-06,3,1\n"])
        before = self.file_bytes()
        token = compute_record_revision_token({
            "student_id": "S1",
            "subject": "Mathematics",
            "score": 50.0,
            "max_score": 100.0,
            "date": "2026-10-06",
            "credits": 3.0,
            "semester": "1",
        })

        with mock.patch("grade_tracker.cli.os.replace", side_effect=OSError("simulated permission error")):
            code, _, stderr = self.run_cli(
                "edit", "S1", "1", "Mathematics", "2026-10-06",
                "--score", "80",
                "--expected-revision", token,
            )

        self.assertEqual(code, 2)
        self.assertIn("failed to write", stderr)
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(self.temp_files(), [])


class TestOptimisticConcurrencyMultiProcess(OptimisticConcurrencyTestCase):
    """Genuine multiprocessing tests for optimistic concurrency control."""

    def _join_children_and_collect_results(self, children, result_queue):
        deadline = time.monotonic() + 40.0
        while time.monotonic() < deadline and any(p.is_alive() for p in children):
            for p in children:
                if p.is_alive():
                    p.join(timeout=max(0.1, deadline - time.monotonic()))
        if any(p.is_alive() for p in children):
            for p in children:
                p.terminate()
            self.fail("concurrent workers did not finish within timeout")
        results = []
        while not result_queue.empty():
            try:
                results.append(result_queue.get_nowait())
            except Exception:
                break
        return results

    def test_two_concurrent_edits_same_token_exactly_one_succeeds(self):
        """Two processes contend on the same record with the same initial revision.

        Exactly one must succeed, and the second must receive a stale revision conflict.
        The file must contain exactly one updated record (no lost updates or duplicate rows).
        """
        self.write_csv(["S1,Mathematics,50,100,2026-10-06,3,1\n"])
        initial_token = compute_record_revision_token({
            "student_id": "S1",
            "subject": "Mathematics",
            "score": 50.0,
            "max_score": 100.0,
            "date": "2026-10-06",
            "credits": 3.0,
            "semester": "1",
        })

        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(2)
        result_queue = ctx.Queue()

        p1 = ctx.Process(
            target=_worker_conditional_edit,
            args=(str(self.data_path), 60.0, initial_token, result_queue, barrier),
        )
        p2 = ctx.Process(
            target=_worker_conditional_edit,
            args=(str(self.data_path), 70.0, initial_token, result_queue, barrier),
        )

        p1.start()
        p2.start()

        results = self._join_children_and_collect_results([p1, p2], result_queue)
        self.assertEqual(len(results), 2, f"unexpected worker count: {results!r}")

        successes = [r for r in results if r.startswith("OK-")]
        conflicts = [r for r in results if r.startswith("CONFLICT:")]

        self.assertEqual(
            len(successes), 1,
            f"Expected exactly 1 successful edit, got results: {results!r}",
        )
        self.assertEqual(
            len(conflicts), 1,
            f"Expected exactly 1 conflict error, got results: {results!r}",
        )

        rows = self.read_rows()
        self.assertEqual(len(rows), 1, "Expected exactly 1 record in CSV")
        winning_score = "60.0" if successes[0] == "OK-60.0" else "70.0"
        self.assertEqual(rows[0]["score"], winning_score)

    def test_two_concurrent_edits_different_records_both_succeed(self):
        """Edits to two distinct records using their respective tokens succeed without conflict."""
        self.write_csv([
            "S1,Mathematics,50,100,2026-10-06,3,1\n",
            "S2,Physics,80,100,2026-10-06,4,1\n",
        ])
        token1 = compute_record_revision_token({
            "student_id": "S1",
            "subject": "Mathematics",
            "score": 50.0,
            "max_score": 100.0,
            "date": "2026-10-06",
            "credits": 3.0,
            "semester": "1",
        })
        token2 = compute_record_revision_token({
            "student_id": "S2",
            "subject": "Physics",
            "score": 80.0,
            "max_score": 100.0,
            "date": "2026-10-06",
            "credits": 4.0,
            "semester": "1",
        })

        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(2)
        result_queue = ctx.Queue()

        p1 = ctx.Process(
            target=_worker_distinct_conditional_edits,
            args=(str(self.data_path), "S1", "Mathematics", 65.0, token1, result_queue, barrier),
        )
        p2 = ctx.Process(
            target=_worker_distinct_conditional_edits,
            args=(str(self.data_path), "S2", "Physics", 85.0, token2, result_queue, barrier),
        )

        p1.start()
        p2.start()

        results = self._join_children_and_collect_results([p1, p2], result_queue)
        self.assertEqual(len(results), 2, f"unexpected results: {results!r}")
        self.assertTrue(all(r.startswith("OK-") for r in results), f"workers failed: {results!r}")

        rows = self.read_rows()
        self.assertEqual(len(rows), 2)
        scores_by_student = {r["student_id"]: r["score"] for r in rows}
        self.assertEqual(scores_by_student["S1"], "65.0")
        self.assertEqual(scores_by_student["S2"], "85.0")


if __name__ == "__main__":
    unittest.main()

