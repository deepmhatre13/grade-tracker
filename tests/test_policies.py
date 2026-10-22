"""Tests for the versioned grading-policy system."""

import contextlib
import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from grade_tracker.cli import main

from grade_tracker.calc import (
    calculate_gpa_breakdown,
    calculate_target_sgpa,
    cgpa,
    explain_student_gpa,
    letter_grade,
    sgpa,
)
from grade_tracker.policies import (
    DEFAULT_POLICY,
    DuplicatePolicyError,
    Policy,
    PolicyRegistry,
    UnknownPolicyError,
    default_policy,
    get_default_registry,
    list_policies,
    register_policy,
    resolve_policy,
)


def _strict_policy(identifier="strict", version="1"):
    """A policy with tighter thresholds and different grade points."""
    return Policy(
        identifier=identifier,
        version=version,
        thresholds=((95, "A"), (85, "B"), (75, "C"), (65, "D")),
        grade_points={"A": 10.0, "B": 9.0, "C": 8.0, "D": 7.0, "F": 0.0},
    )


class TestPolicyModel(unittest.TestCase):

    def test_thresholds_are_inclusive(self):
        policy = default_policy()
        self.assertEqual(policy.letter_for_percentage(Decimal("90")), "A")
        self.assertEqual(policy.letter_for_percentage(Decimal("89.999")), "B")
        self.assertEqual(policy.letter_for_percentage(Decimal("80")), "B")
        self.assertEqual(policy.letter_for_percentage(Decimal("60")), "D")
        self.assertEqual(policy.letter_for_percentage(Decimal("59.999")), "F")

    def test_policy_is_immutable(self):
        policy = default_policy()
        with self.assertRaises(Exception):
            policy.identifier = "changed"

    def test_empty_identifier_rejected(self):
        with self.assertRaises(ValueError):
            Policy(
                identifier="  ",
                version="1",
                thresholds=((90, "A"),),
                grade_points={"A": 10.0, "F": 0.0},
            )

    def test_empty_version_rejected(self):
        with self.assertRaises(ValueError):
            Policy(
                identifier="x",
                version="",
                thresholds=((90, "A"),),
                grade_points={"A": 10.0, "F": 0.0},
            )

    def test_thresholds_must_descend(self):
        with self.assertRaises(ValueError):
            Policy(
                identifier="x",
                version="1",
                thresholds=((80, "B"), (90, "A")),
                grade_points={"A": 10.0, "B": 9.0, "F": 0.0},
            )

    def test_duplicate_letters_rejected(self):
        with self.assertRaises(ValueError):
            Policy(
                identifier="x",
                version="1",
                thresholds=((90, "A"), (80, "A")),
                grade_points={"A": 10.0, "F": 0.0},
            )

    def test_threshold_percentage_must_be_in_range(self):
        with self.assertRaises(ValueError):
            Policy(
                identifier="x",
                version="1",
                thresholds=((120, "A"),),
                grade_points={"A": 10.0, "F": 0.0},
            )

    def test_missing_grade_points_rejected(self):
        with self.assertRaises(ValueError):
            Policy(
                identifier="x",
                version="1",
                thresholds=((90, "A"),),
                grade_points={"F": 0.0},
            )

    def test_negative_grade_points_rejected(self):
        with self.assertRaises(ValueError):
            Policy(
                identifier="x",
                version="1",
                thresholds=((90, "A"),),
                grade_points={"A": -1.0, "F": 0.0},
            )

    def test_invalid_rounding_mode_rejected(self):
        with self.assertRaises(ValueError):
            Policy(
                identifier="x",
                version="1",
                thresholds=((90, "A"),),
                grade_points={"A": 10.0, "F": 0.0},
                gpa_rounding_mode="floor",
            )

    def test_custom_rule_overrides_thresholds(self):
        policy = Policy(
            identifier="x",
            version="1",
            thresholds=((90, "A"), (80, "B")),
            grade_points={"A": 10.0, "B": 9.0, "S": 11.0, "F": 0.0},
            additional_rules=(lambda pct, pol: "S" if pct >= Decimal("95") else None,),
        )
        self.assertEqual(policy.letter_for_percentage(Decimal("96")), "S")
        self.assertEqual(policy.letter_for_percentage(Decimal("90")), "A")

    def test_policy_name_and_key(self):
        self.assertEqual(default_policy().name, "legacy@1")
        self.assertEqual(default_policy().key, ("legacy", "1"))

    def test_to_dict_is_json_serializable(self):
        payload = default_policy().to_dict()
        json.dumps(payload)
        self.assertEqual(payload["identifier"], "legacy")
        self.assertEqual(payload["thresholds"][0], {"percentage": 90.0, "letter": "A"})


class TestRounding(unittest.TestCase):

    def test_half_up_rounds_away_from_zero_at_exact_half(self):
        policy = Policy(
            identifier="x",
            version="1",
            thresholds=((90, "A"),),
            grade_points={"A": 10.0, "F": 0.0},
            gpa_rounding=2,
            gpa_rounding_mode="half_up",
        )
        self.assertEqual(policy.round_gpa(8.125), 8.13)

    def test_half_even_rounds_to_even_at_exact_half(self):
        policy = Policy(
            identifier="x",
            version="1",
            thresholds=((90, "A"),),
            grade_points={"A": 10.0, "F": 0.0},
            gpa_rounding=2,
            gpa_rounding_mode="half_even",
        )
        self.assertEqual(policy.round_gpa(8.125), 8.12)

    def test_no_rounding_returns_raw_value(self):
        policy = Policy(
            identifier="x",
            version="1",
            thresholds=((90, "A"),),
            grade_points={"A": 10.0, "F": 0.0},
            gpa_rounding=None,
        )
        self.assertEqual(policy.round_gpa(8.123456), 8.123456)

    def test_percentage_rounding_is_reporting_only(self):
        policy = Policy(
            identifier="x",
            version="1",
            thresholds=((90, "A"),),
            grade_points={"A": 10.0, "F": 0.0},
            percentage_rounding=1,
        )
        self.assertEqual(policy.round_percentage(Decimal("89.94")), Decimal("89.9"))
        self.assertEqual(
            policy.letter_for_percentage(Decimal("89.94")), "F"
        )


class TestRegistry(unittest.TestCase):

    def test_default_registry_contains_legacy_policy(self):
        registry = get_default_registry()
        self.assertIn("legacy@1", registry.names())
        self.assertEqual(registry.get("legacy").name, "legacy@1")

    def test_duplicate_registration_rejected(self):
        registry = PolicyRegistry()
        registry.register(default_policy())
        with self.assertRaises(DuplicatePolicyError):
            registry.register(default_policy())

    def test_same_identifier_different_version_allowed(self):
        registry = PolicyRegistry()
        registry.register(default_policy())
        registry.register(_strict_policy("legacy", "2"))
        self.assertEqual(registry.names(), ["legacy@1", "legacy@2"])

    def test_bare_identifier_resolves_to_highest_version(self):
        registry = PolicyRegistry()
        registry.register(default_policy())
        registry.register(_strict_policy("legacy", "2"))
        self.assertEqual(registry.resolve("legacy").version, "2")

    def test_unknown_policy_raises(self):
        registry = PolicyRegistry()
        with self.assertRaises(UnknownPolicyError):
            registry.resolve("nope")
        with self.assertRaises(UnknownPolicyError):
            registry.get("legacy", "99")

    def test_resolve_policy_accepts_several_forms(self):
        self.assertEqual(resolve_policy(None).name, "legacy@1")
        self.assertEqual(resolve_policy("legacy").name, "legacy@1")
        self.assertEqual(resolve_policy("legacy@1").name, "legacy@1")
        self.assertEqual(resolve_policy(("legacy", "1")).name, "legacy@1")
        self.assertEqual(resolve_policy(default_policy()).name, "legacy@1")

    def test_resolve_policy_rejects_unknown_explicit_selection(self):
        with self.assertRaises(UnknownPolicyError):
            resolve_policy("does-not-exist")

    def test_resolve_policy_rejects_bad_type(self):
        with self.assertRaises(TypeError):
            resolve_policy(42)


class TestCalculationIntegration(unittest.TestCase):

    def setUp(self):
        self.grades = [
            {
                "student_id": "S1",
                "subject": "Math",
                "score": 92,
                "max_score": 100,
                "credits": 3.0,
                "semester": "1",
                "date": "2025-01-15",
            },
            {
                "student_id": "S1",
                "subject": "Physics",
                "score": 78,
                "max_score": 100,
                "credits": 3.0,
                "semester": "1",
                "date": "2025-01-16",
            },
        ]

    def test_default_policy_matches_legacy_behaviour(self):
        # 92 -> A (10 points), 78 -> C (8 points) => 9.0 CGPA
        self.assertEqual(cgpa(self.grades, "S1"), 9.0)
        self.assertEqual(sgpa(self.grades, "S1", "1"), 9.0)
        self.assertEqual(letter_grade(92, 100), "A")

    def test_strict_policy_changes_letter_grades_and_gpa(self):
        policy = _strict_policy()
        # 92 -> B (9), 78 -> C (8) => 8.5 CGPA
        self.assertEqual(cgpa(self.grades, "S1", policy=policy), 8.5)
        self.assertEqual(letter_grade(92, 100, policy=policy), "B")

    def test_policy_named_string_is_accepted(self):
        register_policy(_strict_policy("strict", "1"))
        try:
            self.assertEqual(cgpa(self.grades, "S1", policy="strict@1"), 8.5)
            self.assertEqual(cgpa(self.grades, "S1", policy="strict"), 8.5)
        finally:
            get_default_registry().unregister("strict", "1")

    def test_unknown_policy_name_raises(self):
        with self.assertRaises(UnknownPolicyError):
            cgpa(self.grades, "S1", policy="missing@9")

    def test_breakdown_reports_policy_name(self):
        breakdown = calculate_gpa_breakdown(self.grades)
        self.assertEqual(breakdown["policy"], "legacy@1")

    def test_explain_reports_policy_name(self):
        explanation = explain_student_gpa(self.grades, "S1")
        self.assertEqual(explanation["policy"], "legacy@1")

    def test_target_sgpa_uses_policy_grade_points(self):
        result = calculate_target_sgpa(
            self.grades, "S1", target_cgpa=9.5, upcoming_credits=3.0
        )
        self.assertEqual(result["current_cgpa"], 9.0)
        self.assertEqual(result["policy"], "legacy@1")

    def test_weighted_credits_still_apply_under_a_policy(self):
        grades = [
            {
                "student_id": "S1",
                "subject": "High",
                "score": 100,
                "max_score": 100,
                "credits": 4.0,
                "semester": "1",
                "date": "2025-01-15",
            },
            {
                "student_id": "S1",
                "subject": "Low",
                "score": 60,
                "max_score": 100,
                "credits": 1.0,
                "semester": "1",
                "date": "2025-01-16",
            },
        ]
        # Legacy: A (10) * 4 + D (7) * 1 = 47 / 5 = 9.4
        self.assertEqual(cgpa(grades, "S1"), 9.4)
        # Strict: A (10) * 4 + F (0) * 1 = 40 / 5 = 8.0
        self.assertEqual(cgpa(grades, "S1", policy=_strict_policy()), 8.0)


class TestPolicyCli(unittest.TestCase):
    """Exercise the --policy option and the policies command."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_path = Path(self.temp_dir.name) / "grades.csv"
        self.data_path.write_text(
            "student_id,subject,score,max_score,date,credits,semester\n"
            "S1,Mathematics,92,100,2025-01-15,3,1\n"
            "S1,Physics,78,100,2025-01-16,3,1\n",
            encoding="utf-8",
        )
        register_policy(_strict_policy("strict", "1"))
        self.addCleanup(get_default_registry().unregister, "strict", "1")

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

    def test_policies_command_lists_default_policy(self):
        code, stdout, stderr = self.run_cli("policies")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("legacy@1", stdout)
        self.assertIn("strict@1", stdout)

    def test_policies_command_json(self):
        code, stdout, stderr = self.run_cli("policies", "--format", "json")
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["command"], "policies")
        self.assertEqual(payload["default"], "legacy@1")
        names = [p["name"] for p in payload["policies"]]
        self.assertIn("legacy@1", names)
        self.assertIn("strict@1", names)

    def test_summary_uses_selected_policy(self):
        code, stdout, stderr = self.run_cli(
            "summary", "S1", "--policy", "strict@1", "--format", "json"
        )
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["policy"], "strict@1")
        self.assertEqual(payload["cgpa"], 8.5)
        self.assertEqual(payload["grades"][0]["letter_grade"], "B")

    def test_summary_defaults_to_legacy_policy(self):
        code, stdout, stderr = self.run_cli(
            "summary", "S1", "--format", "json"
        )
        payload = json.loads(stdout)
        self.assertEqual(payload["policy"], "legacy@1")
        self.assertEqual(payload["cgpa"], 9.0)

    def test_explain_reports_policy(self):
        code, stdout, stderr = self.run_cli(
            "explain", "S1", "--policy", "strict@1", "--format", "json"
        )
        payload = json.loads(stdout)
        self.assertEqual(payload["policy"], "strict@1")
        self.assertEqual(payload["cgpa"]["gpa"], 8.5)

    def test_target_gpa_reports_policy(self):
        code, stdout, stderr = self.run_cli(
            "target-gpa", "S1", "--target", "9.5", "--credits", "3",
            "--policy", "strict@1", "--format", "json",
        )
        payload = json.loads(stdout)
        self.assertEqual(payload["policy"], "strict@1")
        self.assertEqual(payload["current_cgpa"], 8.5)

    def test_unknown_policy_fails_with_clear_error(self):
        code, stdout, stderr = self.run_cli(
            "summary", "S1", "--policy", "nope@1"
        )
        self.assertEqual(code, 1)
        self.assertIn("unknown grading policy", stderr)

    def test_unknown_policy_json_error_payload(self):
        code, stdout, stderr = self.run_cli(
            "summary", "S1", "--policy", "nope@1", "--format", "json"
        )
        self.assertEqual(code, 1)
        payload = json.loads(stdout)
        self.assertEqual(payload["error"]["code"], "unknown_policy")
        self.assertIn("unknown grading policy", payload["error"]["message"])

    def test_export_records_policy_in_html(self):
        output = Path(self.temp_dir.name) / "report.html"
        code, stdout, stderr = self.run_cli(
            "export", "--output", str(output), "--policy", "strict@1"
        )
        self.assertEqual(code, 0)
        html = output.read_text(encoding="utf-8")
        self.assertIn("strict@1", html)
