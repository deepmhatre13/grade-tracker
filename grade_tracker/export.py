"""HTML report generation module for Grade Tracker."""

from pathlib import Path
from grade_tracker.calc import cgpa, letter_grade, percentage, sgpa
from grade_tracker.policies import resolve_policy
from html import escape

def get_enrolled_students(grades):
    """Returns a sorted list of all enrolled student IDs present in grades."""
    students = {
        row["student_id"]
        for row in grades
        if row.get("student_id")
    }
    return sorted(list(students))


def _esc(value):
    """Escape a single user-controlled value for HTML text context."""
    return escape(str(value))


def _esc_attr(value):
    """Escape a single user-controlled value for a quoted HTML attribute."""
    return escape(str(value), quote=True)


def build_html(grades, output_path="reports/index.html", policy=None):
    """Generates an HTML report summarizing student grades and CGPA.

    ``policy`` selects the grading policy used for letter grades and GPAs;
    ``None`` uses the default legacy policy.  The active policy is recorded
    in the report so a reader knows which rules produced the numbers.
    """
    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    policy = resolve_policy(policy)
    student_ids = get_enrolled_students(grades)
    cards = []

    for sid in student_ids:
        student_grades = [g for g in grades if g.get("student_id") == sid]
        student_cgpa = cgpa(grades, student_id=sid, policy=policy)

        semesters = sorted(list(set(g.get("semester", "1") for g in student_grades)))
        rows = []
        for sem in semesters:
            sem_grades = [g for g in student_grades if g.get("semester", "1") == sem]
            sem_sgpa = sgpa(grades, student_id=sid, semester=sem, policy=policy)
            
            rows.append(f"<tr class='semester-row'><td colspan='5'>Semester {_esc(sem)} (SGPA: {_esc(sem_sgpa)})</td></tr>")
            
            for g in sem_grades:
                grade_char = letter_grade(g["score"], g["max_score"], policy=policy)
                # Display-only rounding (1 decimal); the grade above is decided
                # from the unrounded percentage, never from this value.
                pct = float(percentage(g["score"], g["max_score"])) if g["max_score"] > 0 else 0
                rows.append(
                    f"<tr><td>{_esc(g['subject'])}</td><td>{_esc_attr(g.get('credits', 3.0))}</td><td>{_esc(g['score'])} / {_esc(g['max_score'])} ({pct:.1f}%)</td>"
                    f"<td><span class='badge'>{grade_char}</span></td><td>{_esc(g.get('date', ''))}</td></tr>"
                )

        rows_html = "".join(rows) if rows else "<tr><td colspan='5' class='empty'>No records found</td></tr>"

        card = f"""
        <div class="card">
            <div class="card-header">
                <h2>Student: {_esc(sid)}</h2>
                <span class="gpa-tag">CGPA: {_esc(student_cgpa)}</span>
            </div>
            <table>
                <thead>
                    <tr><th>Subject</th><th>Credits</th><th>Score</th><th>Grade</th><th>Date</th></tr>
                </thead>
                <tbody>
                    {rows_html}
                </tbody>
            </table>
        </div>
        """
        cards.append(card)

    cards_html = "\n".join(cards)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Academic Performance Report - Grade Tracker</title>
    <style>
        :root {{
            --bg: #0f172a;
            --surface: #1e293b;
            --surface-border: #334155;
            --text: #f8fafc;
            --text-muted: #94a3b8;
            --primary: #38bdf8;
            --accent: #818cf8;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: var(--bg);
            color: var(--text);
            padding: 2rem;
            line-height: 1.5;
        }}
        header {{
            max-width: 900px;
            margin: 0 auto 2rem auto;
            text-align: center;
        }}
        header h1 {{
            font-size: 2.2rem;
            background: linear-gradient(135deg, var(--primary), var(--accent));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 0.5rem;
        }}
        header p {{ color: var(--text-muted); }}
        .grid {{
            max-width: 900px;
            margin: 0 auto;
            display: grid;
            gap: 1.5rem;
        }}
        .card {{
            background: var(--surface);
            border: 1px solid var(--surface-border);
            border-radius: 12px;
            padding: 1.5rem;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2);
        }}
        .card-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1rem;
            border-bottom: 1px solid var(--surface-border);
            padding-bottom: 0.75rem;
        }}
        .card-header h2 {{ font-size: 1.25rem; font-weight: 600; }}
        .gpa-tag {{
            background: rgba(56, 189, 248, 0.15);
            color: var(--primary);
            padding: 0.25rem 0.75rem;
            border-radius: 9999px;
            font-weight: 600;
            font-size: 0.9rem;
            border: 1px solid rgba(56, 189, 248, 0.3);
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.95rem;
        }}
        th, td {{
            text-align: left;
            padding: 0.6rem 0.75rem;
            border-bottom: 1px solid var(--surface-border);
        }}
        th {{ color: var(--text-muted); font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.05em; }}
        .semester-row td {{
            background-color: rgba(56, 189, 248, 0.05);
            font-weight: 600;
            color: var(--primary);
            padding: 0.4rem 0.75rem;
        }}
        .badge {{
            display: inline-block;
            padding: 0.2rem 0.5rem;
            border-radius: 4px;
            font-weight: bold;
            background: #334155;
        }}
        .empty {{
            text-align: center;
            color: var(--text-muted);
            font-style: italic;
            padding: 1rem;
        }}
    </style>
</head>
<body>
    <header>
        <h1>Academic Performance Report</h1>
        <p>Generated by Grade Tracker</p>
        <p class="policy">Grading policy: {_esc(policy.name)}</p>
    </header>
    <div class="grid">
        {cards_html}
    </div>
</body>
</html>
"""

    with open(out_file, "w", encoding="utf-8") as f:
        f.write(html_content)

    return str(out_file)
