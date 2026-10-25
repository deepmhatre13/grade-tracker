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
| `list [--format json] [--policy NAME]` | Every mark, with its letter grade; `--format json` emits a JSON document |
| `summary <student_id> [--format json] [--policy NAME]` | One student's marks and GPA; `--format json` emits a JSON document |
| `explain <student_id> [--semester N] [--format json] [--policy NAME]` | Explains how assessment marks contribute to a student's SGPA and CGPA; `--format json` emits a JSON document |
| `target-gpa <student_id> --target N --credits N [--format json] [--policy NAME]` | Calculates required SGPA in upcoming credits to achieve a target CGPA; `--format json` emits a JSON document |
| `average <subject> [--format json]` | The class average for a subject; `--format json` emits a JSON document |
| `policies [--format json]` | Lists the registered grading policies; `--format json` emits a JSON document |
| `add <student_id> <subject> <score> <max_score> <date> <credits> <semester> [--allow-duplicate]` | Adds a mark; opt in when recording a separate same-day assessment |

| `edit <student_id> <semester> <subject> <date> [--score N] [--max-score N] [--credits N] [--student-id ID] [--semester N] [--subject NAME] [--date YYYY-MM-DD]` | Edits one existing mark |
| `delete [--student-id ID] [--semester N] [--subject NAME] [--date YYYY-MM-DD] [--dry-run] [--yes]` | Bulk deletes matching grade records under inter-process lock; requires confirmation or dry-run |

| `edit <student_id> <semester> <subject> <date> [--current-score N] [--current-max-score N] [--current-credits N] [--score N] [--max-score N] [--credits N] [--student-id ID] [--semester N] [--subject NAME] [--date YYYY-MM-DD]` | Edits one existing mark; current-value selectors distinguish same-day assessments |

| `export [--output path] [--policy NAME]` | Writes an HTML report, `reports/index.html` by default |
| `validate [--format json]` | Audits the CSV for malformed or invalid records, without changing the file; `--format json` emits a JSON document |
| `backup` | Creates a verified, timestamped backup of the CSV with SHA-256 metadata |
| `restore <backup_path>` | Verifies and restores a backup to the grade data file |



`delete` performs transactional bulk deletion of grade records matching explicit selection criteria:
- **Selectors**: Pass at least one of `--student-id`, `--semester`, `--subject`, or `--date`. Requests with no selectors are rejected to prevent accidental deletion of all records.
- **AND Semantics**: When multiple selectors are provided, only records satisfying *all* selectors are matched.
- **Normalization & Matching**: Student ID and semester match exactly (stripped). Subject matching is case-insensitive. Date matching requires valid `YYYY-MM-DD` calendar dates.
- **Dry-run Preview (`--dry-run`)**: Previews matching records without modifying the CSV. The destination file remains byte-for-byte untouched and no temporary files are created.
- **Confirmation Requirement (`--yes` / `-y`)**: Destructive deletion requires explicit confirmation via `--yes`. If omitted, the command aborts with exit code 2 and leaves the file untouched.
- **Zero Matches**: When no records match, the command reports 0 matches and exits cleanly without rewriting or modifying the file.
- **Transactionality & Concurrency**: Deletion coordinates under the destination CSV's advisory inter-process lock (`_InterProcessLock`). The file is audited for integrity (`audit_grades_csv`), read, and filtered under lock. Remaining records are written to a temporary file in the destination directory, flushed and `fsync`ed, and atomically replaced (`os.replace`). Order and contents of non-deleted records are strictly preserved.
- **Exit Codes & Errors**: Exits 0 on successful deletion, dry run, or 0 matches. Exits 2 on invalid selectors, missing confirmation, corrupt CSV data, or I/O errors with diagnostics printed to `stderr`.


`edit` corrects an existing record without touching the CSV by hand. The record is identified by its current assessment identity: `student_id`, `semester`, `subject`, and `date`. Only the options you pass are changed — `--score`, `--max-score`, `--credits`, `--student-id`, `--semester`, `--subject`, `--date` — and every omitted option keeps the record's current value (it is never replaced with a default). At least one change option is required.

`edit` corrects an existing record without touching the CSV by hand. The record is identified by its current assessment identity: `student_id`, `semester`, `subject`, and `date`. If multiple same-day assessments share that identity, add one or more pre-edit selectors (`--current-score`, `--current-max-score`, or `--current-credits`) until exactly one record matches. Only the change options you pass are modified — `--score`, `--max-score`, `--credits`, `--student-id`, `--semester`, `--subject`, `--date` — and every omitted option keeps the record's current value (it is never replaced with a default). At least one change option is required.


The whole update runs as one transaction: the command takes the same cross-process lock `add` uses, re-reads the file, applies the selectors to the current values, updates exactly one record in memory, and validates it with the same rules as `add` (score between 0 and `max_score`, `max_score` greater than 0, `date` in `YYYY-MM-DD`). Value-only edits within an existing same-day group reject only a resulting full-record duplicate; moving a record to a different identity retains the conservative identity-conflict check. It then writes a temporary file next to the CSV, flushes it, and atomically replaces the original. If the target is missing, the supplied selectors are still ambiguous, validation fails, or the write fails, the command exits 2 with an error on stderr and the file is left byte-for-byte unchanged. Retakes and distinct same-day assessments remain allowed.

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

## How it's supposed to work

- Grades go by percentage: 90 and above is A, 80–89 B, 70–79 C, 60–69 D, below 60 F.
- GPA is on a 10-point scale: A 10, B 9, C 8, D 7, F 0. These thresholds and
  grade points come from the default grading policy (`legacy@1`); see
  [Grading policies](#grading-policies) to use a different one.
- A subject's average is the average percentage, so marks out of different totals compare fairly. 35/50 and 70/100 are both 70%.
- `add`, `edit`, and `import` refuse negative or non-finite credits. Grade writes
  also reject a score below 0 or above the maximum, a maximum of 0 or less, and
  a date that isn't `YYYY-MM-DD`.
- The report has a card for every student in the CSV, and only those.
- Everything in the report is shown as plain text. A subject name containing HTML is displayed, not run.

## Grading policies

Every grade calculation runs under a *grading policy*: a versioned, immutable
bundle of letter-grade thresholds, grade points, and rounding rules.  The
default policy, `legacy@1`, reproduces exactly the behaviour Grade Tracker
had before policies existed, so every command keeps its current results
unless you ask for a different policy.

```
python3 -m grade_tracker policies
python3 -m grade_tracker policies --format json
python3 -m grade_tracker summary CS101 --policy legacy@1
```

- `--policy` accepts a bare identifier (`strict`, which resolves to the
  highest registered version) or an exact `identifier@version` name
  (`strict@1`).
- `--policy` is available on `list`, `summary`, `explain`, `target-gpa`, and
  `export`.
- Selecting a policy that is not registered is an error, not a silent
  fallback to the default.  Human output prints the reason on stderr and
  exits 1; `--format json` emits an `unknown_policy` error document.
- JSON output from `list`, `summary`, `explain`, and `target-gpa` includes a
  `policy` field naming the policy that produced the numbers, and the HTML
  report records the active policy in its header.

### The default policy

| Policy | Thresholds (inclusive) | Grade points | GPA rounding |
|---|---|---|---|
| `legacy@1` | A >= 90, B >= 80, C >= 70, D >= 60, F below 60 | A 10, B 9, C 8, D 7, F 0 | 2 decimals |

A score exactly on a threshold receives that threshold's letter grade, and
classification always uses the unrounded percentage.

### Defining your own policy

```python
from grade_tracker.policies import Policy, register_policy

strict = Policy(
    identifier="strict",
    version="1",
    thresholds=((95, "A"), (85, "B"), (75, "C"), (65, "D")),
    grade_points={"A": 10.0, "B": 9.0, "C": 8.0, "D": 7.0, "F": 0.0},
    description="Stricter thresholds for a honours programme.",
)
register_policy(strict)
```

- Thresholds must be strictly descending and each letter grade must have
  grade points.
- `gpa_rounding_mode` is one of `half_up`, `half_even`, or `python`.
  `python` reproduces Python's built-in `round()`, which is what the default
  policy uses.
- `additional_rules` takes callables that receive the unrounded percentage
  and the policy, and may return a letter grade to override the thresholds.
- A policy identity is `(identifier, version)`.  Registering the same pair
  twice raises `DuplicatePolicyError`, so a published version can never be
  silently redefined; publish a new version instead.
- The calculation API also accepts a policy directly:
  `cgpa(grades, "CS101", policy="strict@1")`, or a `Policy` instance.

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

The `target-gpa` command calculates the exact Semester Grade Point Average (SGPA) a student needs in an upcoming semester to reach a target Cumulative GPA (CGPA):

```bash
python3 -m grade_tracker target-gpa CS101 --target 9.6 --credits 10
python3 -m grade_tracker target-gpa CS101 --target 9.6 --credits 10 --format json
```

If the requested target is mathematically impossible within the upcoming credit budget (required SGPA > 10.0), the command explains that clearly and reports the maximum possible CGPA achievable with a perfect 10.00 SGPA.

## Machine-readable JSON output

The read-only commands `list`, `summary`, `explain`, `target-gpa`, `average`, and `validate` accept `--format json`. The default remains the existing human-readable text, so existing commands are unchanged. In JSON mode, stdout contains exactly one valid JSON document and nothing else; diagnostics and argparse errors go to stderr. Values that are not available are JSON `null`, not `"None"`, `"N/A"`, or similar strings. JSON output uses stable snake_case field names, arrays for collections, and JSON numbers for scores and averages.

```bash
python3 -m grade_tracker list --format json
python3 -m grade_tracker summary CS101 --format json
python3 -m grade_tracker explain CS101 --format json
python3 -m grade_tracker target-gpa CS101 --target 9.6 --credits 10 --format json
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
- `grade_tracker/audit_log.py`: versioned audit event schema, canonical serialization, and SHA-256 hash-chain verification
- `grade_tracker/audit_txn.py`: crash-recoverable write-ahead transaction journal coordinating CSV mutations and audit logs
- `grade_tracker/cli.py`: the commands, and reading and writing the CSV
- `grade_tracker/export.py`: the HTML report
- `tests/`: tests, run with `python3 -m unittest discover tests`

## Tamper-evident audit history

Grade mutations (`add`, `edit`, `delete`) record an immutable, tamper-evident audit history alongside the grade data.

### Storage and naming

- **Audit log**: Stored adjacent to the data CSV as `<csv_stem>.audit.jsonl` (e.g. `data/grades.audit.jsonl`), derived via `audit_log_path_for(csv_path)`. Each committed mutation appends exactly one JSON object line (JSON Lines format).
- **Pending journal**: Stored as `<csv_stem>.audit-pending.json` (e.g. `data/grades.audit-pending.json`), derived via `pending_journal_path_for(csv_path)`. This temporary write-ahead journal coordinates two-phase crash recovery and is cleared upon successful commit.
- **Dataset identifier**: Every event records a `dataset_id` string formatted as `grades:<sha256[:16]>`, deterministically derived from the resolved absolute CSV path via `dataset_id_for(csv_path)`.

### Event schema and SHA-256 hash chain

Audit records follow schema version `1.0` (`AUDIT_SCHEMA_VERSION = "1.0"`). Every event contains twelve standardized fields:

1. `schema_version`: Audit format version (`"1.0"`).
2. `event_id`: Unique UUID4 identifier (`str(uuid.uuid4())`).
3. `sequence`: Strictly increasing 1-based integer sequence (`1, 2, 3, ...`).
4. `operation`: Operation name (`add`, `edit`, `import`, `delete`, `restore`, or `migrate`).
5. `timestamp`: UTC ISO 8601 timestamp with microsecond resolution (`YYYY-MM-DDTHH:MM:SS.ffffffZ`).
6. `dataset_id`: Stable identifier of the target CSV file.
7. `records`: List of affected academic records (each specifying `student_id`, `semester`, `subject`, `date`, `score`, `max_score`, `credits`).
8. `changes`: Mapping with `"before"` and `"after"` representations of the modified record(s).
9. `actor`: Identity of the mutating actor, or `null`.
10. `reason`: Free-form description of the mutation, or `null`.
11. `previous_hash`: SHA-256 digest of the immediately preceding event. For the initial (genesis) event at sequence 1, `previous_hash` is 64 hex zeros (`"0" * 64`).
12. `hash`: SHA-256 digest of this event.

#### Canonical serialization & verification

- To prevent hash ambiguity from dictionary key ordering or whitespace, `canonical_bytes(event)` extracts the eleven hashed fields (all fields except `hash`) in the strict order defined by `EVENT_FIELD_ORDER`.
- Nested dictionaries (such as `changes` or records) are serialized with `sort_keys=True` and compact separators (`,`, `:`).
- The SHA-256 digest over these canonical UTF-8 bytes forms the event's `hash`.
- `verify_log(log_path)` walks the chain from genesis to head and detects:
  - `invalid_event` / `hash_mismatch`: Event payload does not match its computed SHA-256 hash.
  - `broken_link`: An event's `previous_hash` does not match the preceding event's `hash`.
  - `sequence_gap` / `duplicate_sequence`: Sequence numbers fail to start at 1 or do not strictly increment by 1.
  - `duplicate_event_id`: Repeated event UUIDs across the chain.
  - `dataset_mismatch`: Events referencing different dataset identifiers.
  - `malformed_entry`: Non-JSON lines, blank lines, or missing mandatory fields.
  - `unsupported_schema`: Schema versions unrecognized by the verifier.

### Interrupted mutation recovery protocol

Because the grade dataset (`grades.csv`) and the audit log (`grades.audit.jsonl`) are separate files, atomic replacement of both cannot occur simultaneously at the filesystem level. Coordination is achieved via `AuditTransaction` and a write-ahead journal (`grades.audit-pending.json`):

1. **Locking**: The dataset's advisory inter-process lock is acquired.
2. **Pre-recovery**: Any leftover pending journal is inspected and resolved via `recover_pending(csv_path)` before starting a new mutation.
3. **Preparation**: The next audit event is constructed and hashed against the current chain head.
4. **Staging**: The pending journal is written atomically, recording `dataset_sha256_before`, `dataset_sha256_after`, and the prepared audit event.
5. **Dataset commit**: The new CSV content is written to a temporary file, flushed, `fsync`ed, and atomically replaced (`os.replace`).
6. **Audit finalization**: The prepared audit event is appended to `grades.audit.jsonl`.
7. **Cleanup**: The pending journal file is removed.

#### Crash windows & recovery actions

When a crash or process termination occurs mid-transaction:

- **Crash before dataset replace (Step 5)**:
  - Current CSV SHA-256 matches `dataset_sha256_before`.
  - Recovery action: Discards the pending journal (`action: "rolled_back"`). The dataset remains in its original state; no false audit event is appended.
- **Crash between dataset replace and audit finalization (Step 5 to 6)**:
  - Current CSV SHA-256 matches `dataset_sha256_after`.
  - Recovery action: Appends the prepared event to the audit log and removes the journal (`action: "committed"`).
- **Crash between audit finalization and journal removal (Step 6 to 7)**:
  - Current CSV SHA-256 matches `dataset_sha256_after`, and the event ID is already present in the audit log.
  - Recovery action: Removes the orphaned journal without appending duplicate events (`action: "already_committed"`).
- **Ambiguous state**:
  - Current CSV SHA-256 matches neither `dataset_sha256_before` nor `dataset_sha256_after` (e.g. out-of-band edit or corruption).
  - Recovery action: Fails closed by raising `AmbiguousTransactionError`. The journal is preserved for manual inspection.

### Retention policy

- Audit logs are strictly append-only.
- Committed audit events are permanent records; grade deletions (`delete`) record an audit event capturing deleted records rather than truncating historical log entries.
- The hash chain enforces an unbroken sequence from sequence 1. Truncating or deleting historical events invalidates all subsequent hashes in the chain.

### Tamper-detection limitations

- **Local filesystem boundary**: The hash chain guarantees cryptographic tamper-*evidence*, not access control. An adversary with write permissions to the filesystem could rewrite the entire CSV and regenerate a valid hash chain from sequence 1. External anchoring (such as remote replication, detached signatures, or write-once storage) is required to detect complete history rewrites.
- **Advisory locking**: Concurrency guarantees rely on cooperative inter-process locks (`_InterProcessLock`). Uncoordinated processes writing directly to `grades.csv` bypass transactional journaling.
- **Storage durability**: Crash recovery relies on filesystem barrier semantics and `os.fsync`. Hardware-level write cache failures can compromise un-flushed disk blocks.

## Contributing

Fork the repo, make your changes on a new branch, and open a pull request. Run the tests first.

If you find a bug, open an issue with the steps to reproduce it, what you expected, and what happened instead.

Part of Source Start by CSI SPIT. MIT licensed.
