# grade-tracker

Keep track of marks, see letter grades and a GPA for each student, and export an HTML report. A command-line tool in plain Python with no dependencies. Marks are saved in `data/grades.csv`.

## Running it

You need Python 3.8 or newer.

```
python3 -m grade_tracker list
python3 -m grade_tracker summary CS101
python3 -m unittest discover tests
```

On Windows, use `python` instead of `python3`.

## Commands

| Command | Does |
|---|---|
| `list [--format json]` | Every mark, with its letter grade; `--format json` emits a JSON document |
| `summary <student_id> [--format json]` | One student's marks and GPA; `--format json` emits a JSON document |
| `average <subject> [--format json]` | The class average for a subject; `--format json` emits a JSON document |
| `add <student_id> <subject> <score> <max_score> <date> <credits> <semester>` | Adds a mark |
| `edit <student_id> <semester> <subject> <date> [--score N] [--max-score N] [--credits N] [--student-id ID] [--semester N] [--subject NAME] [--date YYYY-MM-DD]` | Edits one existing mark |
| `export [--output path]` | Writes an HTML report, `reports/index.html` by default |
| `validate [--format json]` | Audits the CSV for malformed or invalid records, without changing the file; `--format json` emits a JSON document |

`edit` corrects an existing record without touching the CSV by hand. The record is identified by its current assessment identity: `student_id`, `semester`, `subject`, and `date`. Only the options you pass are changed — `--score`, `--max-score`, `--credits`, `--student-id`, `--semester`, `--subject`, `--date` — and every omitted option keeps the record's current value (it is never replaced with a default). At least one change option is required.

The whole update runs as one transaction: the command takes the same cross-process lock `add` uses, re-reads the file, applies the change in memory, validates it with the same rules as `add` (score between 0 and `max_score`, `max_score` greater than 0, `date` in `YYYY-MM-DD`), checks that the resulting identity does not duplicate a *different* record, then writes a temporary file next to the CSV, flushes it, and atomically replaces the original. If the target is missing, several records match it (ambiguous), validation fails, or the write fails, the command exits 2 with an error on stderr and the file is left byte-for-byte unchanged. Retakes in a different semester or on a different date are still allowed, because they are different assessments.

`validate` is strictly read-only: it never rewrites, repairs, or deletes records, so file timestamps and bytes are untouched. Each problem is reported on stderr with its CSV row number (the header is row 1, the first data record is row 2), the affected field, and a reason. It exits 0 when the file is clean and 1 when any problem is found. Rows that `list`/`summary` would silently skip (a blank student ID or subject, a non-numeric score, and so on) are flagged here instead of being dropped. Legacy rows that simply omit the optional `credits` or `semester` columns are still accepted, matching the loader's documented defaults of 3.0 credits and semester 1.

## How it's supposed to work

- Grades go by percentage: 90 and above is A, 80–89 B, 70–79 C, 60–69 D, below 60 F.
- GPA is on a 4-point scale: A 4, B 3, C 2, D 1, F 0.
- A subject's average is the average percentage, so marks out of different totals compare fairly. 35/50 and 70/100 are both 70%.
- `add` refuses a score below 0 or above the maximum, a maximum of 0 or less, and a date that isn't `YYYY-MM-DD`.
- The report has a card for every student in the CSV, and only those.
- Everything in the report is shown as plain text. A subject name containing HTML is displayed, not run.

## Machine-readable JSON output

The read-only commands `list`, `summary`, `average`, and `validate` accept `--format json`. The default remains the existing human-readable text, so existing commands are unchanged. In JSON mode, stdout contains exactly one valid JSON document and nothing else; diagnostics and argparse errors go to stderr. Values that are not available are JSON `null`, not `"None"`, `"N/A"`, or similar strings. JSON output uses stable snake_case field names, arrays for collections, and JSON numbers for scores and averages.

```bash
python3 -m grade_tracker list --format json
python3 -m grade_tracker summary CS101 --format json
python3 -m grade_tracker average Mathematics --format json
python3 -m grade_tracker validate --format json
```

The `file` and `subject` or `student_id` values echo the CLI input exactly; records preserve CSV order and semester values are strings.

### `list` schema

```json
{"command":"list","count":2,"grades":[{"student_id":"CS101","subject":"Mathematics","score":95.0,"max_score":100.0,"date":"2025-01-15","credits":3.0,"semester":"1","percentage":95.0,"letter_grade":"A"}]}
```

`count` is the number of records in `grades`. Each record has string identifiers, numeric scores and credits, and a one-decimal display `percentage`. `letter_grade` uses the existing classification rules.

### `summary` schema

```json
{"command":"summary","student_id":"CS101","count":2,"cgpa":9.5,"semesters":[{"semester":"1","sgpa":9.5,"grades":[]}],"grades":[]}
```

`grades` contains every record for the student. `semesters` is ordered by semester and each item contains its `sgpa` and matching records. If the student has no records, `count` is 0, `cgpa` is `null`, and both arrays are empty.

### `average` schema

```json
{"command":"average","subject":"Mathematics","average":83.33,"count":3}
```

`average` is the existing rounded average percentage or `null` when no matching subject exists. `count` is the number of matching records.

### `validate` schema

```json
{"command":"validate","valid":true,"error_count":0,"errors":[],"file":"data/grades.csv"}
```

`valid` is `true` only when the existing audit found no problems. `errors` contains the same row-and-field diagnostic strings written to stderr by human-readable `validate`; `error_count` is its length. For example, an invalid score produces `"valid":false`, `"error_count":1`, and `"errors":["row 2, field score: 'abc' is not a numeric score"]`.

### JSON exit codes and failures

Successful `list`, `summary`, and `average` JSON commands exit 0, including empty result sets. `validate` exits 0 when valid and 1 when it finds one or more problems. Invalid `--format` values follow argparse's normal behavior: exit 2 with a usage error on stderr and no stdout.

Malformed CSV input is never converted into a successful empty JSON result for `list`, `summary`, or `average`. They emit a predictable JSON error document with `error.code` equal to `"input_error"` and exit 2; the error message identifies the row or structural problem and suggests running `validate`. A missing data file follows the existing loader contract and is represented as an empty dataset by those three commands; use `validate --format json` to distinguish that case, since validate reports a missing file and exits 1.

## Code

- `grade_tracker/calc.py`: letter grades, GPA, averages
- `grade_tracker/cli.py`: the commands, and reading and writing the CSV
- `grade_tracker/export.py`: the HTML report
- `tests/`: tests, run with `python3 -m unittest discover tests`

## Contributing

Fork the repo, make your changes on a new branch, and open a pull request. Run the tests first.

If you find a bug, open an issue with the steps to reproduce it, what you expected, and what happened instead.

Part of Source Start by CSI SPIT. MIT licensed.
