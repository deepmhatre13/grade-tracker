"""Unit tests for calculation logic in grade_tracker."""

import unittest
from decimal import Decimal
from grade_tracker.calc import (
    assessment_key,
    calculate_gpa_breakdown,
    cgpa,
    explain_student_gpa,
    letter_grade,
    percentage,
    sgpa,
    subject_average,
)


class TestGradeCalculations(unittest.TestCase):

    def test_letter_grade_top_tier(self):
        """A score of 95/100 should return an 'A'."""
        self.assertEqual(letter_grade(95, 100), "A")

    def test_letter_grade_invalid_max_score(self):
        """A max_score <= 0 should raise ValueError."""
        with self.assertRaises(ValueError):
            letter_grade(50, 0)

    def test_cgpa_empty_returns_none(self):
        """Calculating CGPA on an empty list of grades should return None."""
        self.assertIsNone(cgpa([]))

    def test_sgpa_single_course(self):
        """Single course with score 95/100 should evaluate to 10.0 SGPA (A grade, 10.0 pts)."""
        grades = [{"student_id": "S1", "score": 95, "max_score": 100, "credits": 3, "semester": 1}]
        self.assertEqual(sgpa(grades, "S1", 1), 10.0)

    def test_cgpa_worked_example(self):
        """Worked example for CGPA and SGPA with credits."""
        grades = [
            {"student_id": "S1", "score": 95, "max_score": 100, "credits": 4, "semester": 1}, # A -> 10 * 4 = 40
            {"student_id": "S1", "score": 85, "max_score": 100, "credits": 3, "semester": 1}, # B -> 9 * 3 = 27
            {"student_id": "S1", "score": 75, "max_score": 100, "credits": 2, "semester": 2}, # C -> 8 * 2 = 16
        ]
        # SGPA Sem 1: (40 + 27) / 7 = 67 / 7 = 9.57
        self.assertEqual(sgpa(grades, "S1", 1), 9.57)
        # SGPA Sem 2: 16 / 2 = 8.0
        self.assertEqual(sgpa(grades, "S1", 2), 8.0)
        # CGPA: (40 + 27 + 16) / 9 = 83 / 9 = 9.22
        self.assertEqual(cgpa(grades, "S1"), 9.22)

    def test_subject_average_single_entry(self):
        """A single grade entry for a subject should return that score as average."""
        grades = [{"subject": "Mathematics", "score": 85, "max_score": 100}]
        self.assertEqual(subject_average(grades, "Mathematics"), 85.0)

    def test_subject_average_nonexistent(self):
        """Subject average should return None if no entries exist for that subject."""
        self.assertIsNone(subject_average([], "Physics"))

    def test_subject_average_mixed_max_scores(self):
        """Grades with different maximum scores should be averaged as percentages.

        Regression test for Issue #5: 35/50 and 70/100 are both 70%, so the
        average must be 70.0, not the raw-score average of 52.5.
        """
        grades = [
            {"subject": "Physics", "score": 35, "max_score": 50},
            {"subject": "Physics", "score": 70, "max_score": 100},
        ]
        self.assertEqual(subject_average(grades, "Physics"), 70.0)

    def test_subject_average_same_max_score(self):
        """When every grade shares a maximum score the average percentage matches the raw average."""
        grades = [
            {"subject": "Mathematics", "score": 80, "max_score": 100},
            {"subject": "Mathematics", "score": 90, "max_score": 100},
        ]
        self.assertEqual(subject_average(grades, "Mathematics"), 85.0)

    def test_subject_average_different_percentages(self):
        """Percentages that differ should be averaged, not raw scores."""
        grades = [
            {"subject": "Chemistry", "score": 45, "max_score": 50},   # 90%
            {"subject": "Chemistry", "score": 40, "max_score": 100},  # 40%
        ]
        self.assertEqual(subject_average(grades, "Chemistry"), 65.0)

    def test_subject_average_rounds_only_final_result(self):
        """Individual percentages are not rounded before averaging.

        43.07/39.96 = 89.967190...% and 72.3/99.49 = 72.670619...%. Their
        exact average is 90.226701...%, which rounds once to 90.23. Rounding
        each percentage to 2 decimals first (89.97 and 72.67) would average
        to 81.32 — a different value — so the average must be computed from
        the unrounded percentages.
        """
        grades = [
            {"subject": "Physics", "score": 43.07, "max_score": 39.96},
            {"subject": "Physics", "score": 72.3, "max_score": 99.49},
        ]
        # Exact average of the unrounded percentages, rounded once at the end.
        self.assertEqual(subject_average(grades, "Physics"), 90.23)
        # Rounding each percentage first would give a different result,
        # confirming no premature rounding of individual percentages.
        rounded_first = (round(89.967190467967535, 2) + round(72.67061956980598, 2)) / 2
        self.assertNotEqual(round(rounded_first, 2), 90.23)

    def test_subject_average_exact_threshold_display_rounding(self):
        """Average near a threshold rounds once, at the end, to 2 decimals."""
        # 8.1/9 = exactly 90% twice -> average exactly 90.0
        grades = [
            {"subject": "Math", "score": 8.1, "max_score": 9},
            {"subject": "Math", "score": 8.1, "max_score": 9},
        ]
        self.assertEqual(subject_average(grades, "Math"), 90.0)

    def test_subject_average_non_100_max_scores(self):
        """Percentages from different non-100 maximums are averaged fairly."""
        grades = [
            {"subject": "History", "score": 90, "max_score": 150},   # 60%
            {"subject": "History", "score": 105, "max_score": 150},  # 70%
        ]
        self.assertEqual(subject_average(grades, "History"), 65.0)

    def test_letter_grade_boundaries(self):
        """checking letter grade boundaries"""
        self.assertEqual(letter_grade(90, 100), "A")
        self.assertEqual(letter_grade(80, 100), "B")
        self.assertEqual(letter_grade(70, 100), "C")
        self.assertEqual(letter_grade(60, 100), "D")
        self.assertEqual(letter_grade(59, 100), "F")
    def test_letter_grade_boundaries(self):
        """checking letter grade boundaries"""
        self.assertEqual(letter_grade(90, 100), "A")
        self.assertEqual(letter_grade(80, 100), "B")
        self.assertEqual(letter_grade(70, 100), "C")
        self.assertEqual(letter_grade(60, 100), "D")
        self.assertEqual(letter_grade(59, 100), "F")

    def test_letter_grade_around_each_threshold(self):
        """Immediately below / exactly at / immediately above each threshold."""
        # (below, at, above) percentage and expected grades
        cases = [
            (59, 60, 61, "F", "D", "D"),   # 60 threshold
            (69, 70, 71, "D", "C", "C"),   # 70 threshold
            (79, 80, 81, "C", "B", "B"),   # 80 threshold
            (89, 90, 91, "B", "A", "A"),   # 90 threshold
        ]
        for below, at, above, g_below, g_at, g_above in cases:
            self.assertEqual(letter_grade(below, 100), g_below)
            self.assertEqual(letter_grade(at, 100), g_at)
            self.assertEqual(letter_grade(above, 100), g_above)

    def test_letter_grade_uses_unrounded_percentage_not_display(self):
        """A percentage that *displays* as a threshold stays below it.

        89.95/100 is 89.95%, which rounds to 90.0% for display, but the
        actual percentage is below 90, so the grade must remain B.
        """
        pct = (89.95 / 100) * 100
        self.assertEqual(f"{pct:.1f}", "90.0")  # displayed percentage rounds to 90.0
        self.assertEqual(letter_grade(89.95, 100), "B")

    def test_letter_grade_exact_threshold_with_decimal_float_inputs(self):
        """Binary float drift must not push an exact threshold below it.

        Each pair is exactly at the threshold in decimal arithmetic, but
        ``(score / max_score) * 100`` in binary float lands just below it
        (e.g. ``(8.1 / 9) * 100 == 89.99999999999999``), which previously
        misclassified the grade.
        """
        self.assertEqual(letter_grade(8.1, 9), "A")        # exact 90.0% -> A (was B)
        self.assertEqual(letter_grade(0.09, 0.1), "A")     # exact 90.0% -> A (was B)
        self.assertEqual(letter_grade(2.01, 3.35), "D")    # exact 60.0% -> D (was F)
        self.assertEqual(letter_grade(5.81, 8.3), "C")     # exact 70.0% -> C (was D)
        self.assertEqual(letter_grade(4.52, 5.65), "B")    # exact 80.0% -> B (was C)

    def test_letter_grade_genuinely_below_threshold_stays_below(self):
        """No epsilon tolerance: values genuinely below a threshold stay below."""
        # 8.099/9 = 89.988...% — genuinely below 90, must be B not A.
        self.assertEqual(letter_grade(8.099, 9), "B")
        # 0.0899/0.1 = 89.9% — genuinely below 90, must be B not A.
        self.assertEqual(letter_grade(0.0899, 0.1), "B")
        # 59.99/100 = 59.99% — genuinely below 60, must be F not D.
        self.assertEqual(letter_grade(59.99, 100), "F")

    def test_letter_grade_non_100_max_exact_threshold(self):
        """Exact threshold with a non-100 maximum score."""
        # 180/200 = 90% exactly
        self.assertEqual(letter_grade(180, 200), "A")
        # 179/200 = 89.5% — just below
        self.assertEqual(letter_grade(179, 200), "B")
        # 181/200 = 90.5% — just above
        self.assertEqual(letter_grade(181, 200), "A")
        # 140/200 = 70% exactly (decimal-exact threshold with non-100 max)
        self.assertEqual(letter_grade(140, 200), "C")
        # 139/200 = 69.5% — just below
        self.assertEqual(letter_grade(139, 200), "D")

    def test_percentage_helper_full_precision(self):
        """percentage() returns the exact decimal percentage, not a rounded one."""
        self.assertEqual(percentage(8.1, 9), Decimal("90"))
        self.assertEqual(percentage(35, 50), Decimal("70"))
        # Not rounded: full precision value
        pct = percentage(52.1, 57.91)
        self.assertEqual(pct, Decimal("52.1") / Decimal("57.91") * 100)
        self.assertNotEqual(pct, round(pct, 2))


class TestSemesterRecordConsistency(unittest.TestCase):
    """Regression tests for Issue #12: SGPA/CGPA with repeated/duplicate records.

    Model: a record is identified by (student_id, semester, subject, date).
    Multiple records for the same subject in one semester collapse to one
    contribution in that semester's SGPA (and hence in CGPA); the same
    subject in a different semester is a legitimate retake and is kept.
    """

    @staticmethod
    def rec(subject, score, credits, semester, date="", student_id="S1"):
        return {
            "student_id": student_id,
            "subject": subject,
            "score": score,
            "max_score": 100,
            "date": date,
            "credits": credits,
            "semester": str(semester),
        }

    def test_single_subject_sgpa(self):
        """A single subject contributes exactly its grade point."""
        grades = [self.rec("Math", 95, 3, 1)]
        self.assertEqual(sgpa(grades, "S1", 1), 10.0)

    def test_multiple_subjects_credit_weighted_sgpa(self):
        """SGPA is credit-weighted: (10*4 + 9*3) / 7 = 9.57."""
        grades = [
            self.rec("Math", 95, 4, 1),
            self.rec("Phy", 85, 3, 1),
        ]
        self.assertEqual(sgpa(grades, "S1", 1), 9.57)

    def test_same_subject_different_semesters_is_retake(self):
        """The same subject in two semesters is kept in both semesters."""
        grades = [
            self.rec("Math", 95, 3, 1),
            self.rec("Math", 85, 3, 2),
        ]
        self.assertEqual(sgpa(grades, "S1", 1), 10.0)
        self.assertEqual(sgpa(grades, "S1", 2), 9.0)
        # Retake is a distinct result: CGPA = (10*3 + 9*3)/6 = 9.5
        self.assertEqual(cgpa(grades, "S1"), 9.5)

    def test_duplicate_same_semester_record_with_different_score(self):
        """A duplicate record in one semester must not be double-counted.

        Math appears twice in semester 1 with different scores; only the
        first distinct record counts, so SGPA = (10*3 + 9*3)/6 = 9.5,
        not the double-counted 9.0.
        """
        grades = [
            self.rec("Math", 95, 3, 1),
            self.rec("Math", 70, 3, 1),  # duplicate of the same subject/semester
            self.rec("Phy", 85, 3, 1),
        ]
        self.assertEqual(sgpa(grades, "S1", 1), 9.5)

    def test_exact_duplicate_row_with_different_credits(self):
        """A duplicated row with different credits is still one record."""
        grades = [
            self.rec("Math", 95, 3, 1),
            self.rec("Math", 95, 4, 1),  # same record, credits corrupted to 4
            self.rec("Phy", 85, 3, 1),
        ]
        # First record wins: (10*3 + 9*3)/6 = 9.5
        self.assertEqual(sgpa(grades, "S1", 1), 9.5)

    def test_cgpa_across_semesters_with_duplicate(self):
        """CGPA aggregates distinct records across all semesters."""
        grades = [
            self.rec("Math", 95, 4, 1),
            self.rec("Math", 95, 4, 1),  # duplicate in semester 1
            self.rec("Phy", 85, 3, 1),
            self.rec("Chem", 75, 2, 2),
        ]
        # Distinct: sem1 Math(4), Phy(3); sem2 Chem(2)
        # (10*4 + 9*3 + 8*2)/9 = 83/9 = 9.22
        self.assertEqual(cgpa(grades, "S1"), 9.22)

    def test_unequal_credits_preserved(self):
        """Credit weighting uses each distinct record's own credits."""
        grades = [
            self.rec("Math", 95, 5, 1),
            self.rec("Phy", 75, 1, 1),
        ]
        # (10*5 + 8*1)/6 = 58/6 = 9.67
        self.assertEqual(sgpa(grades, "S1", 1), 9.67)

    def test_duplicate_from_csv_rows(self):
        """Duplicate rows as loaded from a CSV are collapsed."""
        grades = [
            {"student_id": "S1", "subject": "Mathematics", "score": 95.0,
             "max_score": 100.0, "date": "2025-01-15", "credits": 3.0, "semester": "1"},
            {"student_id": "S1", "subject": "Mathematics", "score": 70.0,
             "max_score": 100.0, "date": "2025-01-15", "credits": 3.0, "semester": "1"},
            {"student_id": "S1", "subject": "Physics", "score": 85.0,
             "max_score": 100.0, "date": "2025-01-16", "credits": 3.0, "semester": "1"},
        ]
        # First record wins: (10*3 + 9*3)/6 = 9.5
        self.assertEqual(sgpa(grades, "S1", 1), 9.5)

    def test_different_dates_same_subject_are_distinct_assessments(self):
        """Two assessments of the same subject on different dates both count."""
        grades = [
            self.rec("Math", 95, 3, 1, date="2025-01-15"),
            self.rec("Math", 75, 3, 1, date="2025-02-15"),
        ]
        # (10*3 + 8*3)/6 = 9.0
        self.assertEqual(sgpa(grades, "S1", 1), 9.0)

    def test_zero_credits_returns_zero(self):
        """A semester whose records all have zero credits reports 0.0."""
        grades = [self.rec("Math", 95, 0, 1)]
        self.assertEqual(sgpa(grades, "S1", 1), 0.0)

    def test_empty_and_no_match_return_none(self):
        """No matching records still return None, not a bogus number."""
        self.assertIsNone(sgpa([], "S1", 1))
        self.assertIsNone(cgpa([], "S1"))
        grades = [self.rec("Math", 95, 3, 1)]
        self.assertIsNone(sgpa(grades, "S2", 1))

    def test_different_subjects_same_date_are_distinct(self):
        """Two subjects graded on the same date are separate records."""
        grades = [
            self.rec("Math", 95, 3, 1, date="2025-01-15"),
            self.rec("Phy", 75, 3, 1, date="2025-01-15"),
        ]
        # (10*3 + 8*3)/6 = 9.0
        self.assertEqual(sgpa(grades, "S1", 1), 9.0)

    def test_malformed_records_without_subject_are_never_deduplicated(self):
        """A record with no subject has no identity and is always kept.

        Loose-input callers (and the historical tests) may pass rows without
        a subject. Inventing an identity from score/max_score would wrongly
        collapse or split them, so each malformed row is preserved as-is.
        """
        grades = [
            {"student_id": "S1", "score": 95, "max_score": 100, "credits": 4, "semester": "1"},
            {"student_id": "S1", "score": 85, "max_score": 100, "credits": 3, "semester": "1"},
        ]
        # Both rows kept: (10*4 + 9*3)/7 = 67/7 = 9.57
        self.assertEqual(sgpa(grades, "S1", 1), 9.57)

    def test_corrected_score_same_identity_is_not_split(self):
        """A corrected score keeps the same identity, so it is not double-counted.

        Identity must never depend on score: changing 70 -> 95 for the same
        (student, semester, subject, date) is still one assessment.
        """
        original = self.rec("Math", 70, 3, 1, date="2025-01-15")
        corrected = self.rec("Math", 95, 3, 1, date="2025-01-15")
        self.assertEqual(
            assessment_key(original),
            assessment_key(corrected),
        )
        # Only the first row (70 -> C=8) contributes once; corrected row collapses.
        grades = [original, corrected, self.rec("Phy", 85, 3, 1, date="2025-01-16")]
        # (8*3 + 9*3)/6 = 51/6 = 8.5
        self.assertEqual(sgpa(grades, "S1", 1), 8.5)

    def test_malformed_record_key_is_none(self):
        """assessment_key returns None when subject is missing/empty."""
        self.assertIsNone(assessment_key({"student_id": "S1", "score": 95, "semester": "1"}))
        self.assertIsNone(assessment_key({"student_id": "S1", "subject": "  ", "semester": "1"}))

    def test_well_formed_record_key_matches_persistence_identity(self):
        """calc.assessment_key matches the identity used when persisting."""
        grade = {"student_id": "S1", "subject": "Mathematics", "date": "2026-10-06", "semester": "1"}
        self.assertEqual(
            assessment_key(grade),
            ("S1", "1", "mathematics", "2026-10-06"),
        )


class TestGradeRoundingPrecision(unittest.TestCase):
    """Regression tests for Issue #15: rounding/precision policy.

    Policy under test:
      * percentages are computed at full precision;
      * letter grades are decided from the unrounded percentage;
      * individual percentages and intermediate weighted totals are never
        rounded before aggregation;
      * only final user-facing results are rounded (2 decimals).
    """

    @staticmethod
    def rec(subject, score, credits, semester, max_score=100, date="", student_id="S1"):
        return {
            "student_id": student_id,
            "subject": subject,
            "score": score,
            "max_score": max_score,
            "date": date,
            "credits": credits,
            "semester": str(semester),
        }

    def test_sgpa_classifies_from_unrounded_percentage(self):
        """SGPA grade points come from the exact percentage, not a rounded one.

        8.1/9 is exactly 90% in decimal, so the subject earns A (10 points).
        Binary float would compute 89.99999999999999% and wrongly give B.
        """
        grades = [self.rec("Math", 8.1, 3, 1, max_score=9)]
        self.assertEqual(sgpa(grades, "S1", 1), 10.0)

    def test_cgpa_classifies_from_unrounded_percentage(self):
        """Same unrounded classification applies across all semesters (CGPA)."""
        grades = [self.rec("Math", 0.09, 3, 1, max_score=0.1)]  # exactly 90% -> A
        self.assertEqual(cgpa(grades, "S1"), 10.0)

    def test_sgpa_unequal_credits_unequal_grades(self):
        """Credit weighting with unrounded totals, rounded only at the end.

        Sem 1: Math 8.1/9 = 90% -> A = 10 pts * 4 credits = 40
               Phy  85/100 = 85% -> B = 9 pts * 3 credits = 27
               SGPA = (40 + 27) / 7 = 67 / 7 = 9.5714... -> 9.57
        """
        grades = [
            self.rec("Math", 8.1, 4, 1, max_score=9),
            self.rec("Phy", 85, 3, 1),
        ]
        self.assertEqual(sgpa(grades, "S1", 1), 9.57)

    def test_cgpa_multiple_semesters_not_prematurely_rounded(self):
        """Intermediate weighted totals are unrounded; only the result rounds.

        Distinct records: sem1 Math (A, 4cr -> 40), sem1 Phy (B, 3cr -> 27),
        sem2 Chem (C, 2cr -> 16). CGPA = (40 + 27 + 16) / 9 = 83 / 9
        = 9.2222... -> 9.22 (rounding the running totals first could give a
        different value).
        """
        grades = [
            self.rec("Math", 95, 4, 1),
            self.rec("Phy", 85, 3, 1),
            self.rec("Chem", 75, 2, 2),
        ]
        self.assertEqual(cgpa(grades, "S1"), 9.22)

    def test_sgpa_decimal_credits_weighting(self):
        """Decimal credit weights are honoured without premature rounding.

        Math: A = 10 pts * 0.5 cr = 5.0; Phy: B = 9 pts * 0.5 cr = 4.5
        SGPA = (5.0 + 4.5) / 1.0 = 9.5
        """
        grades = [
            self.rec("Math", 95, 0.5, 1),
            self.rec("Phy", 85, 0.5, 1),
        ]
        self.assertEqual(sgpa(grades, "S1", 1), 9.5)

    def test_sgpa_grade_point_boundary_classification(self):
        """Scores exactly at a threshold get the boundary grade's points."""
        # 90/100 = 90% exactly -> A = 10
        self.assertEqual(sgpa([self.rec("Math", 90, 3, 1)], "S1", 1), 10.0)
        # 89/100 = 89% -> B = 9
        self.assertEqual(sgpa([self.rec("Math", 89, 3, 1)], "S1", 1), 9.0)
        # 8.1/9 = exactly 90% -> A = 10 (float would say B)
        self.assertEqual(sgpa([self.rec("Math", 8.1, 3, 1, max_score=9)], "S1", 1), 10.0)

    def test_sgpa_empty_and_zero_credit_unchanged(self):
        """Existing empty-data and zero-credit semantics are preserved."""
        self.assertIsNone(sgpa([], "S1", 1))
        self.assertIsNone(cgpa([], "S1"))
        grades = [self.rec("Math", 95, 0, 1)]
        self.assertEqual(sgpa(grades, "S1", 1), 0.0)

    def test_duplicate_records_still_collapsed(self):
        """Duplicate-record semantics are unchanged by the precision fix."""
        grades = [
            self.rec("Math", 95, 3, 1, date="2025-01-15"),
            self.rec("Math", 70, 3, 1, date="2025-01-15"),  # duplicate
            self.rec("Phy", 85, 3, 1, date="2025-01-16"),
        ]
        # First record wins: (10*3 + 9*3)/6 = 9.5
        self.assertEqual(sgpa(grades, "S1", 1), 9.5)


class TestGpaExplainCalculation(unittest.TestCase):
    """Direct tests for calculate_gpa_breakdown and explain_student_gpa."""

    def test_calculate_gpa_breakdown_per_assessment(self):
        grades = [
            {
                "student_id": "S1",
                "subject": "Math",
                "score": 95,
                "max_score": 100,
                "credits": 3.0,
                "semester": "1",
                "date": "2025-01-15",
            },
            {
                "student_id": "S1",
                "subject": "Math",
                "score": 70,
                "max_score": 100,
                "credits": 3.0,
                "semester": "1",
                "date": "2025-01-15",
            },
            {
                "student_id": "S1",
                "subject": "Physics",
                "score": 85,
                "max_score": 100,
                "credits": 4.0,
                "semester": "1",
                "date": "2025-01-16",
            },
        ]
        breakdown = calculate_gpa_breakdown(grades)
        self.assertEqual(len(breakdown["records"]), 3)

        # First Math is included
        r0 = breakdown["records"][0]
        self.assertTrue(r0["included"])
        self.assertIsNone(r0["exclusion_reason"])
        self.assertEqual(r0["grade_points"], 10.0)
        self.assertEqual(r0["weighted_points"], 30.0)

        # Duplicate Math is excluded
        r1 = breakdown["records"][1]
        self.assertFalse(r1["included"])
        self.assertEqual(r1["exclusion_reason"], "Duplicate assessment in calculation scope")
        self.assertEqual(r1["grade_points"], 8.0)
        self.assertEqual(r1["weighted_points"], 24.0)

        # Physics is included
        r2 = breakdown["records"][2]
        self.assertTrue(r2["included"])
        self.assertEqual(r2["grade_points"], 9.0)
        self.assertEqual(r2["weighted_points"], 36.0)

        # Aggregate numerator = 30 + 36 = 66, denominator = 3 + 4 = 7 -> GPA = 9.43
        self.assertEqual(breakdown["total_points"], 66.0)
        self.assertEqual(breakdown["total_credits"], 7.0)
        self.assertEqual(breakdown["gpa"], 9.43)

    def test_explain_student_gpa_semester_filter(self):
        grades = [
            {"student_id": "S1", "subject": "Math", "score": 95, "max_score": 100, "credits": 3.0, "semester": "1", "date": "2025-01-15"},
            {"student_id": "S1", "subject": "History", "score": 85, "max_score": 100, "credits": 4.0, "semester": "2", "date": "2025-06-15"},
        ]
        explanation = explain_student_gpa(grades, "S1", semester="1")
        self.assertTrue(explanation["student_exists"])
        self.assertTrue(explanation["semester_found"])
        self.assertEqual(list(explanation["semesters"].keys()), ["1"])

        # Overall CGPA includes both semesters: (30 + 36) / 7 = 9.43
        self.assertEqual(explanation["cgpa"]["gpa"], 9.43)
        self.assertEqual(explanation["cgpa"]["total_credits"], 7.0)

    def test_explain_student_gpa_nonexistent_student(self):
        explanation = explain_student_gpa([], "UNKNOWN")
        self.assertFalse(explanation["student_exists"])
        self.assertFalse(explanation["semester_found"])
        self.assertIsNone(explanation["cgpa"]["gpa"])

    def test_explain_student_gpa_nonexistent_semester(self):
        grades = [
            {"student_id": "S1", "subject": "Math", "score": 95, "max_score": 100, "credits": 3.0, "semester": "1", "date": "2025-01-15"}
        ]
        explanation = explain_student_gpa(grades, "S1", semester="99")
        self.assertTrue(explanation["student_exists"])
        self.assertFalse(explanation["semester_found"])
        self.assertEqual(explanation["semesters"], {})
        self.assertEqual(explanation["cgpa"]["gpa"], 10.0)


if __name__ == "__main__":
    unittest.main()
