"""Crash-recoverable write-ahead transaction journal for grade mutations (Issue #47).

This module implements a centralized transaction coordinator for all grade
mutations. It manages safe staging, write-ahead journaling, atomic commitment,
checksum verification, automatic crash recovery, and transaction status inspection.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Dict, List, Optional, Tuple

JOURNAL_FORMAT_VERSION = "1.0"
TRANSACTION_ID_REGEX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SHA256_HEX_REGEX = re.compile(r"^[0-9a-f]{64}$")

# Transaction States
STATE_PREPARED = "PREPARED"
STATE_COMMITTING = "COMMITTING"
STATE_COMMITTED = "COMMITTED"
STATE_ABORTED = "ABORTED"

VALID_STATES = {STATE_PREPARED, STATE_COMMITTING, STATE_COMMITTED, STATE_ABORTED}

# Global test-only fault injection hook: Callable[[str, 'TransactionJournalEntry', Path], None]
_FAULT_INJECTION_HOOK: Optional[Callable[[str, Any, Path], None]] = None


def set_fault_injection_hook(hook: Optional[Callable[[str, Any, Path], None]]) -> None:
    """Set a test-only callback hook invoked at transaction state transitions."""
    global _FAULT_INJECTION_HOOK
    _FAULT_INJECTION_HOOK = hook


def compute_file_sha256(path: Path) -> Optional[str]:
    """Compute hex SHA-256 digest of exact file bytes. Returns None if file does not exist."""
    if not path.exists():
        return None
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest().lower()


def compute_bytes_sha256(data: bytes) -> str:
    """Compute hex SHA-256 digest of given bytes."""
    return hashlib.sha256(data).hexdigest().lower()


def get_journal_dir(target_csv: Path) -> Path:
    """Return the dedicated transaction journal directory for target_csv.
    
    Located at <target_csv.parent> / .tx_journal_<target_csv.name>.
    """
    resolved = target_csv.resolve()
    return resolved.parent / f".tx_journal_{resolved.name}"


@dataclass
class TransactionJournalEntry:
    version: str
    tx_id: str
    mutation_type: str
    target_csv: str
    state: str
    source_checksum: Optional[str]
    target_checksum: str
    staged_filename: str
    created_at: str
    updated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "tx_id": self.tx_id,
            "mutation_type": self.mutation_type,
            "target_csv": self.target_csv,
            "state": self.state,
            "source_checksum": self.source_checksum,
            "target_checksum": self.target_checksum,
            "staged_filename": self.staged_filename,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], journal_file_path: Optional[Path] = None) -> "TransactionJournalEntry":
        if not isinstance(data, dict):
            raise ValueError("journal root must be a JSON object")

        version = data.get("version")
        if version != JOURNAL_FORMAT_VERSION:
            raise ValueError(f"unsupported journal version: {version!r} (expected {JOURNAL_FORMAT_VERSION!r})")

        tx_id = data.get("tx_id")
        if not isinstance(tx_id, str) or not TRANSACTION_ID_REGEX.fullmatch(tx_id):
            raise ValueError(f"invalid or malformed tx_id: {tx_id!r}")

        mutation_type = data.get("mutation_type")
        if not isinstance(mutation_type, str) or not mutation_type.strip():
            raise ValueError(f"invalid mutation_type: {mutation_type!r}")

        target_csv = data.get("target_csv")
        if not isinstance(target_csv, str) or not target_csv.strip():
            raise ValueError(f"invalid target_csv: {target_csv!r}")

        state = data.get("state")
        if state not in VALID_STATES:
            raise ValueError(f"invalid transaction state: {state!r}")

        source_checksum = data.get("source_checksum")
        if source_checksum is not None:
            if not isinstance(source_checksum, str) or not SHA256_HEX_REGEX.fullmatch(source_checksum.lower()):
                raise ValueError(f"invalid source_checksum: {source_checksum!r}")
            source_checksum = source_checksum.lower()

        target_checksum = data.get("target_checksum")
        if not isinstance(target_checksum, str) or not SHA256_HEX_REGEX.fullmatch(target_checksum.lower()):
            raise ValueError(f"invalid target_checksum: {target_checksum!r}")
        target_checksum = target_checksum.lower()

        staged_filename = data.get("staged_filename")
        if not isinstance(staged_filename, str) or not staged_filename.strip():
            raise ValueError(f"invalid staged_filename: {staged_filename!r}")

        # Security check: staged_filename must be a simple filename without path traversal
        if Path(staged_filename).name != staged_filename or ".." in staged_filename or "/" in staged_filename or "\\" in staged_filename:
            raise ValueError(f"unsafe staged_filename path traversal: {staged_filename!r}")

        created_at = data.get("created_at")
        if not isinstance(created_at, str) or not created_at.strip():
            raise ValueError(f"invalid created_at: {created_at!r}")

        updated_at = data.get("updated_at")
        if not isinstance(updated_at, str) or not updated_at.strip():
            raise ValueError(f"invalid updated_at: {updated_at!r}")

        return cls(
            version=version,
            tx_id=tx_id,
            mutation_type=mutation_type,
            target_csv=target_csv,
            state=state,
            source_checksum=source_checksum,
            target_checksum=target_checksum,
            staged_filename=staged_filename,
            created_at=created_at,
            updated_at=updated_at,
        )


def _write_atomic_json(path: Path, data: Dict[str, Any]) -> None:
    """Safely write JSON data to path using temporary file and atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".tx_tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def save_journal_entry(journal_dir: Path, entry: TransactionJournalEntry) -> Path:
    """Save or update a journal entry atomically."""
    journal_path = journal_dir / f"{entry.tx_id}.json"
    _write_atomic_json(journal_path, entry.to_dict())
    return journal_path


def load_journal_entry(journal_path: Path) -> TransactionJournalEntry:
    """Read and validate a journal entry from disk."""
    with open(journal_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return TransactionJournalEntry.from_dict(data, journal_file_path=journal_path)


class TransactionJournalCoordinator:
    """Coordinates write-ahead transactional mutations and crash recovery for a target CSV."""

    def __init__(self, target_csv_path: Path, lock_acquire_fn: Optional[Callable] = None):
        self.target_csv = Path(target_csv_path).resolve()
        self.journal_dir = get_journal_dir(self.target_csv)
        self.lock_acquire_fn = lock_acquire_fn

    def _trigger_hook(self, stage_name: str, entry: TransactionJournalEntry, staged_file: Path) -> None:
        if _FAULT_INJECTION_HOOK is not None:
            _FAULT_INJECTION_HOOK(stage_name, entry, staged_file)

    def recover(self, validate_staged_fn: Optional[Callable[[Path], None]] = None) -> List[Dict[str, Any]]:
        """Discover and recover any pending or interrupted transactions for target_csv.

        Must be called while holding the target CSV's inter-process lock.
        Returns a list of recovery outcome reports for non-terminal / actionable transactions.
        """
        outcomes = []
        if not self.journal_dir.exists():
            return outcomes

        # List all .json files in journal_dir sorted by name/timestamp
        journal_files = sorted(self.journal_dir.glob("*.json"))
        for j_file in journal_files:
            outcome = self._recover_single_journal(j_file, validate_staged_fn=validate_staged_fn)
            if outcome is not None:
                outcomes.append(outcome)

        return outcomes

    def _recover_single_journal(
        self, journal_file: Path, validate_staged_fn: Optional[Callable[[Path], None]] = None
    ) -> Optional[Dict[str, Any]]:
        """Perform recovery on a single journal file following Cases A-E.

        Returns None if the transaction is already in a clean terminal state (COMMITTED or ABORTED).
        """
        # Case E: Journal cannot be loaded or is invalid
        try:
            entry = load_journal_entry(journal_file)
        except Exception as exc:
            return {
                "journal_file": str(journal_file),
                "status": "ambiguous",
                "action": "none",
                "error": f"Case E: Malformed or invalid journal entry: {exc}",
            }

        # Verify dataset identity matches coordinator target
        try:
            entry_target = Path(entry.target_csv).resolve()
            if entry_target != self.target_csv:
                return {
                    "journal_file": str(journal_file),
                    "tx_id": entry.tx_id,
                    "status": "ambiguous",
                    "action": "none",
                    "error": f"Case E: Journal target mismatch ({entry.target_csv} != {self.target_csv})",
                }
        except Exception as exc:
            return {
                "journal_file": str(journal_file),
                "tx_id": entry.tx_id,
                "status": "ambiguous",
                "action": "none",
                "error": f"Case E: Invalid target path: {exc}",
            }

        staged_file = self.target_csv.parent / entry.staged_filename

        # If already in a terminal state, nothing to recover
        if entry.state == STATE_COMMITTED:
            # Clean up staged file if it somehow remained
            if staged_file.exists():
                try:
                    os.unlink(staged_file)
                except OSError:
                    pass
            return None

        if entry.state == STATE_ABORTED:
            if staged_file.exists():
                try:
                    os.unlink(staged_file)
                except OSError:
                    pass
            return None

        # Compute current actual dataset checksum
        current_checksum = compute_file_sha256(self.target_csv)

        # Case A: Dataset checksum matches the expected post-transaction checksum
        # The atomic replacement happened, but journal was not updated to COMMITTED.
        if current_checksum is not None and current_checksum == entry.target_checksum:
            entry.state = STATE_COMMITTED
            entry.updated_at = datetime.now(timezone.utc).isoformat()
            save_journal_entry(self.journal_dir, entry)
            if staged_file.exists():
                try:
                    os.unlink(staged_file)
                except OSError:
                    pass
            return {
                "journal_file": str(journal_file),
                "tx_id": entry.tx_id,
                "status": "committed",
                "action": "finalized_committed",
                "message": "Case A: Target dataset matches expected target checksum; finalized as committed.",
            }

        # Case B: Dataset checksum matches original source checksum AND state is PREPARED
        if current_checksum == entry.source_checksum and entry.state == STATE_PREPARED:
            entry.state = STATE_ABORTED
            entry.updated_at = datetime.now(timezone.utc).isoformat()
            save_journal_entry(self.journal_dir, entry)
            if staged_file.exists():
                try:
                    os.unlink(staged_file)
                except OSError:
                    pass
            return {
                "journal_file": str(journal_file),
                "tx_id": entry.tx_id,
                "status": "aborted",
                "action": "aborted_prepared",
                "message": "Case B: Target dataset matches original source checksum and state was PREPARED; rolled back.",
            }

        # Case C: Dataset checksum matches original source checksum AND state is COMMITTING
        if current_checksum == entry.source_checksum and entry.state == STATE_COMMITTING:
            # We must check if staged file exists, matches target checksum, and passes validation
            if not staged_file.exists():
                return {
                    "journal_file": str(journal_file),
                    "tx_id": entry.tx_id,
                    "status": "ambiguous",
                    "action": "none",
                    "error": "Case C: State is COMMITTING but staged file is missing; requires manual investigation.",
                }

            staged_checksum = compute_file_sha256(staged_file)
            if staged_checksum != entry.target_checksum:
                return {
                    "journal_file": str(journal_file),
                    "tx_id": entry.tx_id,
                    "status": "ambiguous",
                    "action": "none",
                    "error": "Case C: Staged file checksum mismatch; requires manual investigation.",
                }

            if validate_staged_fn is not None:
                try:
                    validate_staged_fn(staged_file)
                except Exception as val_exc:
                    return {
                        "journal_file": str(journal_file),
                        "tx_id": entry.tx_id,
                        "status": "ambiguous",
                        "action": "none",
                        "error": f"Case C: Staged file validation failed: {val_exc}",
                    }

            # Complete the interrupted commit via atomic replacement
            try:
                os.replace(staged_file, self.target_csv)
            except OSError as exc:
                return {
                    "journal_file": str(journal_file),
                    "tx_id": entry.tx_id,
                    "status": "ambiguous",
                    "action": "none",
                    "error": f"Case C: Failed to replace target CSV during recovery: {exc}",
                }

            # Verify post-replacement checksum
            verified_checksum = compute_file_sha256(self.target_csv)
            if verified_checksum != entry.target_checksum:
                return {
                    "journal_file": str(journal_file),
                    "tx_id": entry.tx_id,
                    "status": "ambiguous",
                    "action": "none",
                    "error": "Case C: Replaced CSV checksum does not match expected target checksum.",
                }

            entry.state = STATE_COMMITTED
            entry.updated_at = datetime.now(timezone.utc).isoformat()
            save_journal_entry(self.journal_dir, entry)
            return {
                "journal_file": str(journal_file),
                "tx_id": entry.tx_id,
                "status": "committed",
                "action": "completed_commit",
                "message": "Case C: Successfully replayed atomic replacement and finalized as committed.",
            }

        # Case D: Dataset checksum matches neither recorded checksum
        return {
            "journal_file": str(journal_file),
            "tx_id": entry.tx_id,
            "status": "ambiguous",
            "action": "none",
            "error": (
                f"Case D: Target checksum {current_checksum!r} matches neither original "
                f"{entry.source_checksum!r} nor target {entry.target_checksum!r}; manual investigation required."
            ),
        }

    def execute_mutation(
        self,
        mutation_type: str,
        mutate_rows_fn: Callable[[Optional[List[List[str]]]], List[List[str]]],
        header: List[str],
        validate_staged_fn: Optional[Callable[[Path], None]] = None,
        retention_limit: int = 50,
    ) -> TransactionJournalEntry:
        """Execute a complete write-ahead transactional mutation lifecycle.

        Caller MUST already hold the inter-process lock for self.target_csv.
        """
        import uuid

        # 1. Recover pending transactions first.
        # Fail closed if any journal is unresolved / ambiguous.
        recovery_outcomes = self.recover(validate_staged_fn=validate_staged_fn)
        for outcome in recovery_outcomes:
            if outcome.get("status") == "ambiguous":
                raise ValueError(
                    f"Refusing to execute mutation: pending transaction in ambiguous state: {outcome.get('error')}"
                )

        # 2. Source file state and checksum
        source_exists = self.target_csv.exists()
        source_checksum = compute_file_sha256(self.target_csv) if source_exists else None

        # Read existing rows if present
        existing_rows: Optional[List[List[str]]] = None
        if source_exists:
            import csv
            with open(self.target_csv, "r", encoding="utf-8", newline="") as f:
                reader = csv.reader(f)
                actual_header = next(reader, None)
                if actual_header is not None and actual_header != header:
                    raise ValueError(
                        f"grades file has an unexpected schema; refusing to rewrite {self.target_csv} "
                        f"(expected {header!r}, got {actual_header!r})"
                    )
                existing_rows = list(reader) if actual_header is not None else []

        # 3. Apply business operation
        new_rows = mutate_rows_fn(existing_rows)

        # 4. Generate transaction ID and safe staged filename
        tx_id = str(uuid.uuid4())
        staged_filename = f".tx_stage_{tx_id}.tmp"
        staged_path = self.target_csv.parent / staged_filename

        # Ensure target directory exists
        self.target_csv.parent.mkdir(parents=True, exist_ok=True)
        self.journal_dir.mkdir(parents=True, exist_ok=True)

        try:
            # 5. Write staged file with fsync
            import csv
            fd, tmp_path_str = tempfile.mkstemp(
                dir=str(self.target_csv.parent), prefix=".tx_write_", suffix=".tmp"
            )
            tmp_path = Path(tmp_path_str)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(header)
                    for r in new_rows:
                        writer.writerow(r)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, staged_path)
            finally:
                if tmp_path.exists():
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

            # 6. Validate staged file
            if validate_staged_fn is not None:
                validate_staged_fn(staged_path)

            # 7. Compute staged file checksum
            target_checksum = compute_file_sha256(staged_path)
            if target_checksum is None:
                raise ValueError("Failed to compute staged file checksum.")

            now_str = datetime.now(timezone.utc).isoformat()
            entry = TransactionJournalEntry(
                version=JOURNAL_FORMAT_VERSION,
                tx_id=tx_id,
                mutation_type=mutation_type,
                target_csv=str(self.target_csv),
                state=STATE_PREPARED,
                source_checksum=source_checksum,
                target_checksum=target_checksum,
                staged_filename=staged_filename,
                created_at=now_str,
                updated_at=now_str,
            )

            # 8. Write PREPARED journal
            save_journal_entry(self.journal_dir, entry)
            self._trigger_hook("after_prepared", entry, staged_path)

            # 9. Transition to COMMITTING journal
            entry.state = STATE_COMMITTING
            entry.updated_at = datetime.now(timezone.utc).isoformat()
            save_journal_entry(self.journal_dir, entry)
            self._trigger_hook("after_committing", entry, staged_path)

            # 10. Atomic replace
            os.replace(staged_path, self.target_csv)
            self._trigger_hook("after_replace", entry, staged_path)

            # 11. Verify target checksum matches expected
            actual_new_checksum = compute_file_sha256(self.target_csv)
            if actual_new_checksum != target_checksum:
                raise ValueError(
                    f"Post-commit checksum mismatch: expected {target_checksum}, got {actual_new_checksum}"
                )

            # 12. Transition to COMMITTED journal
            entry.state = STATE_COMMITTED
            entry.updated_at = datetime.now(timezone.utc).isoformat()
            save_journal_entry(self.journal_dir, entry)
            self._trigger_hook("after_committed", entry, staged_path)

            # 13. Clean up old committed/aborted transactions according to retention limit
            self._cleanup_old_journals(retention_limit)

            return entry

        except Exception:
            # Clean up staged file if it was not atomically replaced
            if staged_path.exists():
                try:
                    os.unlink(staged_path)
                except OSError:
                    pass
            raise

    def _cleanup_old_journals(self, retention_limit: int) -> None:
        """Safely prune oldest COMMITTED or ABORTED journal files exceeding retention_limit.
        
        Never touches unresolved (PREPARED / COMMITTING) or ambiguous journals.
        """
        if retention_limit <= 0 or not self.journal_dir.exists():
            return

        terminal_files: List[Tuple[float, Path]] = []
        for j_path in self.journal_dir.glob("*.json"):
            try:
                entry = load_journal_entry(j_path)
                if entry.state in (STATE_COMMITTED, STATE_ABORTED):
                    terminal_files.append((j_path.stat().st_mtime, j_path))
            except Exception:
                # Suspicious or unparseable files are preserved for investigation
                continue

        if len(terminal_files) > retention_limit:
            # Sort oldest first
            terminal_files.sort(key=lambda t: t[0])
            to_remove = terminal_files[: len(terminal_files) - retention_limit]
            for _, path_to_del in to_remove:
                try:
                    os.unlink(path_to_del)
                except OSError:
                    pass

    def inspect_status(self) -> List[Dict[str, Any]]:
        """Read-only inspection of transactions for target_csv.

        Does not acquire locks, does not execute mutations, does not recover or delete files.
        """
        results = []
        if not self.journal_dir.exists():
            return results

        current_checksum = compute_file_sha256(self.target_csv)

        for j_file in sorted(self.journal_dir.glob("*.json")):
            try:
                entry = load_journal_entry(j_file)
            except Exception as exc:
                results.append({
                    "journal_file": j_file.name,
                    "tx_id": j_file.stem,
                    "version": "unknown",
                    "mutation_type": "unknown",
                    "target_csv": str(self.target_csv),
                    "state": "MALFORMED",
                    "needs_recovery": True,
                    "health": "ambiguous",
                    "message": f"Malformed journal entry: {exc}",
                })
                continue

            staged_path = self.target_csv.parent / entry.staged_filename
            staged_exists = staged_path.exists()

            # Determine health & recovery status without modifying state
            needs_recovery = False
            health = "unknown"
            message = ""

            if entry.state == STATE_COMMITTED:
                needs_recovery = False
                health = "committed"
                message = "Transaction committed successfully."
            elif entry.state == STATE_ABORTED:
                needs_recovery = False
                health = "aborted"
                message = "Transaction aborted and safely rolled back."
            elif entry.state == STATE_PREPARED:
                needs_recovery = True
                if current_checksum == entry.source_checksum:
                    health = "recoverable"
                    message = "Ready to rollback uncommitted prepared transaction."
                elif current_checksum == entry.target_checksum:
                    health = "recoverable"
                    message = "Dataset already matches target checksum; can finalize commit."
                else:
                    health = "ambiguous"
                    message = "Dataset checksum matches neither source nor target."
            elif entry.state == STATE_COMMITTING:
                needs_recovery = True
                if current_checksum == entry.target_checksum:
                    health = "recoverable"
                    message = "Replacement complete; ready to finalize commit."
                elif current_checksum == entry.source_checksum:
                    if staged_exists:
                        health = "recoverable"
                        message = "Staged file present; ready to complete interrupted atomic replacement."
                    else:
                        health = "ambiguous"
                        message = "Staged file missing while in COMMITTING state."
                else:
                    health = "ambiguous"
                    message = "Dataset checksum matches neither source nor target."

            results.append({
                "journal_file": j_file.name,
                "tx_id": entry.tx_id,
                "version": entry.version,
                "mutation_type": entry.mutation_type,
                "target_csv": entry.target_csv,
                "state": entry.state,
                "needs_recovery": needs_recovery,
                "health": health,
                "created_at": entry.created_at,
                "updated_at": entry.updated_at,
                "message": message,
            })

        return results

