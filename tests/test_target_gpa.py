"""Unit tests for target-gpa calculation and CLI command."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from grade_tracker.calc import calculate_target_sgpa


class TestTargetGpaCalculation(unittest.TestCase):

    def setUp(self):
        # Student S1: 40 credits, total points = 300.0, CGPA = 7.50
        # 10 credits of A (10.0 * 10 = 100), 10 credits of B (9.0 * 10 = 90),
        # 10 credits of C (8.0 * 10 = 80), 10 credits of F (3.0 * 10 = 30) -> (100+90+80+30)/40 = 300/40 = 7.5
        self.sample_grades = [
            {"student_id": "S1", "score": 95, "max_score": 100, "credits": 10.0, "semester": 1},  # A (10 pts) -> 100
            {"student_id": "S1", "score": 85, "max_score": 100, "credits": 10.0, "semester": 1},  # B (9 pts) -> 90
            {"student_id": "S1", "score": 75, "max_score": 100, "credits": 10.0, "semester": 2},  # C (8 pts) -> 80
            {"student_id": "S1", "score": 50, "max_score": 100, "credits": 10.0, "semester": 2},  # F (0 pts, but 30 pts for D? Wait, 50% is F, 0 pts. Total = 270)
        ]

    def test_achievable_target_exact_math(self):
        # 4 courses with 10 credits each: A (10), B (9), C (8), D (7)
        # Total points = 100 + 90 + 80 + 70 = 340. Total credits = 40. CGPA = 340/40 = 8.50.
        grades = [
            {"student_id": "S1", "score": 95, "max_score": 100, "credits": 10.0, "semester": 1},
            {"student_id": "S1", "score": 85, "max_score": 100, "credits": 10.0, "semester": 1},
            {"student_id": "S1", "score": 75, "max_score": 100, "credits": 10.0, "semester": 2},
            {"student_id": "S1", "score": 65, "max_score": 100, "credits": 10.0, "semester": 2},
        ]
        # Target CGPA = 8.80 with 20 upcoming credits.
        # Future credits = 60. Target points = 8.80 * 60 = 528.0.
        # Points needed = 528.0 - 340.0 = 188.0.
        # Required SGPA = 188.0 / 20 = 9.40.
        res = calculate_target_sgpa(grades, "S1", target_cgpa=8.80, upcoming_credits=20.0)
        self.assertTrue(res["student_exists"])
        self.assertEqual(res["current_cgpa"], 8.50)
        self.assertEqual(res["current_credits"], 40.0)
        self.assertEqual(res["target_cgpa"], 8.80)
        self.assertEqual(res["upcoming_credits"], 20.0)
        self.assertEqual(res["total_future_credits"], 60.0)
        self.assertTrue(res["is_achievable"])
        self.assertEqual(res["required_sgpa"], 9.40)
        self.assertEqual(res["max_possible_cgpa"], 9.00)  # (340 + 200) / 60 = 540/60 = 9.00

    def test_impossible_target_reports_max_achievable_cgpa(self):
        grades = [
            {"student_id": "S1", "score": 95, "max_score": 100, "credits": 10.0, "semester": 1},
            {"student_id": "S1", "score": 85, "max_score": 100, "credits": 10.0, "semester": 1},
            {"student_id": "S1", "score": 75, "max_score": 100, "credits": 10.0, "semester": 2},
            {"student_id": "S1", "score": 65, "max_score": 100, "credits": 10.0, "semester": 2},
        ]
        # Current CGPA = 8.50 over 40 credits (340 points).
        # Target CGPA = 9.50 over 20 upcoming credits.
        # Future credits = 60. Target points = 9.50 * 60 = 570.
        # Points needed = 570 - 340 = 230.
        # Required SGPA = 230 / 20 = 11.50 (> 10.0) -> Unachievable!
        # Max possible CGPA = (340 + 200) / 60 = 540 / 60 = 9.00.
        res = calculate_target_sgpa(grades, "S1", target_cgpa=9.50, upcoming_credits=20.0)
        self.assertTrue(res["student_exists"])
        self.assertFalse(res["is_achievable"])
        self.assertEqual(res["required_sgpa"], 11.50)
        self.assertEqual(res["max_possible_cgpa"], 9.00)

    def test_target_already_achieved(self):
        grades = [
            {"student_id": "S1", "score": 95, "max_score": 100, "credits": 40.0, "semester": 1},  # A (10.0) -> 400 points
        ]
        # Current CGPA = 10.0 over 40 credits. Target = 6.0 over 10 credits.
        # Target points = 6.0 * 50 = 300. Points needed = 300 - 400 = -100.
        # Required SGPA is clamped to 0.0, achievable = True.
        res = calculate_target_sgpa(grades, "S1", target_cgpa=6.0, upcoming_credits=10.0)
        self.assertTrue(res["is_achievable"])
        self.assertEqual(res["required_sgpa"], 0.0)

    def test_brand_new_student_zero_completed_credits(self):
        # When student has no completed records, required SGPA equals target CGPA
        res = calculate_target_sgpa([], "NEW_STUDENT", target_cgpa=8.50, upcoming_credits=20.0)
        self.assertFalse(res["student_exists"])
        self.assertIsNone(res["current_cgpa"])
        self.assertEqual(res["current_credits"], 0.0)
        self.assertTrue(res["is_achievable"])
        self.assertEqual(res["required_sgpa"], 8.50)
        self.assertEqual(res["max_possible_cgpa"], 10.0)

    def test_invalid_target_cgpa_range_raises_value_error(self):
        with self.assertRaises(ValueError):
            calculate_target_sgpa([], "S1", target_cgpa=-0.5, upcoming_credits=10.0)
        with self.assertRaises(ValueError):
            calculate_target_sgpa([], "S1", target_cgpa=10.5, upcoming_credits=10.0)

    def test_invalid_upcoming_credits_raises_value_error(self):
        with self.assertRaises(ValueError):
            calculate_target_sgpa([], "S1", target_cgpa=8.0, upcoming_credits=0.0)
        with self.assertRaises(ValueError):
            calculate_target_sgpa([], "S1", target_cgpa=8.0, upcoming_credits=-5.0)


class TestTargetGpaCli(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.csv_path = Path(self.tmp_dir.name) / "test_grades.csv"
        # Create sample data with known credits and scores
        # S1: Mathematics (100/100, 10 cr, A) -> 100 pts. Physics (90/100, 10 cr, A) -> 100 pts. Total = 200 pts, 20 cr, CGPA = 10.0
        # S2: Chemistry (75/100, 20 cr, C) -> 160 pts, 20 cr, CGPA = 8.00
        content = (
            "student_id,subject,score,max_score,date,credits,semester\n"
            "S1,Mathematics,100,100,2025-01-15,10,1\n"
            "S1,Physics,90,100,2025-01-16,10,1\n"
            "S2,Chemistry,75,100,2025-01-17,20,1\n"
        )
        with open(self.csv_path, "w", encoding="utf-8") as f:
            f.write(content)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def run_cli(self, *args):
        cmd = [sys.executable, "-m", "grade_tracker", "--data", str(self.csv_path)] + list(args)
        return subprocess.run(cmd, capture_output=True, text=True)

    def test_cli_achievable_human_output(self):
        # S2: current CGPA = 8.00 (20 cr, 160 pts). Target = 8.50 over 20 upcoming credits.
        # Total credits = 40. Target points = 8.50 * 40 = 340. Needed = 340 - 160 = 180.
        # Required SGPA = 180 / 20 = 9.00.
        res = self.run_cli("target-gpa", "S2", "--target", "8.5", "--credits", "20")
        self.assertEqual(res.returncode, 0)
        self.assertIn("Student: S2", res.stdout)
        self.assertIn("Current CGPA: 8.00 (over 20.0 credits)", res.stdout)
        self.assertIn("Target CGPA:  8.50", res.stdout)
        self.assertIn("Result: Achievable", res.stdout)
        self.assertIn("Required SGPA: 9.00", res.stdout)

    def test_cli_unachievable_human_output(self):
        # S2: current CGPA = 8.00 (20 cr, 160 pts). Target = 9.80 over 10 upcoming credits.
        # Total credits = 30. Target points = 9.80 * 30 = 294. Needed = 294 - 160 = 134.
        # Required SGPA = 134 / 10 = 13.40 (> 10.0). Max CGPA = (160 + 100) / 30 = 8.67.
        res = self.run_cli("target-gpa", "S2", "--target", "9.8", "--credits", "10")
        self.assertEqual(res.returncode, 0)
        self.assertIn("Student: S2", res.stdout)
        self.assertIn("Result: Unachievable in the upcoming semester", res.stdout)
        self.assertIn("Maximum achievable CGPA: 8.67 (with a perfect 10.00 SGPA)", res.stdout)

    def test_cli_json_contract(self):
        res = self.run_cli("target-gpa", "S2", "--target", "8.5", "--credits", "20", "--json")
        self.assertEqual(res.returncode, 0)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["command"], "target-gpa")
        self.assertEqual(payload["student_id"], "S2")
        self.assertEqual(payload["current_cgpa"], 8.0)
        self.assertEqual(payload["current_credits"], 20.0)
        self.assertEqual(payload["target_cgpa"], 8.5)
        self.assertEqual(payload["upcoming_credits"], 20.0)
        self.assertEqual(payload["total_future_credits"], 40.0)
        self.assertEqual(payload["required_sgpa"], 9.0)
        self.assertTrue(payload["is_achievable"])
        self.assertEqual(payload["max_possible_cgpa"], 9.0)

    def test_cli_format_json_flag(self):
        res = self.run_cli("target-gpa", "S2", "--target", "8.5", "--credits", "20", "--format", "json")
        self.assertEqual(res.returncode, 0)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["command"], "target-gpa")
        self.assertEqual(payload["required_sgpa"], 9.0)

    def test_cli_nonexistent_student_exits_1(self):
        res = self.run_cli("target-gpa", "UNKNOWN_STUDENT", "--target", "8.0", "--credits", "10")
        self.assertEqual(res.returncode, 1)
        self.assertIn("Error: student 'UNKNOWN_STUDENT' does not exist.", res.stderr)

    def test_cli_invalid_target_exits_1(self):
        res = self.run_cli("target-gpa", "S1", "--target", "12.0", "--credits", "10")
        self.assertEqual(res.returncode, 1)
        self.assertIn("Error: target CGPA must be between 0.0 and 10.0.", res.stderr)

    def test_cli_invalid_credits_exits_1(self):
        res = self.run_cli("target-gpa", "S1", "--target", "8.0", "--credits", "0")
        self.assertEqual(res.returncode, 1)
        self.assertIn("Error: upcoming credits must be greater than 0.", res.stderr)


if __name__ == "__main__":
    unittest.main()
