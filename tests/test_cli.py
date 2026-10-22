
"""Regression tests for add-command validation and persistence."""

import contextlib
import csv
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from grade_tracker.cli import main


class TestAddValidation(unittest.TestCase):
    """Exercise the public CLI before any grade row reaches the data file."""

    def run_add(self, data_path, *args):
        argv = ["grade_tracker", "--data", str(data_path), "add", *args]
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

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_path = Path(self.temp_dir.name) / "grades.csv"

    def read_rows(self):
        if not self.data_path.exists():
            return []

        with self.data_path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def assert_rejected(self, *args, expected_error):
        code, stdout, stderr = self.run_add(self.data_path, *args)

        self.assertEqual(code, 2)
        self.assertIn(expected_error, stderr)
        self.assertNotIn("Added grade", stdout)
        self.assertEqual(self.read_rows(), [])

    def test_rejects_zero_max_score_without_writing(self):
        self.assert_rejected(
            "S1", "Mathematics", "50", "0", "not-a-date", "3", "1",
            expected_error="max_score must be greater than 0",
        )

    def test_rejects_score_above_max_without_writing(self):
        self.assert_rejected(
            "S1", "Mathematics", "101", "100", "2026-10-05", "3", "1",
            expected_error="score must be between 0 and max_score",
        )

    def test_rejects_negative_score_without_writing(self):
        self.assert_rejected(
            "S1", "Mathematics", "-1", "100", "2026-10-05", "3", "1",
            expected_error="score must be between 0 and max_score",
        )

    def test_rejects_negative_credits_without_writing(self):
        self.assert_rejected(
            "S1", "Mathematics", "50", "100", "2026-10-05", "-2", "1",
            expected_error="credits must be a finite number greater than or equal to 0",
        )

    def test_rejects_non_finite_credits_without_writing(self):
        self.assert_rejected(
            "S1", "Mathematics", "50", "100", "2026-10-05", "nan", "1",
            expected_error="credits must be a finite number greater than or equal to 0",
        )

    def test_rejects_malformed_date_without_writing(self):
        self.assert_rejected(
            "S1", "Mathematics", "50", "100", "not-a-date", "3", "1",
            expected_error="date must use YYYY-MM-DD format",
        )

    def test_rejects_impossible_calendar_date_without_writing(self):
        self.assert_rejected(
            "S1", "Mathematics", "50", "100", "2026-02-30", "3", "1",
            expected_error="date must be a valid calendar date",
        )

    def test_accepts_zero_score_boundary(self):
        code, stdout, stderr = self.run_add(
            self.data_path,
            "S1", "Mathematics", "0", "100", "2026-10-05", "3", "1",
        )

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("Added grade for S1", stdout)
        self.assertEqual(
            self.read_rows(),
            [{
                "student_id": "S1",
                "subject": "Mathematics",
                "score": "0.0",
                "max_score": "100.0",
                "date": "2026-10-05",
                "credits": "3.0",
                "semester": "1",
            }],
        )

    def test_accepts_zero_credits_boundary(self):
        code, stdout, stderr = self.run_add(
            self.data_path,
            "S1", "Mathematics", "50", "100", "2026-10-05", "0", "1",
        )

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("Added grade for S1", stdout)
        self.assertEqual(self.read_rows()[0]["credits"], "0.0")

    def test_save_grade_rejects_negative_credits_without_writing(self):
        from grade_tracker.cli import save_grade

        with self.assertRaisesRegex(ValueError, "credits must be"):
            save_grade(
                "S1", "Mathematics", 50, 100, "2026-10-05", -2, "1",
                self.data_path,
            )

        self.assertEqual(self.read_rows(), [])

    def test_accepts_score_equal_to_max_boundary(self):
        code, stdout, stderr = self.run_add(
            self.data_path,
            "S1", "Mathematics", "100", "100", "2026-10-05", "3", "1",
        )

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("Added grade for S1", stdout)
        self.assertEqual(len(self.read_rows()), 1)

    def test_accepts_normal_valid_input(self):
        code, stdout, stderr = self.run_add(
            self.data_path,
            "S1", "Mathematics", "50", "100", "2026-10-05", "3", "1",
        )

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("Added grade for S1", stdout)
        rows = self.read_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["score"], "50.0")


class TestDuplicateDetection(unittest.TestCase):
    """Reject duplicate assessments while permitting distinct assessments."""

    def run_add(self, data_path, *args):
        argv = ["grade_tracker", "--data", str(data_path), "add", *args]
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

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_path = Path(self.temp_dir.name) / "grades.csv"

    def read_rows(self):
        if not self.data_path.exists():
            return []

        with self.data_path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def test_duplicate_assessment_is_rejected(self):
        code, stdout, stderr = self.run_add(
            self.data_path,
            "S1", "Mathematics", "25", "50", "2026-10-06", "3", "1",
        )

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("Added grade for S1", stdout)
        self.assertEqual(len(self.read_rows()), 1)

        code, stdout, stderr = self.run_add(
            self.data_path,
            "S1", "Mathematics", "25", "50", "2026-10-06", "3", "1",
        )

        self.assertEqual(code, 2)
        self.assertIn("assessment already exists", stderr)
        self.assertNotIn("Added grade", stdout)
        self.assertEqual(len(self.read_rows()), 1)

    def test_duplicate_does_not_modify_csv(self):
        self.run_add(
            self.data_path,
            "S1", "Mathematics", "25", "50", "2026-10-06", "3", "1",
        )
        self.run_add(
            self.data_path,
            "S1", "Physics", "85", "100", "2026-10-06", "3", "1",
        )
        self.assertEqual(len(self.read_rows()), 2)

        code, stdout, stderr = self.run_add(
            self.data_path,
            "S1", "Mathematics", "25", "50", "2026-10-06", "3", "1",
        )

        self.assertEqual(code, 2)
        self.assertEqual(len(self.read_rows()), 2)

    def test_corrected_score_same_assessment_is_rejected(self):
        self.run_add(
            self.data_path,
            "S1", "Mathematics", "25", "50", "2026-10-06", "3", "1",
        )

        code, stdout, stderr = self.run_add(
            self.data_path,
            "S1", "Mathematics", "30", "50", "2026-10-06", "3", "1",
        )

        self.assertEqual(code, 2)
        self.assertIn("assessment already exists", stderr)
        rows = self.read_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["score"], "25.0")

    def test_explicit_same_day_assessment_is_accepted(self):
        self.run_add(
            self.data_path,
            "S1", "Mathematics", "95", "100", "2026-10-06", "3", "1",
        )

        code, stdout, stderr = self.run_add(
            self.data_path,
            "S1", "Mathematics", "55", "100", "2026-10-06", "3", "1",
            "--allow-duplicate",
        )

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("Added grade", stdout)
        self.assertEqual(len(self.read_rows()), 2)

    def test_allow_duplicate_still_rejects_exact_repeated_row(self):
        args = (
            "S1", "Mathematics", "95", "100", "2026-10-06", "3", "1",
        )
        self.run_add(self.data_path, *args)

        code, stdout, stderr = self.run_add(
            self.data_path, *args, "--allow-duplicate"
        )

        self.assertEqual(code, 2)
        self.assertIn("assessment already exists with the same grade data", stderr)
        self.assertNotIn("Added grade", stdout)
        self.assertEqual(len(self.read_rows()), 1)

    def test_different_subjects_same_date_are_both_accepted(self):
        code, _, stderr = self.run_add(
            self.data_path,
            "S1", "Mathematics", "25", "50", "2026-10-06", "3", "1",
        )
        self.assertEqual((code, stderr), (0, ""))

        code, _, stderr = self.run_add(
            self.data_path,
            "S1", "Physics", "85", "100", "2026-10-06", "3", "1",
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(len(self.read_rows()), 2)

    def test_different_semesters_same_subject_date_are_both_accepted(self):
        code, _, stderr = self.run_add(
            self.data_path,
            "S1", "Mathematics", "25", "50", "2026-10-06", "3", "1",
        )
        self.assertEqual((code, stderr), (0, ""))

        code, _, stderr = self.run_add(
            self.data_path,
            "S1", "Mathematics", "25", "50", "2026-10-06", "3", "2",
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(len(self.read_rows()), 2)

    def test_different_students_same_assessment_are_both_accepted(self):
        code, _, stderr = self.run_add(
            self.data_path,
            "S1", "Mathematics", "25", "50", "2026-10-06", "3", "1",
        )
        self.assertEqual((code, stderr), (0, ""))

        code, _, stderr = self.run_add(
            self.data_path,
            "S2", "Mathematics", "25", "50", "2026-10-06", "3", "1",
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(len(self.read_rows()), 2)

    def test_same_score_different_assessments_are_both_accepted(self):
        code, _, stderr = self.run_add(
            self.data_path,
            "S1", "Mathematics", "50", "100", "2026-10-06", "3", "1",
        )
        self.assertEqual((code, stderr), (0, ""))

        code, _, stderr = self.run_add(
            self.data_path,
            "S1", "Physics", "50", "100", "2026-10-07", "3", "1",
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(len(self.read_rows()), 2)

    def test_save_grade_raises_valueerror_for_duplicate(self):
        from grade_tracker.cli import save_grade

        save_grade(
            "S1", "Mathematics", 25, 50, "2026-10-06", 3, "1",
            self.data_path,
        )

        with self.assertRaises(ValueError):
            save_grade(
                "S1", "Mathematics", 25, 50, "2026-10-06", 3, "1",
                self.data_path,
            )

        self.assertEqual(len(self.read_rows()), 1)


class TestDuplicateRecordsLoadedFromCsv(unittest.TestCase):
    """SGPA/CGPA must remain correct when CSV files contain duplicates."""

    HEADER = "student_id,subject,score,max_score,date,credits,semester\n"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_path = Path(self.temp_dir.name) / "grades.csv"

    def write_csv(self, rows):
        with self.data_path.open("w", newline="", encoding="utf-8") as handle:
            handle.write(self.HEADER)
            handle.writelines(rows)

    def run_summary(self, student_id):
        argv = [
            "grade_tracker", "--data", str(self.data_path),
            "summary", student_id,
        ]
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

    def test_summary_reports_deduplicated_sgpa_and_cgpa(self):
        self.write_csv([
            "S1,Mathematics,95,100,2025-01-15,3,1\n",
            "S1,Mathematics,70,100,2025-01-15,3,1\n",
            "S1,Physics,85,100,2025-01-16,3,1\n",
            "S1,Mathematics,85,100,2025-06-15,3,2\n",
        ])

        code, stdout, stderr = self.run_summary("S1")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Semester 1 (SGPA: 9.0)", stdout)
        self.assertIn("Semester 2 (SGPA: 9.0)", stdout)
        self.assertIn("CGPA: 9.0", stdout)

    def test_summary_preserves_retake_in_both_semesters(self):
        self.write_csv([
            "S1,Mathematics,95,100,2025-01-15,3,1\n",
            "S1,Mathematics,75,100,2025-06-15,3,2\n",
        ])

        code, stdout, stderr = self.run_summary("S1")

        self.assertEqual(code, 0)
        self.assertIn("Semester 1 (SGPA: 10.0)", stdout)
        self.assertIn("Semester 2 (SGPA: 8.0)", stdout)
        self.assertIn("CGPA: 9.0", stdout)

    def test_persistence_and_calculation_keys_are_identical(self):
        from grade_tracker.cli import assessment_key as cli_key
        from grade_tracker.calc import assessment_key as calc_key

        self.assertEqual(
            cli_key("S1", "1", "Mathematics", "2026-10-06"),
            calc_key({
                "student_id": "S1",
                "semester": "1",
                "subject": "Mathematics",
                "date": "2026-10-06",
            }),
        )

    def test_same_day_different_scores_are_both_in_summary(self):
        self.write_csv([
            "S1,Mathematics,70,100,2025-01-15,3,1\n",
            "S1,Mathematics,95,100,2025-01-15,3,1\n",
            "S1,Physics,85,100,2025-01-16,3,1\n",
        ])

        code, stdout, stderr = self.run_summary("S1")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Semester 1 (SGPA: 9.0)", stdout)


class TestSummaryOutputFormat(unittest.TestCase):
    """Test summary percentage, SGPA, and CGPA formatting."""

    HEADER = "student_id,subject,score,max_score,date,credits,semester\n"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_path = Path(self.temp_dir.name) / "grades.csv"

    def write_csv(self, rows):
        with self.data_path.open("w", newline="", encoding="utf-8") as handle:
            handle.write(self.HEADER)
            handle.writelines(rows)

    def run_summary(self, student_id, *args):
        argv = [
            "grade_tracker", "--data", str(self.data_path),
            "summary", student_id, *args,
        ]
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

    def test_percentage_display_rounds_to_one_decimal(self):
        """Display rounds to 1 decimal without changing the assigned grade."""
        self.write_csv([
            "S1,Mathematics,89.95,100,2025-01-15,3,1\n",
        ])

        code, stdout, stderr = self.run_summary("S1")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("(90.0%) -> B", stdout)

    def test_summary_sgpa_and_cgpa_format_unchanged(self):
        self.write_csv([
            "S1,Mathematics,95,100,2025-01-15,3,1\n",
            "S1,Physics,85,100,2025-01-16,3,1\n",
        ])

        code, stdout, stderr = self.run_summary("S1")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Semester 1 (SGPA: 9.5)", stdout)
        self.assertIn("CGPA: 9.5", stdout)


class TestJsonOutput(unittest.TestCase):
    """Machine-readable ``--format json`` contracts for read-only commands."""

    HEADER = "student_id,subject,score,max_score,date,credits,semester\n"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_path = Path(self.temp_dir.name) / "grades.csv"

    def write_csv(self, rows, header=None):
        self.data_path.write_text(
            (self.HEADER if header is None else header) + "".join(rows),
            encoding="utf-8",
            newline="",
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

    def parse_json_stdout(self, stdout):
        self.assertTrue(stdout.endswith("\n"))
        return json.loads(stdout)

    def test_list_json_schema_and_values(self):
        self.write_csv([
            "S1,Mathematics,95,100,2025-01-15,3,1\n",
            "S1,Physics,85,100,2025-01-16,3,1\n",
        ])

        code, stdout, stderr = self.run_cli("list", "--format", "json")
        payload = self.parse_json_stdout(stdout)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(payload["command"], "list")
        self.assertEqual(payload["count"], 2)
        self.assertEqual(len(payload["grades"]), 2)
        first = payload["grades"][0]
        self.assertEqual(
            first,
            {
                "student_id": "S1",
                "subject": "Mathematics",
                "score": 95.0,
                "max_score": 100.0,
                "date": "2025-01-15",
                "credits": 3.0,
                "semester": "1",
                "percentage": 95.0,
                "letter_grade": "A",
            },
        )
        self.assertIsInstance(first["score"], float)
        self.assertIsInstance(first["semester"], str)

    def test_list_json_empty_dataset(self):
        code, stdout, stderr = self.run_cli("list", "--format", "json")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(self.parse_json_stdout(stdout), {
            "command": "list",
            "count": 0,
            "grades": [],
            "policy": "legacy@1",
        })

    def test_summary_json_schema_sgpa_and_cgpa(self):
        self.write_csv([
            "S1,Mathematics,95,100,2025-01-15,3,1\n",
            "S1,Physics,85,100,2025-01-16,3,1\n",
            "S1,History,85,100,2025-10-24,4,2\n",
        ])

        code, stdout, stderr = self.run_cli("summary", "S1", "--format", "json")
        payload = self.parse_json_stdout(stdout)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(payload["command"], "summary")
        self.assertEqual(payload["student_id"], "S1")
        self.assertEqual(payload["count"], 3)
        self.assertEqual(payload["cgpa"], 9.3)
        self.assertEqual(len(payload["semesters"]), 2)
        self.assertEqual(payload["semesters"][0]["semester"], "1")
        self.assertEqual(payload["semesters"][0]["sgpa"], 9.5)
        self.assertEqual(len(payload["semesters"][0]["grades"]), 2)
        self.assertEqual(payload["semesters"][1]["semester"], "2")
        self.assertEqual(payload["semesters"][1]["sgpa"], 9.0)
        self.assertEqual(len(payload["grades"]), 3)

    def test_summary_json_empty_student_uses_null_and_empty_arrays(self):
        self.write_csv(["S1,Mathematics,95,100,2025-01-15,3,1\n"])

        code, stdout, stderr = self.run_cli("summary", "UNKNOWN", "--format", "json")
        payload = self.parse_json_stdout(stdout)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(payload, {
            "command": "summary",
            "student_id": "UNKNOWN",
            "count": 0,
            "cgpa": None,
            "semesters": [],
            "grades": [],
            "policy": "legacy@1",
        })

    def test_average_json_schema_and_null_for_no_matches(self):
        self.write_csv([
            "S1,Mathematics,95,100,2025-01-15,3,1\n",
            "S2,Mathematics,70,100,2025-01-16,3,1\n",
        ])

        code, stdout, stderr = self.run_cli("average", "Mathematics", "--format", "json")
        payload = self.parse_json_stdout(stdout)

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(payload, {
            "command": "average",
            "subject": "Mathematics",
            "average": 82.5,
            "count": 2,
        })
        self.assertIsInstance(payload["average"], float)

        code, stdout, stderr = self.run_cli("average", "Physics", "--format", "json")
        payload = self.parse_json_stdout(stdout)

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(payload, {
            "command": "average",
            "subject": "Physics",
            "average": None,
            "count": 0,
        })

    def test_validate_json_success_and_failure(self):
        self.write_csv(["S1,Mathematics,95,100,2025-01-15,3,1\n"])

        code, stdout, stderr = self.run_cli("validate", "--format", "json")
        payload = self.parse_json_stdout(stdout)

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(payload["command"], "validate")
        self.assertIs(payload["valid"], True)
        self.assertEqual(payload["error_count"], 0)
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["file"], str(self.data_path))

        self.write_csv(["S1,Mathematics,abc,100,2025-01-15,3,1\n"])

        code, stdout, stderr = self.run_cli("validate", "--format", "json")
        payload = self.parse_json_stdout(stdout)

        self.assertEqual((code, stderr), (1, ""))
        self.assertIs(payload["valid"], False)
        self.assertEqual(payload["error_count"], 1)
        self.assertEqual(payload["errors"], [
            "row 2, field score: 'abc' is not a numeric score",
        ])

    def test_malformed_csv_json_reports_error_not_empty_success(self):
        self.data_path.write_text(
            self.HEADER + "S1,Mathematics,abc,100,2025-01-15,3,1\n",
            encoding="utf-8",
            newline="",
        )

        code, stdout, stderr = self.run_cli("list", "--format", "json")
        payload = self.parse_json_stdout(stdout)

        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        self.assertEqual(payload["command"], "list")
        self.assertEqual(payload["error"]["code"], "input_error")
        self.assertIn("row 2 cannot be loaded", payload["error"]["message"])
        self.assertNotIn("grades", payload)

    def test_utf8_bom_header_is_normalized(self):
        self.data_path.write_bytes(
            (
                "\ufeff" + self.HEADER + "S1,Mathematics,95,100,2025-01-15,3,1\n"
            ).encode("utf-8")
        )

        code, stdout, stderr = self.run_cli("list", "--format", "json")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        payload = self.parse_json_stdout(stdout)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["grades"][0]["subject"], "Mathematics")

    def test_missing_required_columns_json_reports_error(self):
        self.data_path.write_text(
            "student_id,subject,score,date,credits,semester\n"
            "S1,Mathematics,95,2025-01-15,3,1\n",
            encoding="utf-8",
            newline="",
        )

        code, stdout, stderr = self.run_cli(
            "average", "Mathematics", "--format", "json"
        )
        payload = self.parse_json_stdout(stdout)

        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        self.assertEqual(payload["error"]["code"], "input_error")
        self.assertIn(
            "missing required column(s): max_score", payload["error"]["message"]
        )

    def test_default_human_output_is_unchanged(self):
        self.write_csv(["S1,Mathematics,95,100,2025-01-15,3,1\n"])

        code, stdout, stderr = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Student ID", stdout)
        self.assertIn("Mathematics", stdout)

        code, stdout, stderr = self.run_cli("summary", "S1")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("CGPA: 10.0", stdout)

    def test_invalid_format_is_rejected_by_argparse(self):
        self.write_csv(["S1,Mathematics,95,100,2025-01-15,3,1\n"])

        code, stdout, stderr = self.run_cli("list", "--format", "yaml")

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("invalid choice", stderr)

    def test_json_stdout_contains_only_json(self):
        self.write_csv(["S1,Mathematics,95,100,2025-01-15,3,1\n"])

        for command in (
            ("list",),
            ("summary", "S1"),
            ("average", "Mathematics"),
            ("validate",),
        ):
            with self.subTest(command=command):
                code, stdout, stderr = self.run_cli(*command, "--format", "json")
                self.assertEqual(code, 0)
                self.assertEqual(stderr, "")
                self.assertEqual(stdout.count("\n"), 1)
                json.loads(stdout)

    def test_subprocess_entrypoint_json_contract(self):
        self.write_csv(["S1,Mathematics,95,100,2025-01-15,3,1\n"])

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "grade_tracker",
                "--data",
                str(self.data_path),
                "list",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["grades"][0]["subject"], "Mathematics")


class TestCmdList(unittest.TestCase):
    """Test listing grade records via cmd_list."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_path = Path(self.temp_dir.name) / "grades.csv"

    def run_list(self):
        argv = ["grade_tracker", "--data", str(self.data_path), "list"]
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

    def test_list_multiple_records(self):
        from grade_tracker.cli import save_grade

        save_grade(
            "S1", "Mathematics", 90.0, 100.0,
            "2026-10-01", 3.0, "1", csv_path=self.data_path,
        )
        save_grade(
            "S1", "Physics", 80.0, 100.0,
            "2026-10-02", 3.0, "1", csv_path=self.data_path,
        )

        code, stdout, stderr = self.run_list()

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Student ID", stdout)
        self.assertIn("Mathematics", stdout)
        self.assertIn("Physics", stdout)
        self.assertIn("S1", stdout)

    def test_list_empty(self):
        code, stdout, stderr = self.run_list()

        self.assertEqual(code, 0)
        self.assertIn("No grades found.", stdout)


class TestCmdExplain(unittest.TestCase):
    """Test suite for the explain subcommand in both human and JSON modes."""

    HEADER = "student_id,subject,score,max_score,date,credits,semester\n"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_path = Path(self.temp_dir.name) / "grades.csv"

    def write_csv(self, rows, header=None):
        text = (self.HEADER if header is None else header) + "".join(rows)
        self.data_path.write_text(text, encoding="utf-8", newline="")

    def run_explain(self, *args):
        argv = ["grade_tracker", "--data", str(self.data_path), "explain", *args]
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

    def test_explain_human_output_and_reconciliation(self):
        self.write_csv([
            "S1,Mathematics,95,100,2025-01-15,3,1\n",
            "S1,Physics,85,100,2025-01-16,3,1\n",
            "S1,History,85,100,2025-02-15,4,2\n",
        ])
        code, stdout, stderr = self.run_explain("S1")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Explanation for Student: S1", stdout)
        self.assertIn("Semester 1:", stdout)
        self.assertIn("Mathematics", stdout)
        self.assertIn("95.0%", stdout)
        self.assertIn("Included", stdout)
        self.assertIn("Calculation: Numerator = 57.00, Denominator = 6.00 -> SGPA = 9.50", stdout)
        self.assertIn("Semester 2:", stdout)
        self.assertIn("Calculation: Numerator = 36.00, Denominator = 4.00 -> SGPA = 9.00", stdout)
        self.assertIn("Overall Cumulative (CGPA):", stdout)
        self.assertIn("Calculation: Numerator = 93.00, Denominator = 10.00 -> CGPA = 9.30", stdout)

    def test_explain_semester_filter_does_not_narrow_cgpa(self):
        self.write_csv([
            "S1,Mathematics,95,100,2025-01-15,3,1\n",
            "S1,History,85,100,2025-02-15,4,2\n",
        ])
        code, stdout, stderr = self.run_explain("S1", "--semester", "1")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Filtered Semester: 1", stdout)
        self.assertIn("Semester 1:", stdout)
        self.assertNotIn("Semester 2:", stdout)
        # CGPA still uses both semesters: (30 + 36) / 7 = 9.43
        self.assertIn("Calculation: Numerator = 66.00, Denominator = 7.00 -> CGPA = 9.43", stdout)

    def test_explain_missing_student(self):
        self.write_csv(["S1,Mathematics,95,100,2025-01-15,3,1\n"])
        code, stdout, stderr = self.run_explain("UNKNOWN")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Error: student 'UNKNOWN' does not exist.", stderr)

    def test_explain_missing_student_subprocess(self):
        import subprocess
        self.write_csv(["S1,Mathematics,95,100,2025-01-15,3,1\n"])
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "grade_tracker",
                "--data",
                str(self.data_path),
                "explain",
                "UNKNOWN",
            ],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Error: student 'UNKNOWN' does not exist.", result.stderr)

    def test_explain_student_with_nonexistent_semester(self):
        self.write_csv(["S1,Mathematics,95,100,2025-01-15,3,1\n"])
        code, stdout, stderr = self.run_explain("S1", "--semester", "99")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("No records found for student 'S1' in semester 99.", stdout)

    def test_explain_deduplication_exclusion_reason(self):
        self.write_csv([
            "S1,Mathematics,95,100,2025-01-15,3,1\n",
            "S1,Mathematics,95,100,2025-01-15,3,1\n",
        ])
        code, stdout, stderr = self.run_explain("S1")
        self.assertEqual(code, 0)
        self.assertIn("Included", stdout)
        self.assertIn("Excluded (Duplicate assessment in calculation scope)", stdout)
        # Only 95 counts: Numerator = 30, Denominator = 3 -> SGPA = 10.00
        self.assertIn("Calculation: Numerator = 30.00, Denominator = 3.00 -> SGPA = 10.00", stdout)

    def test_explain_read_only_preserves_csv(self):
        self.write_csv(["S1,Mathematics,95,100,2025-01-15,3,1\n"])
        original_bytes = self.data_path.read_bytes()
        code, _, _ = self.run_explain("S1")
        self.assertEqual(code, 0)
        self.assertEqual(self.data_path.read_bytes(), original_bytes)

    def test_explain_distinct_assessments_same_date(self):
        self.write_csv([
            "S1,Mathematics,95,100,2025-01-15,3,1\n",
            "S1,Physics,85,100,2025-01-15,3,1\n",
        ])
        code, stdout, stderr = self.run_explain("S1")
        self.assertEqual(code, 0)
        self.assertIn("Mathematics", stdout)
        self.assertIn("Physics", stdout)
        self.assertEqual(stdout.count("Included"), 2)
        self.assertNotIn("Excluded", stdout)

    def test_explain_zero_credits_denominator(self):
        self.write_csv(["S1,Mathematics,95,100,2025-01-15,0,1\n"])
        code, stdout, stderr = self.run_explain("S1")
        self.assertEqual(code, 0)
        self.assertIn("SGPA = 0.00", stdout)
        self.assertIn("CGPA = 0.00", stdout)

    def test_explain_json_mode(self):
        import json
        self.write_csv([
            "S1,Mathematics,95,100,2025-01-15,3,1\n",
            "S1,Mathematics,70,100,2025-01-15,3,1\n",
            "S1,History,85,100,2025-02-15,4,2\n",
        ])
        code, stdout, stderr = self.run_explain("S1", "--format", "json")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        data = json.loads(stdout)
        self.assertEqual(data["command"], "explain")
        self.assertEqual(data["student_id"], "S1")
        self.assertTrue(data["student_exists"])
        self.assertTrue(data["semester_found"])
        self.assertEqual(data["cgpa"]["gpa"], 9.0)
        self.assertEqual(len(data["semesters"]), 2)
        sem1 = data["semesters"][0]
        self.assertEqual(sem1["semester"], "1")
        self.assertEqual(sem1["sgpa"], 9.0)
        self.assertEqual(len(sem1["records"]), 2)
        self.assertTrue(sem1["records"][0]["included"])
        self.assertTrue(sem1["records"][1]["included"])
        self.assertIsNone(sem1["records"][1]["exclusion_reason"])

    def test_explain_json_mode_nonexistent_student(self):
        import json
        self.write_csv(["S1,Mathematics,95,100,2025-01-15,3,1\n"])
        code, stdout, stderr = self.run_explain("MISSING", "--format", "json")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        data = json.loads(stdout)
        self.assertFalse(data["student_exists"])
        self.assertIsNone(data["cgpa"]["gpa"])

    def test_explain_json_mode_nonexistent_semester(self):
        import json
        self.write_csv(["S1,Mathematics,95,100,2025-01-15,3,1\n"])
        code, stdout, stderr = self.run_explain("S1", "--semester", "99", "--format", "json")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        data = json.loads(stdout)
        self.assertTrue(data["student_exists"])
        self.assertFalse(data["semester_found"])
        self.assertEqual(data["semesters"], [])
        self.assertEqual(data["cgpa"]["gpa"], 10.0)

    def test_explain_json_mode_malformed_csv_exits_two(self):
        import json
        self.write_csv(["S1,Mathematics,invalid_score,100,2025-01-15,3,1\n"])
        code, stdout, stderr = self.run_explain("S1", "--format", "json")
        self.assertEqual(code, 2)
        data = json.loads(stdout)
        self.assertEqual(data["command"], "explain")
        self.assertIn("error", data)


if __name__ == "__main__":
    unittest.main()
