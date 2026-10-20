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
| `explain <student_id> [--semester N] [--format json]` | Explains how assessment marks contribute to a student's SGPA and CGPA; `--format json` emits a JSON document |
| `average <subject> [--format json]` | The class average for a subject; `--format json` emits a JSON document |
| `add <student_id> <subject> <score> <max_score> <date> <credits> <semester> [--allow-duplicate]` | Adds a mark; opt in when recording a separate same-day assessment |
| `edit <student_id> <semester> <subject> <date> [--score N] [--max-score N] [--credits N] [--student-id ID] [--semester N] [--subject NAME] [--date YYYY-MM-DD]` | Edits one existing mark |
| `export [--output path]` | Writes an HTML report, `reports/index.html` by default |
| `validate [--format json]` | Audits the CSV for malformed or invalid records, without changing the file; `--format json` emits a JSON document |
| `backup` | Creates a verified, timestamped backup of the CSV with SHA-256 metadata |
| `restore <backup_path>` | Verifies and restores a backup to the grade data file |
| `transactions [--status {PREPARED,COMMITTING,COMMITTED,ABORTED}] [--format json]` | Inspects write-ahead transaction journal status and history |


`edit` corrects an existing record without touching the CSV by hand. The record is identified by its current assessment identity: `student_id`, `semester`, `subject`, and `date`. Only the options you pass are changed — `--score`, `--max-score`, `--credits`, `--student-id`, `--semester`, `--subject`, `--date` — and every omitted option keeps the record's current value (it is never replaced with a default). At least one change option is required.

The whole update runs as one transaction: the command takes the same cross-process lock `add` uses, re-reads the file, applies the change in memory, validates it with the same rules as `add` (score between 0 and `max_score`, `max_score` greater than 0, `date` in `YYYY-MM-DD`), checks that the resulting identity does not duplicate a *different* record, then writes a temporary file next to the CSV, flushes it, and atomically replaces the original. If the target is missing, several records match it (ambiguous), validation fails, or the write fails, the command exits 2 with an error on stderr and the file is left byte-for-byte unchanged. Retakes in a different semester or on a different date are still allowed, because they are different assessments.

`add` normally rejects a second row with the same student, semester, subject,
and date so a corrected score cannot accidentally become another assessment;
use `--allow-duplicate` only when the row is a genuinely separate same-day
assessment. The authoritative identity remains those four fields for editing
and corrections. GPA calculations and validation compare the complete stored
grade data so distinct opted-in assessments count independently, while exact
repeated rows are still rejected and counted only once.

`validate` is strictly read-only: it never rewrites, repairs, or deletes records, so file timestamps and bytes are untouched. Each problem is reported on stderr with its CSV row number (the header is row 1, the first data record is row 2), the affected field, and a reason. It exits 0 when the file is clean and 1 when any problem is found. Rows that `list`/`summary` would silently skip (a blank student ID or subject, a non-numeric score, and so on) are flagged here instead of being dropped. Legacy rows that simply omit the optional `credits` or `semester` columns are still accepted, matching the loader's documented defaults of 3.0 credits and semester 1.

`backup` creates a consistent, verified snapshot of the active grade data:
- Backups are stored in a `backups/` directory adjacent to the data CSV (e.g. `data/backups/`).
- Filenames follow the timestamped convention `grades-YYYYMMDDTHHMMSSZ.csv` and are accompanied by a JSON manifest `grades-YYYYMMDDTHHMMSSZ.json`. If multiple backups occur within the same second, an incrementing numerical suffix (`-1`, `-2`, ...) ensures uniqueness without overwriting.
- The manifest records `format_version` ("1.0"), `schema_version` ("1.0"), UTC creation time `created_at` (ISO 8601), `source_file`, `bytes`, record count, canonical CSV `header`, and the exact `sha256` digest of the backup CSV.
- Backup acquisition coordinates under the target CSV's advisory inter-process lock (`_InterProcessLock`), ensuring no backup captures a partially written or concurrent mutation.
- The source data file is audited (`audit_grades_csv`) before backup generation; if the file is missing or invalid, backup aborts with exit code 1 and no backup artifact is left behind.

`restore <backup_path>` restores a backup to the configured destination data CSV:
- `<backup_path>` may point either to the backup CSV or its companion JSON manifest.
- Before modifying the destination, the command:
  1. Validates manifest existence, syntax, supported format version ("1.0"), and schema version ("1.0").
  2. Verifies the backup data file's SHA-256 checksum matches the manifest digest.
  3. Validates the header and audits all records using `audit_grades_csv`.
  4. Prevents restoring a backup directly over its own source files.
- It then acquires the destination file's advisory inter-process lock, re-verifies destination collision and checksum integrity under lock, writes restored contents to a temporary file in the destination directory, flushes and `fsync`s the temporary file, and atomically replaces the destination (`os.replace`).
- Temporary files are cleaned up on any failure, leaving the original destination byte-for-byte unchanged.
- If the destination does not yet exist, its directory structure is created and the file is safely established.
- Concurrency & atomicity limitations: Advisory locks protect coordinating processes using this CLI. Durability is bounded by the underlying OS filesystem guarantees.

### Crash-Recoverable Transaction Journal

All grade mutation operations (`add`, `edit`, and `import`) execute under a write-ahead transaction journal coordinator to guarantee atomic, durable, and crash-recoverable state transitions across OS crashes or power interruptions.

#### Architecture and Storage Layout
- Transaction journals are stored in a dedicated directory adjacent to the data CSV named `.tx_journal_<csv_filename>` (for example, `data/.tx_journal_grades.csv/`).
- Staged mutation files are written directly in the same filesystem directory using the naming convention `.tx_stage_<tx_id>.tmp`.
- Journal entries are stored as `<tx_id>.json` files using JSON format version `1.0`. Journal entries strictly record transaction metadata and cryptographic hashes without duplicating raw student grade data.

#### Transaction Journal Schema (`version: "1.0"`)
| Field | Type | Description |
|---|---|---|
| `version` | string | Format version (`"1.0"`) |
| `tx_id` | string | Canonical UUID identifying the mutation |
| `mutation_type` | string | Operation type (`"add"`, `"edit"`, or `"import"`) |
| `target_csv` | string | Absolute path to the destination CSV |
| `source_checksum` | string \| null | SHA-256 hex digest of the target CSV before mutation (`null` if newly created) |
| `target_checksum` | string | SHA-256 hex digest of the staged post-mutation file |
| `staged_filename` | string | Basename of the staged temporary file in the target directory |
| `state` | string | Current transaction state: `PREPARED`, `COMMITTING`, `COMMITTED`, or `ABORTED` |
| `created_at` | string | ISO 8601 UTC timestamp of transaction creation |
| `updated_at` | string | ISO 8601 UTC timestamp of last state transition |

#### Write-Ahead Commit Protocol
1. **Lock Acquisition**: Acquire the target CSV's advisory inter-process lock (`_InterProcessLock`).
2. **Crash Recovery**: Run pending transaction recovery before reading or applying mutations.
3. **Source Checksum**: Compute initial SHA-256 hash of the target CSV and verify existing schema.
4. **Staging & Fsync**: Write mutated records to a temporary file (`.tx_stage_<tx_id>.tmp`), flush buffers, and invoke `os.fsync`.
5. **Staged Audit**: Audit staged file integrity (`audit_grades_csv`) prior to commit.
6. **PREPARED State**: Write and fsync `<tx_id>.json` in `PREPARED` state with `source_checksum` and `target_checksum`.
7. **COMMITTING State**: Write and fsync `<tx_id>.json` in `COMMITTING` state.
8. **Atomic Replacement**: Atomically replace the destination CSV (`os.replace`) with the staged file.
9. **Verification**: Verify target CSV checksum matches `target_checksum`.
10. **COMMITTED State**: Write and fsync `<tx_id>.json` in `COMMITTED` state.
11. **Pruning**: Prune oldest completed journals exceeding retention limit (`retention_limit=50`).

#### Recovery Decision Rules
When a mutation process recovers pending transactions under lock:
- **Case A (Replace Succeeded)**: Target dataset checksum matches `target_checksum`. The atomic replacement succeeded prior to crash; journal is finalized as `COMMITTED` and staged file is cleaned up.
- **Case B (Prepared Rollback)**: Target dataset checksum matches `source_checksum` and state is `PREPARED`. The replacement did not occur; journal is marked `ABORTED` and the staged file is removed.
- **Case C (Committing Replay)**: Target dataset checksum matches `source_checksum` and state is `COMMITTING`. If the staged file exists, matches `target_checksum`, and passes audit, recovery completes the atomic replacement, verifies checksum, and marks `COMMITTED`. If staged file is missing or invalid, recovery fails closed as ambiguous.
- **Case D (Checksum Mismatch)**: Target dataset checksum matches neither `source_checksum` nor `target_checksum`. Fails closed as ambiguous; manual operator intervention required without altering files.
- **Case E (Malformed Journal)**: Corrupt JSON, invalid UUID, or path traversal detected. Fails closed as ambiguous; manual operator intervention required.

#### Inspection Command (`transactions`)
Inspect write-ahead transaction journal status and history without acquiring locks or altering data files:
```bash
python3 -m grade_tracker transactions
python3 -m grade_tracker transactions --status PREPARED
python3 -m grade_tracker transactions --format json
```


## How it's supposed to work

- Grades go by percentage: 90 and above is A, 80–89 B, 70–79 C, 60–69 D, below 60 F.
- GPA is on a 4-point scale: A 4, B 3, C 2, D 1, F 0.
- A subject's average is the average percentage, so marks out of different totals compare fairly. 35/50 and 70/100 are both 70%.
- `add`, `edit`, and `import` refuse negative or non-finite credits. Grade writes
  also reject a score below 0 or above the maximum, a maximum of 0 or less, and
  a date that isn't `YYYY-MM-DD`.
- The report has a card for every student in the CSV, and only those.
- Everything in the report is shown as plain text. A subject name containing HTML is displayed, not run.

## Explaining GPA Calculations

The `explain` command breaks down how each individual assessment contributes to a student's SGPA (Semester Grade Point Average) and CGPA (Cumulative Grade Point Average). It exposes the authoritative calculation layer's intermediate inputs, weighted contributions, deduplication inclusion decisions, and aggregate numerators and denominators.

```bash
python3 -m grade_tracker explain CS101
python3 -m grade_tracker explain CS101 --semester 2
python3 -m grade_tracker explain CS101 --format json
```

### Assessment Fields
- **Subject**: Course or subject name.
- **Score / Max**: Score earned and maximum possible score.
- **Pct**: Calculated percentage (`Decimal(score) / Decimal(max_score) * 100`).
- **Grade**: Letter grade assigned (`A`: >=90%, `B`: >=80%, `C`: >=70%, `D`: >=60%, `F`: <60%).
- **Pts**: Grade point value (`A`=10.0, `B`=9.0, `C`=8.0, `D`=7.0, `F`=0.0).
- **Cr**: Credit weight assigned to the assessment (defaults to 3.0 on legacy rows).
- **W.Pts**: Weighted contribution to the GPA numerator (`Pts * Cr`).
- **Status**: Record inclusion status (`Included` or `Excluded (<reason>)`).

### Calculation Semantics and Rules
- **Assessment Identity & Deduplication**: Distinct assessments are identified by `(student_id, semester, subject.casefold(), date)`. Two different subjects on the same date are distinct assessments. A repeat of the same assessment identity in the same semester is excluded as a duplicate, and only the first occurrence is counted. Legitimate retakes in different semesters have distinct identities and are both counted.
- **SGPA & CGPA Aggregates**:
  - Numerator: Sum of `Pts * Cr` across all included records.
  - Denominator: Sum of `Cr` across all included records.
  - Result: `round(Numerator / Denominator, 2)`. If Denominator is 0.0, GPA is 0.00. If no valid records exist, GPA is reported as unavailable.
- **Semester Filtering**: When `--semester <sem>` is provided, only the assessments and SGPA for that semester are displayed, but the overall CGPA continues to be computed across all of the student's records across all semesters.
- **Missing Students vs Missing Semesters**: A student not present in the CSV is reported as not found (`No records found for student '<id>.'`). A student who exists but has no records for the filtered semester is clearly distinguished (`No records found for student '<id>' in semester <sem>.`).
- **Read-Only**: The command never modifies or rewrites the underlying CSV.

## Machine-readable JSON output

The read-only commands `list`, `summary`, `explain`, `average`, and `validate` accept `--format json`. The default remains the existing human-readable text, so existing commands are unchanged. In JSON mode, stdout contains exactly one valid JSON document and nothing else; diagnostics and argparse errors go to stderr. Values that are not available are JSON `null`, not `"None"`, `"N/A"`, or similar strings. JSON output uses stable snake_case field names, arrays for collections, and JSON numbers for scores and averages.

```bash
python3 -m grade_tracker list --format json
python3 -m grade_tracker summary CS101 --format json
python3 -m grade_tracker explain CS101 --format json
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

### `explain` schema

```json
{"cgpa":{"denominator":19.0,"gpa":9.47,"numerator":180.0},"command":"explain","semester":null,"semester_found":true,"semesters":[{"denominator":12.0,"numerator":114.0,"records":[{"credits":3.0,"date":"2025-01-15","exclusion_reason":null,"grade_points":10.0,"included":true,"letter_grade":"A","max_score":100.0,"percentage":95.0,"score":95.0,"semester":"1","student_id":"CS101","subject":"Mathematics","weighted_points":30.0}],"semester":"1","sgpa":9.5}],"student_exists":true,"student_id":"CS101"}
```

`student_exists` and `semester_found` boolean flags clearly differentiate missing students and absent semesters. `cgpa` exposes the overall cumulative numerator, denominator, and GPA across all student records. `semesters` contains semester-by-semester SGPA calculations and per-assessment records with `included` and `exclusion_reason`.

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

Successful `list`, `summary`, `explain`, and `average` JSON commands exit 0, including empty result sets. `validate` exits 0 when valid and 1 when it finds one or more problems. Invalid `--format` values follow argparse's normal behavior: exit 2 with a usage error on stderr and no stdout.

Malformed CSV input is never converted into a successful empty JSON result for `list`, `summary`, `explain`, or `average`. They emit a predictable JSON error document with `error.code` equal to `"input_error"` and exit 2; the error message identifies the row or structural problem and suggests running `validate`. A missing data file follows the existing loader contract and is represented as an empty dataset by those commands; use `validate --format json` to distinguish that case, since validate reports a missing file and exits 1.

## Code

- `grade_tracker/calc.py`: letter grades, GPA, averages
- `grade_tracker/cli.py`: the commands, and reading and writing the CSV
- `grade_tracker/export.py`: the HTML report
- `tests/`: tests, run with `python3 -m unittest discover tests`

## Contributing

Fork the repo, make your changes on a new branch, and open a pull request. Run the tests first.

If you find a bug, open an issue with the steps to reproduce it, what you expected, and what happened instead.

Part of Source Start by CSI SPIT. MIT licensed.
