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
import re

# Percentages are compared against these thresholds exactly, with no
# epsilon tolerance: a value genuinely below a threshold stays below it.
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

    Used by BOTH the calculation layer (SGPA/CGPA dedup) and the persistence
    layer (duplicate rejection on write), so the two can never drift apart.

    A grade row records a result for one student, in one (semester), for one
    (subject) on one (date). score, max_score, and credits describe the
    result; they are NOT part of its identity, so a corrected score is still
    the same assessment. Two rows with the same
    (student_id, semester, subject, date) therefore describe the same
    academic record, and repeating one must not be counted twice.

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


def _dedupe_assessments(grades):
    """Keep the first row for each distinct academic record.

    Legacy, manually edited, or corrupted CSV files can contain the same
    record more than once (even though save_grade now rejects exact
    duplicates on write). Each distinct record must contribute exactly once
    to SGPA/CGPA, so repeats are collapsed here rather than counted again.

    A record with no subject is malformed: it has no identity under the
    schema, so it is never deduplicated (it is always kept). Inventing an
    identity from score/max_score would wrongly split a corrected record.
    Records from different semesters are never collapsed with each other.
    """
    seen = set()
    unique = []
    for g in grades:
        key = assessment_key(g)
        if key is None:
            unique.append(g)
            continue
        if key in seen:
            continue
        seen.add(key)
        unique.append(g)
    return unique


def _credit(grade):
    """Credits of a grade row as a float, falling back to the default of 1.0."""
    try:
        return float(grade.get("credits", 1.0))
    except (TypeError, ValueError):
        return 1.0


def validate_grade(score, max_score, date_value):
    """Validate a grade before it can be persisted.

    Raises:
        ValueError: If the score range or calendar date is invalid.
    """
    if max_score <= 0:
        raise ValueError("max_score must be greater than 0")
    if not 0 <= score <= max_score:
        raise ValueError("score must be between 0 and max_score")
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


def letter_grade(score, max_score=100):
    """Determines the letter grade corresponding to a score and max_score.

    Classification uses the unrounded percentage (see :func:`percentage`),
    never a rounded display percentage, and compares against the existing
    thresholds exactly — no epsilon tolerance is applied, so a value
    genuinely below a threshold keeps the lower grade.
    """
    if max_score <= 0:
        raise ValueError("max_score must be greater than 0")

    pct = percentage(score, max_score)

    for threshold, grade in GRADE_THRESHOLDS:
        if pct >= threshold:
            return grade
    return "F"


def calculate_gpa_breakdown(grades):
    """Calculates GPA breakdown and intermediate values for a pre-filtered list of grades.

    Processes records in input order:
    1. Evaluates percentage, letter grade, grade points, and credits for each record.
    2. Deduplicates records using assessment_key:
       - The first occurrence of an assessment key is included.
       - Subsequent occurrences of the same assessment key are excluded as duplicates.
       - Records with no determinate identity (assessment_key is None) are always included.
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

    for g in grades:
        score = g.get("score")
        max_score = g.get("max_score")
        key = assessment_key(g)
        c = _credit(g)

        # Calculate percentage, letter grade, and grade points
        try:
            pct_val = float(percentage(score, max_score)) if max_score > 0 else 0.0
            lg = letter_grade(score, max_score)
        except (ValueError, TypeError, KeyError):
            pct_val = 0.0
            lg = "F"

        pts = GRADE_POINTS.get(lg, 0.0)
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
        calculated_gpa = round(total_points / total_credits, 2)

    return {
        "records": records_breakdown,
        "total_points": total_points,
        "total_credits": total_credits,
        "gpa": calculated_gpa,
    }


def sgpa(grades, student_id=None, semester=None):
    """Calculates Semester Grade Point Average on a 10.0 scale.

    Each distinct assessment (student_id, semester, subject, date) in the
    semester contributes exactly once. Duplicate rows for the same assessment
    — possible in legacy or hand-edited CSV files — are collapsed so a
    subject cannot be double-counted within one semester.

    Numerical policy: each grade point is classified from the unrounded
    percentage, weighted grade-point and credit totals are accumulated
    without rounding, and only the final average is rounded to 2 decimals.
    """
    if student_id is not None:
        grades = [g for g in grades if g.get("student_id") == student_id]
    if semester is not None:
        grades = [g for g in grades if str(g.get("semester", "")) == str(semester)]

    return calculate_gpa_breakdown(grades)["gpa"]


def cgpa(grades, student_id=None):
    """Calculates Cumulative Grade Point Average on a 10.0 scale.

    Aggregates every distinct assessment across all of the student's
    semesters. A subject repeated in a later semester is a separate
    assessment (a legitimate retake) and is kept; only rows that repeat the
    same (student_id, semester, subject, date) assessment are collapsed, so
    duplicate records cannot inflate the CGPA.

    Numerical policy: same as :func:`sgpa` — unrounded classification,
    unrounded intermediate totals, final result rounded to 2 decimals.
    """
    if student_id is not None:
        grades = [g for g in grades if g.get("student_id") == student_id]

    return calculate_gpa_breakdown(grades)["gpa"]


def explain_student_gpa(grades, student_id, semester=None):
    """Generates structured GPA explanation for a student.

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
            "cgpa": {
                "records": [],
                "total_points": 0.0,
                "total_credits": 0.0,
                "gpa": None,
            },
        }

    # CGPA breakdown uses all student records
    cgpa_breakdown = calculate_gpa_breakdown(student_records)

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
        semesters_breakdown[sem] = calculate_gpa_breakdown(sem_records)

    return {
        "student_id": student_id,
        "requested_semester": sem_filter_str,
        "student_exists": True,
        "semester_found": semester_found,
        "semesters": semesters_breakdown,
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
