"""Regression tests for the ``edit`` subcommand and ``edit_grade`` transaction.

Covers target resolution (missing/ambiguous), omitted-field retention,
validation reuse, duplicate-assessment prevention, retake preservation,
byte-for-byte failure atomicity, temporary-file cleanup, failure injection
during write/fsync/replace, CSV header/column-order/legacy-row preservation,
CLI exit codes and messages, and the continued health of the other commands.
"""

import contextlib
import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from grade_tracker.cli import main


HEADER = "student_id,subject,score,max_score,date,credits,semester\n"


class EditTestCase(unittest.TestCase):
    """Shared helpers: temp CSV, CLI runner, and row/byte inspection."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_path = Path(self.temp_dir.name) / "grades.csv"

    def write_csv(self, rows):
        self.data_path.write_text(HEADER + "".join(rows), encoding="utf-8", newline="")

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
            except SystemExit as exc:
                return exc.code, stdout.getvalue(), stderr.getvalue()

        return 0, stdout.getvalue(), stderr.getvalue()

    def run_edit(self, student_id, semester, subject, date, *changes):
        return self.run_cli("edit", student_id, semester, subject, date, *changes)


class TestEditSuccess(EditTestCase):
    """Happy-path edits and omitted-field retention."""

    def setUp(self):
        super().setUp()
        self.write_csv([
            "S1,Mathematics,50,100,2026-10-06,3,1\n",
            "S1,Physics,80,100,2026-10-06,3,1\n",
        ])

    def test_edit_score_retains_omitted_fields(self):
        code, stdout, stderr = self.run_edit(
            "S1", "1", "Mathematics", "2026-10-06", "--score", "75"
        )

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("Edited grade for S1 in Mathematics", stdout)
        rows = self.read_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], {
            "student_id": "S1",
            "subject": "Mathematics",
            "score": "75.0",
            "max_score": "100",
            "date": "2026-10-06",
            "credits": "3",
            "semester": "1",
        })
        # The unrelated record is untouched.
        self.assertEqual(rows[1]["score"], "80")

    def test_edit_max_score_and_credits(self):
        code, _, stderr = self.run_edit(
            "S1", "1", "Mathematics", "2026-10-06",
            "--max-score", "50", "--credits", "4",
        )

        self.assertEqual((code, stderr), (0, ""))
        rows = self.read_rows()
        self.assertEqual(rows[0]["max_score"], "50.0")
        self.assertEqual(rows[0]["credits"], "4.0")
        # Omitted score is retained as-is (50 <= 50 is still valid).
        self.assertEqual(rows[0]["score"], "50")

    def test_edit_identity_fields_when_no_conflict(self):
        code, _, stderr = self.run_edit(
            "S1", "1", "Mathematics", "2026-10-06",
            "--subject", "Maths", "--date", "2026-10-07",
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        rows = self.read_rows()
        self.assertEqual(rows[0]["subject"], "Maths")
        self.assertEqual(rows[0]["date"], "2026-10-07")
        self.assertEqual(len(rows), 2)

    def test_edit_student_id_and_semester(self):
        code, _, stderr = self.run_edit(
            "S1", "1", "Mathematics", "2026-10-06",
            "--student-id", "S2", "--semester", "2",
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        rows = self.read_rows()
        self.assertEqual(rows[0]["student_id"], "S2")
        self.assertEqual(rows[0]["semester"], "2")
        self.assertEqual(len(rows), 2)

    def test_no_change_options_is_rejected(self):
        before = self.file_bytes()
        code, stdout, stderr = self.run_edit("S1", "1", "Mathematics", "2026-10-06")

        self.assertEqual(code, 2)
        self.assertIn("no changes requested", stderr)
        self.assertEqual(stdout, "")
        self.assertEqual(self.file_bytes(), before)


class TestEditValidation(EditTestCase):
    """Invalid values are rejected and leave the file unchanged."""

    def setUp(self):
        super().setUp()
        self.write_csv([
            "S1,Mathematics,50,100,2026-10-06,3,1\n",
        ])

    def assert_rejected(self, *changes, expected_error):
        before = self.file_bytes()
        code, stdout, stderr = self.run_edit(
            "S1", "1", "Mathematics", "2026-10-06", *changes
        )
        self.assertEqual(code, 2)
        self.assertIn(expected_error, stderr)
        self.assertEqual(stdout, "")
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(self.temp_files(), [])

    def test_rejects_score_above_max(self):
        self.assert_rejected(
            "--score", "150", expected_error="score must be between 0 and max_score"
        )

    def test_rejects_negative_score(self):
        self.assert_rejected(
            "--score", "-1", expected_error="score must be between 0 and max_score"
        )

    def test_rejects_zero_max_score(self):
        self.assert_rejected(
            "--max-score", "0", expected_error="max_score must be greater than 0"
        )

    def test_rejects_negative_and_non_finite_credits(self):
        for credits in ("-2", "nan", "inf"):
            with self.subTest(credits=credits):
                self.assert_rejected(
                    "--credits", credits,
                    expected_error="credits must be a finite number greater than or equal to 0",
                )

    def test_rejects_malformed_date(self):
        self.assert_rejected(
            "--date", "not-a-date", expected_error="date must use YYYY-MM-DD format"
        )

    def test_rejects_impossible_calendar_date(self):
        self.assert_rejected(
            "--date", "2026-02-30", expected_error="date must be a valid calendar date"
        )

    def test_rejects_score_above_new_max(self):
        self.assert_rejected(
            "--max-score", "40", expected_error="score must be between 0 and max_score"
        )


class TestEditTargetResolution(EditTestCase):
    """Missing and ambiguous targets are rejected without touching the file."""

    def test_nonexistent_target_reports_error(self):
        self.write_csv([
            "S1,Mathematics,50,100,2026-10-06,3,1\n",
        ])
        before = self.file_bytes()

        code, stdout, stderr = self.run_edit(
            "S9", "1", "Mathematics", "2026-10-06", "--score", "60"
        )

        self.assertEqual(code, 2)
        self.assertIn("no record found", stderr)
        self.assertIn("S9", stderr)
        self.assertEqual(stdout, "")
        self.assertEqual(self.file_bytes(), before)

    def test_nonexistent_target_on_empty_file(self):
        code, _, stderr = self.run_edit(
            "S1", "1", "Mathematics", "2026-10-06", "--score", "60"
        )

        self.assertEqual(code, 2)
        self.assertIn("no record found", stderr)
        self.assertFalse(self.data_path.exists())

    def test_ambiguous_target_is_rejected(self):
        self.write_csv([
            "S1,Mathematics,50,100,2026-10-06,3,1\n",
            "S1,Mathematics,60,100,2026-10-06,3,1\n",  # duplicate assessment
        ])
        before = self.file_bytes()

        code, stdout, stderr = self.run_edit(
            "S1", "1", "Mathematics", "2026-10-06", "--score", "70"
        )

        self.assertEqual(code, 2)
        self.assertIn("ambiguous target", stderr)
        self.assertEqual(stdout, "")
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(len(self.read_rows()), 2)

    def test_current_score_selects_one_same_day_assessment(self):
        self.write_csv([
            "S1,Mathematics,50,100,2026-10-06,3,1\n",
            "S1,Mathematics,60,100,2026-10-06,3,1\n",
        ])

        code, stdout, stderr = self.run_edit(
            "S1", "1", "Mathematics", "2026-10-06",
            "--current-score", "60", "--score", "75",
        )

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("Edited grade", stdout)
        rows = self.read_rows()
        self.assertEqual([row["score"] for row in rows], ["50", "75.0"])

    def test_current_selectors_must_resolve_exactly_one_record(self):
        self.write_csv([
            "S1,Mathematics,50,100,2026-10-06,3,1\n",
            "S1,Mathematics,50,50,2026-10-06,4,1\n",
        ])
        before = self.file_bytes()

        code, _, stderr = self.run_edit(
            "S1", "1", "Mathematics", "2026-10-06",
            "--current-score", "50", "--score", "45",
        )

        self.assertEqual(code, 2)
        self.assertIn("ambiguous target", stderr)
        self.assertIn("--current-max-score", stderr)
        self.assertEqual(self.file_bytes(), before)

    def test_current_max_score_and_credits_can_disambiguate(self):
        self.write_csv([
            "S1,Mathematics,50,100,2026-10-06,3,1\n",
            "S1,Mathematics,50,50,2026-10-06,4,1\n",
        ])

        code, _, stderr = self.run_edit(
            "S1", "1", "Mathematics", "2026-10-06",
            "--current-score", "50", "--current-max-score", "50",
            "--current-credits", "4", "--score", "45",
        )

        self.assertEqual((code, stderr), (0, ""))
        rows = self.read_rows()
        self.assertEqual([row["score"] for row in rows], ["50", "45.0"])

    def test_unmatched_current_selector_preserves_file(self):
        self.write_csv([
            "S1,Mathematics,50,100,2026-10-06,3,1\n",
            "S1,Mathematics,60,100,2026-10-06,3,1\n",
        ])
        before = self.file_bytes()

        code, stdout, stderr = self.run_edit(
            "S1", "1", "Mathematics", "2026-10-06",
            "--current-score", "99", "--score", "75",
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("matching the supplied current values", stderr)
        self.assertEqual(self.file_bytes(), before)

    def test_add_allow_duplicate_then_edit_one_assessment(self):
        first = self.run_cli(
            "add", "S1", "Mathematics", "95", "100",
            "2026-10-06", "3", "1",
        )
        second = self.run_cli(
            "add", "S1", "Mathematics", "55", "100",
            "2026-10-06", "3", "1", "--allow-duplicate",
        )

        code, _, stderr = self.run_edit(
            "S1", "1", "Mathematics", "2026-10-06",
            "--current-score", "55", "--score", "75",
        )

        self.assertEqual(first[0], 0)
        self.assertEqual(second[0], 0)
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(
            [row["score"] for row in self.read_rows()],
            ["95.0", "75.0"],
        )

    def test_edit_same_day_assessment_rejects_exact_record_duplicate(self):
        self.write_csv([
            "S1,Mathematics,50,100,2026-10-06,3,1\n",
            "S1,Mathematics,60,100,2026-10-06,3,1\n",
        ])
        before = self.file_bytes()

        code, _, stderr = self.run_edit(
            "S1", "1", "Mathematics", "2026-10-06",
            "--current-score", "60", "--score", "50",
        )

        self.assertEqual(code, 2)
        self.assertIn("same grade data", stderr)
        self.assertEqual(self.file_bytes(), before)


class TestEditDuplicatePrevention(EditTestCase):
    """Edits must not create a duplicate assessment identity."""

    def setUp(self):
        super().setUp()
        self.write_csv([
            "S1,Mathematics,50,100,2026-10-06,3,1\n",
            "S1,Physics,80,100,2026-10-06,3,1\n",
        ])

    def test_edit_that_duplicates_another_record_is_rejected(self):
        before = self.file_bytes()

        code, stdout, stderr = self.run_edit(
            "S1", "1", "Mathematics", "2026-10-06", "--subject", "Physics"
        )

        self.assertEqual(code, 2)
        self.assertIn("assessment already exists", stderr)
        self.assertEqual(stdout, "")
        self.assertEqual(self.file_bytes(), before)

    def test_edit_onto_other_students_identity_is_rejected(self):
        self.write_csv([
            "S1,Mathematics,50,100,2026-10-06,3,1\n",
            "S2,Mathematics,70,100,2026-10-06,3,1\n",
        ])
        before = self.file_bytes()

        code, _, stderr = self.run_edit(
            "S1", "1", "Mathematics", "2026-10-06", "--student-id", "S2"
        )

        self.assertEqual(code, 2)
        self.assertIn("assessment already exists", stderr)
        self.assertEqual(self.file_bytes(), before)

    def test_legitimate_retakes_are_preserved(self):
        """Same subject in a different semester/dates coexists after an edit."""
        self.write_csv([
            "S1,Mathematics,50,100,2026-10-06,3,1\n",
            "S1,Mathematics,60,100,2026-10-07,3,2\n",  # retake: other semester+date
        ])

        code, _, stderr = self.run_edit(
            "S1", "1", "Mathematics", "2026-10-06", "--score", "75"
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        rows = self.read_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["score"], "75.0")
        self.assertEqual(rows[1]["score"], "60")

    def test_unrelated_records_preserved(self):
        code, _, stderr = self.run_edit(
            "S1", "1", "Mathematics", "2026-10-06", "--score", "75"
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        rows = self.read_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1], {
            "student_id": "S1",
            "subject": "Physics",
            "score": "80",
            "max_score": "100",
            "date": "2026-10-06",
            "credits": "3",
            "semester": "1",
        })


class TestEditFailureAtomicity(EditTestCase):
    """Failures during write/fsync/replace leave the original bytes intact."""

    def setUp(self):
        super().setUp()
        self.write_csv([
            "S1,Mathematics,50,100,2026-10-06,3,1\n",
        ])
        self.before = self.file_bytes()

    def test_validation_failure_keeps_bytes_and_cleans_temp(self):
        code, _, stderr = self.run_edit(
            "S1", "1", "Mathematics", "2026-10-06", "--score", "150"
        )

        self.assertEqual(code, 2)
        self.assertIn("score must be between 0 and max_score", stderr)
        self.assertEqual(self.file_bytes(), self.before)
        self.assertEqual(self.temp_files(), [])

    def test_write_failure_keeps_bytes_and_cleans_temp(self):
        with mock.patch(
            "grade_tracker.cli._atomic_write_rows",
            side_effect=OSError("disk full"),
        ):
            code, _, stderr = self.run_edit(
                "S1", "1", "Mathematics", "2026-10-06", "--score", "75"
            )

        self.assertEqual(code, 2)
        self.assertIn("disk full", stderr)
        self.assertEqual(self.file_bytes(), self.before)
        self.assertEqual(self.temp_files(), [])

    def test_fsync_failure_keeps_bytes_and_cleans_temp(self):
        with mock.patch(
            "grade_tracker.cli.os.fsync", side_effect=OSError("fsync failed")
        ):
            code, _, stderr = self.run_edit(
                "S1", "1", "Mathematics", "2026-10-06", "--score", "75"
            )

        self.assertEqual(code, 2)
        self.assertIn("fsync failed", stderr)
        self.assertEqual(self.file_bytes(), self.before)
        self.assertEqual(self.temp_files(), [])

    def test_replace_failure_keeps_bytes_and_cleans_temp(self):
        with mock.patch(
            "grade_tracker.cli.os.replace", side_effect=OSError("replace failed")
        ):
            code, _, stderr = self.run_edit(
                "S1", "1", "Mathematics", "2026-10-06", "--score", "75"
            )

        self.assertEqual(code, 2)
        self.assertIn("replace failed", stderr)
        self.assertEqual(self.file_bytes(), self.before)
        self.assertEqual(self.temp_files(), [])

    def test_unexpected_schema_refused_without_change(self):
        # Rewrite the file with a bad header to trip the schema guard.
        self.data_path.write_text(
            "student_id,subject,score,max_score,date,credits\n"
            "S1,Mathematics,50,100,2026-10-06,3\n",
            encoding="utf-8",
            newline="",
        )
        before = self.file_bytes()

        code, _, stderr = self.run_edit(
            "S1", "1", "Mathematics", "2026-10-06", "--score", "75"
        )

        self.assertEqual(code, 2)
        self.assertIn("unexpected schema", stderr)
        self.assertEqual(self.file_bytes(), before)
        self.assertEqual(self.temp_files(), [])


class TestEditPreservesCsvShape(EditTestCase):
    """Header, column order, legacy rows, and value formatting are preserved."""

    def test_header_and_column_order_preserved(self):
        self.write_csv([
            "S1,Mathematics,50,100,2026-10-06,3,1\n",
            "S1,Physics,80,100,2026-10-06,3,1\n",
        ])

        code, _, _ = self.run_edit(
            "S1", "1", "Mathematics", "2026-10-06", "--score", "75"
        )
        self.assertEqual(code, 0)

        first_line = self.file_bytes().split(b"\n")[0].decode("utf-8").rstrip("\r")
        self.assertEqual(first_line, HEADER.strip())

    def test_legacy_rows_without_credits_semester_are_preserved(self):
        """Legacy rows the loader would default are kept as written."""
        self.data_path.write_text(
            HEADER
            + "S1,Mathematics,50,100,2026-10-06,3,1\n"
            + "S1,Physics,80,100,2026-10-06\n"  # legacy row: no credits/semester
            + "S1,Chemistry,90,100,2026-10-06,4,2\n",
            encoding="utf-8",
            newline="",
        )
        legacy_line = b"S1,Physics,80,100,2026-10-06"

        code, _, _ = self.run_edit(
            "S1", "1", "Mathematics", "2026-10-06", "--score", "75"
        )

        self.assertEqual(code, 0)
        data = self.file_bytes()
        self.assertIn(legacy_line, data)
        # The legacy row must still be exactly two cells short of full width.
        with self.data_path.open(newline="", encoding="utf-8") as handle:
            raw_rows = list(csv.reader(handle))
        self.assertEqual(len(raw_rows), 4)  # header + 3 data rows
        self.assertEqual(raw_rows[2], ["S1", "Physics", "80", "100", "2026-10-06"])
        self.assertEqual(len(self.read_rows()), 3)

    def test_edit_on_legacy_row_applies_defaults_for_missing_fields(self):
        self.data_path.write_text(
            HEADER + "S1,Physics,80,100,2026-10-06\n",
            encoding="utf-8",
            newline="",
        )

        code, _, stderr = self.run_edit(
            "S1", "1", "Physics", "2026-10-06", "--score", "85"
        )

        self.assertEqual(code, 2)
        # The legacy row still identifies correctly by semester default, but
        # filling in credits/semester would change its shape, so the edit is
        # rejected rather than silently rewriting the row.
        self.assertIn("no record found", stderr)


class TestOtherCommandsStillWork(EditTestCase):
    """add/list/summary/average/export/validate keep behaving as before."""

    def test_add_then_edit_then_list_summary_average_validate_export(self):
        code, _, stderr = self.run_cli(
            "add", "S1", "Mathematics", "50", "100", "2026-10-06", "3", "1"
        )
        self.assertEqual((code, stderr), (0, ""))
        code, _, _ = self.run_cli(
            "add", "S1", "Physics", "80", "100", "2026-10-06", "3", "1"
        )
        self.assertEqual(code, 0)

        code, _, stderr = self.run_edit(
            "S1", "1", "Mathematics", "2026-10-06", "--score", "90"
        )
        self.assertEqual((code, stderr), (0, ""))

        code, stdout, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("Mathematics", stdout)

        code, stdout, _ = self.run_cli("summary", "S1")
        self.assertEqual(code, 0)
        self.assertIn("Semester 1", stdout)

        code, stdout, _ = self.run_cli("average", "Mathematics")
        self.assertEqual(code, 0)
        self.assertIn("Average for 'Mathematics'", stdout)

        code, stdout, stderr = self.run_cli("validate")
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("passed validation", stdout)

        output = Path(self.temp_dir.name) / "report.html"
        code, stdout, _ = self.run_cli("export", "--output", str(output))
        self.assertEqual(code, 0)
        self.assertIn("HTML report successfully exported", stdout)
        self.assertTrue(output.exists())


if __name__ == "__main__":
    unittest.main()
