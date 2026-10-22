"""Calculation module for academic grades, GPA, and averages.

Numerical policy
----------------
1. Percentages are calculated at full precision.
2. Letter grades are determined from the *unrounded* percentage, never from
   a rounded display percentage.
3. Individual percentages are never rounded before being averaged into a
   subject average.
4. Intermediate weighted grade-point totals and credit totals are never
   rounded; only the final user-facing result is rounded, at the existing
   documented output precision (2 decimals for SGPA/CGPA/subject average,
   1 decimal for displayed percentages in the CLI/report).
5. Letter-grade thresholds (90/80/70/60) are unchanged.

Why Decimal is used for classification
--------------------------------------
Binary floating-point cannot represent most decimal fractions exactly.
Computing ``(score / max_score) * 100`` with ``float`` can land a hair
*below* a threshold even when the values are exactly at that threshold in
decimal (e.g. ``8.1 / 9`` is exactly 90%, but ``(8.1 / 9) * 100`` evaluates
to ``89.99999999999999``), misclassifying the grade. Constructing the
operands with ``Decimal(str(x))`` recovers the shortest round-trip decimal
text (which is what the CSV/CLI actually stored) and yields an exact decimal
percentage for classification. Aggregates (averages, GPAs) stay in ``float``
because their final 2-decimal rounding absorbs representation error
(verified against exact rational arithmetic), so converting the whole
module to ``Decimal`` would be unnecessary refactoring.
"""

from datetime import date
from decimal import Decimal
import math
import re

from grade_tracker.policies import resolve_policy

# Percentages are compared against these thresholds exactly, with no
# epsilon tolerance: a value genuinely below a threshold stays below it.
#
# These constants describe the *default* (legacy) grading policy.  They are
# kept for backwards compatibility; new code should read the thresholds from
# a :class:`grade_tracker.policies.Policy` instead.
GRADE_THRESHOLDS = ((90, "A"), (80, "B"), (70, "C"), (60, "D"))


GRADE_POINTS = {
    "A": 10.0,
    "B": 9.0,
    "C": 8.0,
    "D": 7.0,
    "F": 0.0,
}


def assessment_key(grade):
    """Authoritative identity of one academic record (single source of truth).

    Used by mutation and validation paths to locate an assessment without
    making mutable grade data part of its identity.

    A grade row records a result for one student, in one (semester), for one
    (subject) on one (date). score, max_score, and credits describe the
    result; they are NOT part of its identity, so a corrected score is still
    the same assessment. Two rows with the same
    (student_id, semester, subject, date) therefore address the same academic
    record for editing and ordinary duplicate rejection.

    A subject repeated in a *different* semester yields a different key, so
    legitimate retakes are preserved.

    Returns None when the record is malformed, i.e. it has no (non-empty)
    subject and therefore no determinate identity. Callers must then treat
    the record as un-deduplicatable rather than invent an identity for it.
    score/max_score are deliberately never consulted.
    """
    subject = str(grade.get("subject", "")).strip()
    if not subject:
        return None
    return (
        str(grade.get("student_id", "")).strip(),
        str(grade.get("semester", "")).strip(),
        subject.casefold(),
        str(grade.get("date", "")).strip(),
    )


def assessment_record_key(grade):
    """Return the complete stored-record key used only for GPA deduplication.

    ``assessment_key`` remains the authoritative four-field identity used by
    add/edit/import/validate.  Score, maximum score, and credits deliberately
    stay outside that identity so a correction still addresses the same
    assessment.  Once multiple same-day assessments have been explicitly
    stored, however, calculations must distinguish their complete grade data
    or one of them would silently disappear from SGPA/CGPA.

    Exact repeated rows share this key and are still counted only once.
    """
    key = assessment_key(grade)
    if key is None:
        return None
    return key + (
        grade.get("score"),
        grade.get("max_score"),
        _credit(grade),
    )


def _credit(grade):
    """Credits of a grade row as a float, falling back to the default of 1.0."""
    try:
        return float(grade.get("credits", 1.0))
    except (TypeError, ValueError):
        return 1.0


def validate_grade(score, max_score, date_value, credits=None):
    """Validate a grade before it can be persisted.

    Raises:
        ValueError: If the score range, credits, or calendar date is invalid.
    """
    if max_score <= 0:
        raise ValueError("max_score must be greater than 0")
    if not 0 <= score <= max_score:
        raise ValueError("score must be between 0 and max_score")
    if credits is not None and (not math.isfinite(credits) or credits < 0):
        raise ValueError("credits must be a finite number greater than or equal to 0")
    if not isinstance(date_value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value):
        raise ValueError("date must use YYYY-MM-DD format")
    try:
        date.fromisoformat(date_value)
    except ValueError as exc:
        raise ValueError("date must be a valid calendar date in YYYY-MM-DD format") from exc


def percentage(score, max_score):
    """Returns the full-precision percentage ``score / max_score * 100``.

    Computed with ``Decimal`` built from each value's shortest round-trip
    decimal text, so percentages that are exact in decimal (e.g. ``8.1/9``)
    stay exact instead of drifting a hair below a threshold in binary
    floating-point. The result is a ``Decimal``; use ``float(...)`` for
    display or accumulation.
    """
    if max_score <= 0:
        raise ValueError("max_score must be greater than 0")
    return Decimal(str(score)) / Decimal(str(max_score)) * 100


def letter_grade(score, max_score=100, policy=None):
    """Determines the letter grade corresponding to a score and max_score.

    Classification uses the unrounded percentage (see :func:`percentage`),
    never a rounded display percentage, and compares against the policy's
    thresholds exactly — no epsilon tolerance is applied, so a value
    genuinely below a threshold keeps the lower grade.

    ``policy`` selects the grading policy to use (a
    :class:`grade_tracker.policies.Policy`, an ``"identifier@version"``
    string, an ``(identifier, version)`` tuple, or ``None`` for the default
    legacy policy).
    """
    if max_score <= 0:
        raise ValueError("max_score must be greater than 0")

    policy = resolve_policy(policy)
    pct = percentage(score, max_score)

    return policy.letter_for_percentage(pct)


def calculate_gpa_breakdown(grades, policy=None):
    """Calculates GPA breakdown and intermediate values for a pre-filtered list of grades.

    ``policy`` selects the grading policy (a
    :class:`grade_tracker.policies.Policy`, an ``"identifier@version"``
    string, an ``(identifier, version)`` tuple, or ``None`` for the default
    legacy policy).  The policy supplies the letter-grade thresholds, the
    grade points per letter grade, and the GPA rounding rule.

    Processes records in input order:
    1. Evaluates percentage, letter grade, grade points, and credits for each record.
    2. Deduplicates records using assessment_record_key:
       - The first occurrence of a complete stored record is included.
       - Exact repeated rows are excluded as duplicates.
       - Same-day assessments with different grade data are included.
       - Records with no determinate identity are always included.
    3. Accumulates weighted grade points (grade_points * credits) and credits for included records.
    4. Computes the resulting GPA:
       - None if no records are included.
       - 0.0 if total_credits == 0.0.
       - round(total_points / total_credits, 2) otherwise.

    Returns:
        dict:
            - 'records': list of dicts with assessment-level details and inclusion status.
            - 'total_points': float, aggregate numerator (sum of points * credits).
            - 'total_credits': float, aggregate denominator (sum of credits).
            - 'gpa': float or None, final rounded GPA.
    """
    seen_keys = set()
    records_breakdown = []
    total_points = 0.0
    total_credits = 0.0
    included_count = 0

    policy = resolve_policy(policy)

    for g in grades:
        score = g.get("score")
        max_score = g.get("max_score")
        key = assessment_record_key(g)
        c = _credit(g)

        # Calculate percentage, letter grade, and grade points
        try:
            pct_val = float(percentage(score, max_score)) if max_score > 0 else 0.0
            lg = letter_grade(score, max_score, policy=policy)
        except (ValueError, TypeError, KeyError):
            pct_val = 0.0
            lg = policy.failing_letter

        pts = policy.grade_points_for(lg)
        weighted_points = pts * c

        if key is not None and key in seen_keys:
            included = False
            exclusion_reason = "Duplicate assessment in calculation scope"
        else:
            if key is not None:
                seen_keys.add(key)
            included = True
            exclusion_reason = None
            total_points += weighted_points
            total_credits += c
            included_count += 1

        record_entry = {
            "record": g,
            "student_id": g.get("student_id"),
            "subject": g.get("subject"),
            "semester": str(g.get("semester", "")),
            "date": g.get("date", ""),
            "score": score,
            "max_score": max_score,
            "credits": c,
            "percentage": pct_val,
            "letter_grade": lg,
            "grade_points": pts,
            "weighted_points": weighted_points,
            "included": included,
            "exclusion_reason": exclusion_reason,
        }
        records_breakdown.append(record_entry)

    if included_count == 0:
        calculated_gpa = None
    elif total_credits == 0.0:
        calculated_gpa = 0.0
    else:
        calculated_gpa = policy.round_gpa(total_points / total_credits)

    return {
        "records": records_breakdown,
        "total_points": total_points,
        "total_credits": total_credits,
        "gpa": calculated_gpa,
        "policy": policy.name,
    }


def sgpa(grades, student_id=None, semester=None, policy=None):
    """Calculates Semester Grade Point Average on a 10.0 scale.

    Each distinct stored assessment in the semester contributes exactly once.
    Exact duplicate rows are collapsed, while same-day assessments with
    different grade data are retained.

    ``policy`` selects the grading policy; ``None`` uses the default legacy
    policy.

    Numerical policy: each grade point is classified from the unrounded
    percentage, weighted grade-point and credit totals are accumulated
    without rounding, and only the final average is rounded to 2 decimals.
    """
    if student_id is not None:
        grades = [g for g in grades if g.get("student_id") == student_id]
    if semester is not None:
        grades = [g for g in grades if str(g.get("semester", "")) == str(semester)]

    return calculate_gpa_breakdown(grades, policy=policy)["gpa"]


def cgpa(grades, student_id=None, policy=None):
    """Calculates Cumulative Grade Point Average on a 10.0 scale.

    Aggregates every distinct assessment across all of the student's
    semesters. A subject repeated in a later semester is a separate
    assessment (a legitimate retake) and is kept. Only exact repeated rows
    are collapsed; distinct same-day assessment results are retained.

    ``policy`` selects the grading policy; ``None`` uses the default legacy
    policy.

    Numerical policy: same as :func:`sgpa` — unrounded classification,
    unrounded intermediate totals, final result rounded to 2 decimals.
    """
    if student_id is not None:
        grades = [g for g in grades if g.get("student_id") == student_id]

    return calculate_gpa_breakdown(grades, policy=policy)["gpa"]


def explain_student_gpa(grades, student_id, semester=None, policy=None):
    """Generates structured GPA explanation for a student.

    ``policy`` selects the grading policy; ``None`` uses the default legacy
    policy.

    Returns a dict explaining:
      - student_id: ID of the student.
      - requested_semester: Filtered semester or None.
      - student_exists: Boolean whether any record exists for student_id.
      - semester_found: Boolean whether student has records in requested_semester (or True if semester is None).
      - semesters: Dict mapping semester name to its calculation breakdown.
      - cgpa: Cumulative GPA calculation breakdown across all student records.
    """
    student_records = [g for g in grades if g.get("student_id") == student_id]
    if not student_records:
        return {
            "student_id": student_id,
            "requested_semester": str(semester) if semester is not None else None,
            "student_exists": False,
            "semester_found": False,
            "semesters": {},
            "policy": resolve_policy(policy).name,
            "cgpa": {
                "records": [],
                "total_points": 0.0,
                "total_credits": 0.0,
                "gpa": None,
            },
        }

    # CGPA breakdown uses all student records
    cgpa_breakdown = calculate_gpa_breakdown(student_records, policy=policy)

    # Determine unique semesters preserving order of first appearance or sorted
    all_semesters = sorted(list(set(str(g.get("semester", "")) for g in student_records)))

    sem_filter_str = str(semester) if semester is not None else None
    if sem_filter_str is not None:
        semester_found = sem_filter_str in all_semesters
        semesters_to_explain = [sem_filter_str] if semester_found else []
    else:
        semester_found = True
        semesters_to_explain = all_semesters

    semesters_breakdown = {}
    for sem in semesters_to_explain:
        sem_records = [g for g in student_records if str(g.get("semester", "")) == sem]
        semesters_breakdown[sem] = calculate_gpa_breakdown(
            sem_records, policy=policy
        )

    return {
        "student_id": student_id,
        "requested_semester": sem_filter_str,
        "student_exists": True,
        "semester_found": semester_found,
        "semesters": semesters_breakdown,
        "policy": resolve_policy(policy).name,
        "cgpa": cgpa_breakdown,
    }


def subject_average(grades, subject):
    """Calculates the average percentage for a given subject.

    Each grade is converted to a full-precision percentage first (see
    :func:`percentage`), so marks out of different maximum scores compare
    fairly (35/50 and 70/100 are both 70%). Individual percentages are
    never rounded before averaging; only the final average is rounded to
    the existing output precision (2 decimals).

    Returns None if no entries exist for the subject.
    """
    matching = [
        g
        for g in grades
        if g.get("subject", "").strip().lower() == subject.strip().lower()
    ]
    if not matching:
        return None

    percentages = [percentage(g["score"], g["max_score"]) for g in matching]
    return round(float(sum(percentages) / len(percentages)), 2)


def calculate_target_sgpa(
    grades, student_id, target_cgpa, upcoming_credits, policy=None
):
    """Calculates required SGPA in upcoming credits to achieve a target CGPA.

    Derives the required performance using exact unrounded point totals:
        Required SGPA = (Target CGPA * (Current Credits + Upcoming Credits) - Current Points) / Upcoming Credits

    ``policy`` selects the grading policy; ``None`` uses the default legacy
    policy.  The maximum grade points of the policy define the perfect
    score used for the achievability check.

    Parameters:
        grades: List of grade dicts.
        student_id: ID of the student.
        target_cgpa: Float, target cumulative GPA (0.0 <= target_cgpa <= 10.0).
        upcoming_credits: Float, credits in the upcoming semester (> 0).

    Returns:
        dict:
            - student_id: str
            - student_exists: bool
            - current_cgpa: float or None
            - current_credits: float
            - current_points: float
            - target_cgpa: float
            - upcoming_credits: float
            - total_future_credits: float
            - required_sgpa: float or None
            - is_achievable: bool
            - max_possible_cgpa: float
    """
    if not math.isfinite(target_cgpa) or not (0.0 <= target_cgpa <= 10.0):
        raise ValueError("target_cgpa must be a finite number between 0.0 and 10.0")
    if not math.isfinite(upcoming_credits) or upcoming_credits <= 0.0:
        raise ValueError("upcoming_credits must be a finite number greater than 0")

    policy = resolve_policy(policy)
    max_grade_points = max(policy.grade_points.values()) if policy.grade_points else 10.0
    scale_max = max_grade_points

    student_records = [g for g in grades if g.get("student_id") == student_id]
    if not student_records:
        return {
            "student_id": student_id,
            "student_exists": False,
            "current_cgpa": None,
            "current_credits": 0.0,
            "current_points": 0.0,
            "target_cgpa": round(float(target_cgpa), 2),
            "upcoming_credits": round(float(upcoming_credits), 2),
            "total_future_credits": round(float(upcoming_credits), 2),
            "required_sgpa": round(float(target_cgpa), 2),
            "is_achievable": True,
            "max_possible_cgpa": scale_max,
            "policy": policy.name,
        }

    breakdown = calculate_gpa_breakdown(student_records, policy=policy)
    current_credits = breakdown["total_credits"]
    current_points = breakdown["total_points"]
    current_cgpa = breakdown["gpa"]

    total_future_credits = current_credits + upcoming_credits

    if current_credits == 0.0:
        return {
            "student_id": student_id,
            "student_exists": True,
            "current_cgpa": None,
            "current_credits": 0.0,
            "current_points": 0.0,
            "target_cgpa": round(float(target_cgpa), 2),
            "upcoming_credits": round(float(upcoming_credits), 2),
            "total_future_credits": round(float(total_future_credits), 2),
            "required_sgpa": round(float(target_cgpa), 2),
            "is_achievable": True,
            "max_possible_cgpa": scale_max,
            "policy": policy.name,
        }

    target_total_points = target_cgpa * total_future_credits
    needed_points = target_total_points - current_points
    raw_required_sgpa = needed_points / upcoming_credits

    max_possible_points = current_points + (scale_max * upcoming_credits)
    max_possible_cgpa = policy.round_gpa(max_possible_points / total_future_credits)

    if raw_required_sgpa > scale_max:
        is_achievable = False
        required_sgpa = policy.round_gpa(raw_required_sgpa)
    elif raw_required_sgpa <= 0.0:
        is_achievable = True
        required_sgpa = 0.0
    else:
        is_achievable = True
        required_sgpa = policy.round_gpa(raw_required_sgpa)

    return {
        "student_id": student_id,
        "student_exists": True,
        "current_cgpa": current_cgpa,
        "current_credits": current_credits,
        "current_points": current_points,
        "target_cgpa": round(float(target_cgpa), 2),
        "upcoming_credits": round(float(upcoming_credits), 2),
        "total_future_credits": round(float(total_future_credits), 2),
        "required_sgpa": required_sgpa,
        "is_achievable": is_achievable,
        "max_possible_cgpa": max_possible_cgpa,
        "policy": policy.name,
    }

