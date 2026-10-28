"""Recoverable coordination between the grades dataset and its audit log.

The grades CSV and the append-only audit log are two separate files, so a
plain ``os.replace`` of each is *not* an atomic multi-file commit.  This
module supplies the missing piece: a small write-ahead journal that makes
the pair of writes a single *recoverable* logical transaction.

Protocol
--------

Every mutation runs inside :class:`AuditTransaction`, which holds the
dataset's inter-process lock for the whole operation:

1. **Recover** any journal left behind by an earlier interrupted mutation
   (see :func:`recover_pending`).  Recovery happens *before* any new work,
   so the log and the dataset are consistent again.
2. **Prepare** the audit event.  Its sequence number, ``previous_hash`` and
   own hash are computed from the current chain head.
3. **Stage** the new dataset bytes and record their SHA-256 digest in a
   pending journal file, together with the digest of the dataset *before*
   the change and the fully prepared audit event.
4. **Commit the dataset** with an atomic replace of the staged bytes.
5. **Finalize the audit** by appending the prepared event to the log.
6. **Clear** the pending journal.

Crash windows
-------------

* Crash *before* step 4: the journal is present but the dataset still holds
  its old digest.  Recovery discards the journal; nothing was committed and
  no audit event exists.
* Crash *between* steps 4 and 5: the dataset holds the new digest.  Recovery
  appends the prepared event exactly once (the event's ``event_id`` is
  checked first) and clears the journal.
* Crash *between* steps 5 and 6: the event is already in the log.  Recovery
  finds the ``event_id`` already present, appends nothing, and clears the
  journal.

If the dataset digest matches neither the old nor the new value the state
is *ambiguous*: recovery fails closed and refuses to touch either file.

No multi-file atomicity is claimed.  What is claimed -- and tested -- is
that the pair of files can always be brought back to a consistent state by
running recovery, which happens automatically before the next mutation and
can also be triggered explicitly.
"""

import hashlib
import json
import os
import tempfile
from pathlib import Path

from grade_tracker.audit_log import (
    AuditIntegrityError,
    append_event,
    audit_log_path_for,
    build_event,
    chain_head,
    dataset_id_for,
    event_exists,
    pending_journal_path_for,
)

JOURNAL_VERSION = "1.0"


class AuditTransactionError(Exception):
    """Raised when a mutation cannot be committed or recovered safely."""


class AmbiguousTransactionError(AuditTransactionError):
    """Raised when an interrupted transaction cannot be resolved safely."""


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path):
    path = Path(path)
    if not path.exists():
        return None
    with open(path, "rb") as handle:
        return _sha256_bytes(handle.read())


def _write_json_atomic(path, payload):
    """Write ``payload`` as JSON to ``path`` atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".audit-journal-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _read_journal(path):
    path = Path(path)
    if not path.exists():
        return None
    try:
        with open(path, mode="r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditTransactionError(
            f"pending audit journal at {path} is unreadable: {exc}"
        ) from exc


def _clear_journal(path):
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise AuditTransactionError(
            f"could not remove pending audit journal {path}: {exc}"
        ) from exc


def recover_pending(csv_path, log_path=None):
    """Reconcile a pending journal left by an interrupted mutation.

    Returns a dictionary describing what was done:

    ``{"recovered": bool, "action": str, "event_id": str|None}``

    ``action`` is one of ``"none"``, ``"committed"`` (the dataset had been
    replaced, so the audit event was appended now), ``"rolled_back"`` (the
    dataset had not been replaced, so the journal was discarded) or
    ``"already_committed"`` (the event was already in the log).
    """
    csv_path = Path(csv_path)
    log_path = Path(log_path) if log_path else audit_log_path_for(csv_path)
    journal_path = pending_journal_path_for(csv_path)

    journal = _read_journal(journal_path)
    if journal is None:
        return {"recovered": False, "action": "none", "event_id": None}

    if journal.get("journal_version") != JOURNAL_VERSION:
        raise AuditTransactionError(
            f"pending audit journal at {journal_path} has unsupported version "
            f"{journal.get('journal_version')!r} (expected {JOURNAL_VERSION!r})"
        )

    event = journal.get("event")
    if not isinstance(event, dict):
        raise AuditTransactionError(
            f"pending audit journal at {journal_path} does not contain a "
            "prepared audit event"
        )

    current = _sha256_file(csv_path)
    old_digest = journal.get("dataset_sha256_before")
    new_digest = journal.get("dataset_sha256_after")

    if current == new_digest:
        # The dataset replacement happened; finish the audit step.
        if event_exists(log_path, event["event_id"]):
            _clear_journal(journal_path)
            return {
                "recovered": True,
                "action": "already_committed",
                "event_id": event["event_id"],
            }
        append_event(log_path, event)
        _clear_journal(journal_path)
        return {
            "recovered": True,
            "action": "committed",
            "event_id": event["event_id"],
        }

    if current == old_digest:
        # The dataset was never replaced; the mutation did not commit.
        _clear_journal(journal_path)
        return {
            "recovered": True,
            "action": "rolled_back",
            "event_id": None,
        }

    raise AmbiguousTransactionError(
        "an interrupted grade mutation left the audit journal in an "
        f"ambiguous state: the dataset at {csv_path} matches neither the "
        "recorded pre-transaction digest nor the recorded post-transaction "
        "digest.  Refusing to guess; inspect the dataset and the journal "
        f"at {journal_path} manually."
    )


def journal_state(csv_path):
    """Return the pending journal payload for ``csv_path``, or ``None``."""
    return _read_journal(pending_journal_path_for(csv_path))




class AuditTransaction:
    """A recoverable logical transaction over the dataset and audit log.

    The instance must be used as a context manager.  Call
    :meth:`write_dataset` (or :meth:`write_bytes`) exactly once to commit
    the change; the audit event is prepared from the ``records`` and
    ``changes`` supplied to the constructor.
    """

    def __init__(
        self,
        csv_path,
        operation,
        records=None,
        changes=None,
        actor=None,
        reason=None,
        log_path=None,
        dataset_id=None,
        manage_lock=True,
    ):
        self.csv_path = Path(csv_path)
        self.operation = operation
        self.records = list(records or [])
        self.changes = dict(changes or {})
        self.actor = actor
        self.reason = reason
        self.log_path = (
            Path(log_path) if log_path else audit_log_path_for(self.csv_path)
        )
        self.dataset_id = dataset_id or dataset_id_for(self.csv_path)
        self.journal_path = pending_journal_path_for(self.csv_path)
        self.manage_lock = manage_lock
        self._lock = None
        self._committed = False
        self._event = None

    def __enter__(self):
        from grade_tracker.cli import _InterProcessLock, _lock_path_for

        if self.manage_lock:
            self._lock = _InterProcessLock(_lock_path_for(self.csv_path))
            self._lock.acquire()
        try:
            # Reconcile any earlier interrupted mutation first, so the chain
            # head we read below is the true head of the log.
            recover_pending(self.csv_path, self.log_path)
        except Exception:
            if self._lock is not None:
                self._lock.release()
                self._lock = None
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._lock is not None:
            self._lock.release()
            self._lock = None
        return False

    def _prepare_event(self):
        """Build the audit event for this transaction.

        The chain head is read while the dataset lock is held, so the
        sequence number and ``previous_hash`` are always correct.
        """
        head = chain_head(self.log_path)
        if head is None:
            sequence = 1
            previous_hash = None
        else:
            sequence = head["sequence"] + 1
            previous_hash = head["hash"]
        return build_event(
            sequence=sequence,
            operation=self.operation,
            dataset_id=self.dataset_id,
            records=self.records,
            changes=self.changes,
            actor=self.actor,
            reason=self.reason,
            previous_hash=previous_hash,
        )

    def write_bytes(self, data):
        """Replace the dataset with ``data`` and record the audit event."""
        from grade_tracker.cli import _atomic_write_bytes

        self._commit(data, lambda: _atomic_write_bytes(self.csv_path, data))

    def write_dataset(self, rows):
        """Replace the dataset with ``rows`` (data records, no header)."""
        from grade_tracker.cli import _atomic_write_rows, _csv_rows_bytes

        data = _csv_rows_bytes(rows)
        self._commit(data, lambda: _atomic_write_rows(self.csv_path, rows))

    def _commit(self, data, write):
        """Commit the dataset bytes and finalize the audit event."""
        if self._committed:
            raise AuditTransactionError("this transaction was already committed")

        event = self._prepare_event()
        old_digest = _sha256_file(self.csv_path)
        new_digest = _sha256_bytes(data)

        journal = {
            "journal_version": JOURNAL_VERSION,
            "dataset_id": self.dataset_id,
            "operation": self.operation,
            "dataset_path": str(self.csv_path),
            "log_path": str(self.log_path),
            "dataset_sha256_before": old_digest,
            "dataset_sha256_after": new_digest,
            "event": event,
        }
        _write_json_atomic(self.journal_path, journal)

        try:
            write()
        except Exception:
            # The dataset was not replaced; discard the journal so the
            # mutation is treated as never having happened.
            _clear_journal(self.journal_path)
            raise

        self._event = event
        self._finalize()

    def _finalize(self):
        """Append the prepared event and clear the journal."""
        try:
            if not event_exists(self.log_path, self._event["event_id"]):
                append_event(self.log_path, self._event)
        except AuditIntegrityError:
            # The log is broken; leave the journal in place so that the next
            # recovery attempt can finish the step once the log is repaired.
            raise
        except Exception:
            # The dataset is already committed but the audit step failed.
            # The journal remains, so recovery completes it exactly once.
            raise AuditTransactionError(
                "the grade change was committed but the audit event could "
                "not be written; the pending journal was kept so that the "
                "audit step can be completed by recovery"
            ) from None
        _clear_journal(self.journal_path)
        self._committed = True

