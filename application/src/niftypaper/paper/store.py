"""Durable, append-only record of every accepted virtual trade (one JSON object per line), so the daily risk budget survives a restart.

The file is the source of truth: on start the engine rebuilds cash, positions and the day's realised P&L by replaying it with the same code that
applied the trades, and checks every record (sequence, turnover, realised P&L, starting balance, account). Anything unreadable, truncated, out of sequence,
inconsistent or for another account LOCKS the engine (no trade of any kind) instead of starting from a clean slate, and the file is never rewritten or
deleted by the program. To start a new virtual account, move or delete the file yourself: that is a deliberate act, and it also resets the daily risk budget.

Single writer: while an engine uses a file it holds an operating-system lock on `<file>.lock` (released automatically when the process ends, even if it is
killed, so a crash leaves no stale lock). A second process, or a second engine in the same process, that finds the lock taken gets StoreError("IN_USE") and
must not trade. Before every append the file's size is compared with what this store last wrote; any difference means someone else changed the file
(StoreError("CHANGED")) and the trade is refused. The lock is advisory: it protects against two copies of this program, not against a person editing the file.

Initialized accounts (replay accounts): with `require_initialized=True` the file must exist and carry a header that names this exact account and binding. A
missing file, a zero-byte file, a header-only file with a different account or binding, or anything damaged is an error (MISSING, EMPTY_FILE, DIFFERENT_ACCOUNT,
TRUNCATED, CORRUPT), never an empty account. `initialize()` creates the header-only file once (exclusive create, flushed to disk), never replaces anything, and is
idempotent only when the existing file passes the same validation. A header-only file is an account with zero trades; it is not the same as no file.

Other limits: the file is not tamper-proof, and the trade is written and flushed to disk before it is applied in memory, so after a crash the file may hold
a trade the screen never showed (it is replayed on the next start, which is the safe direction).
"""
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path

SCHEMA = 1
DROP = ("simulated", "quote_vs_fill")        # derived fields in TradeRecord.to_dict() that are not stored


class StoreError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _lock_handle(handle):
    """Take an exclusive, non-blocking lock on an open file; True if taken. Owned by the open handle, so a second handle in the same process also conflicts."""
    try:
        import msvcrt
    except ImportError:
        import fcntl
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


class JsonlStore:
    def __init__(self, path, starting_balance, account=None, binding=None, require_initialized=False):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.starting_balance = Decimal(starting_balance)
        self.account = account                       # name of the virtual account this file belongs to, e.g. "synthetic" or "fyers"
        self.binding = binding                       # optional: what the account was set up on (replay accounts: the input identity); written to the header and required to match
        self.require_initialized = require_initialized
        self._header_written = False
        self._lock_handle = None
        self._size = None

    # ---- single-writer lock --------------------------------------------------------------------------------------------------------------
    def acquire(self):
        if self._lock_handle is not None:
            return
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.lock_path, "a+b")
        except OSError:
            raise StoreError("LOCK_UNAVAILABLE", "the lock file could not be created")
        if not _lock_handle(handle):
            handle.close()
            raise StoreError("IN_USE", "another dashboard (or another engine) is already using this state file; only one may run at a time")
        self._lock_handle = handle

    def close(self):
        """Release the lock. Safe to call twice. (A killed process releases it anyway.)"""
        handle, self._lock_handle = self._lock_handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    # ---- reading and writing -------------------------------------------------------------------------------------------------------------
    def load(self):
        """The stored trade dictionaries in order ([] if there is no file yet). Takes the lock first. Raises StoreError for anything that is not a clean file."""
        self.acquire()
        if not self.path.exists():
            if self.require_initialized:
                raise StoreError("MISSING", "the account was never initialized: there is no state file (a missing file is not an empty account)")
            self._size = 0
            return []
        try:
            raw = self.path.read_bytes()
        except OSError:
            raise StoreError("UNREADABLE")
        self._size = len(raw)
        if not raw:
            if self.require_initialized:
                raise StoreError("EMPTY_FILE", "the state file has no header: it is damaged or was never initialized, not an empty account")
            return []
        if not raw.endswith(b"\n"):
            raise StoreError("TRUNCATED", "the last line is incomplete (the program may have stopped while writing)")
        try:
            lines = [json.loads(line, parse_constant=_no_constants) for line in raw.decode("utf-8").split("\n")[:-1]]
        except (ValueError, UnicodeDecodeError):
            raise StoreError("CORRUPT", "a line is not valid JSON")
        header, trades = lines[0], lines[1:]
        if not isinstance(header, dict) or header.get("kind") != "header" or header.get("schema") != SCHEMA:
            raise StoreError("CORRUPT", "missing or unknown header")
        try:
            same = Decimal(header["starting_balance"]) == self.starting_balance
        except (KeyError, InvalidOperation, TypeError):
            raise StoreError("CORRUPT", "header has no valid starting balance")
        if not same:
            raise StoreError("DIFFERENT_ACCOUNT", f"the file was created with starting balance {header['starting_balance']}, this run uses {self.starting_balance}")
        if self.require_initialized and (self.account is None or self.binding is None or "account" not in header or "binding" not in header):
            raise StoreError("DIFFERENT_ACCOUNT", "the header does not name an account and a binding, so it cannot be shown to belong to this replay")
        if self.account is not None and header.get("account", self.account) != self.account:
            raise StoreError("DIFFERENT_ACCOUNT", f"the file belongs to the {header.get('account')!r} account, not {self.account!r}")
        if self.binding is not None and header.get("binding") != self.binding:
            raise StoreError("DIFFERENT_ACCOUNT", "the file was set up on different inputs or settings than this run (its binding does not match); nothing is mixed")
        if any(not isinstance(t, dict) or t.get("kind") != "trade" for t in trades):
            raise StoreError("CORRUPT", "a line is not a trade record")
        self._header_written = True
        return [{k: v for k, v in t.items() if k != "kind"} for t in trades]

    def initialize(self):
        """Create the header-only state file of a bound account if it does not exist, then validate whatever is there with load(). Takes the lock. Never
        overwrites, truncates or repairs: an existing file is accepted only if it passes the same checks as at every start (same starting balance, account and
        binding, clean lines). Returns the stored trades ([] for a header-only file). Raises StoreError (WRITE_FAILED if the file cannot be made durable)."""
        if not self.require_initialized or self.account is None or self.binding is None:
            raise StoreError("NOT_BOUND", "only an account with a name and a binding, opened with require_initialized, can be initialized")
        self.acquire()
        if not self.path.exists() and not self.path.is_symlink():
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "xb") as handle:                      # exclusive: an existing file is never replaced
                    handle.write(_line({"kind": "header", "schema": SCHEMA, "starting_balance": str(self.starting_balance), "account": self.account,
                                        "binding": self.binding}))
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                pass                                                           # someone created it first: it is validated below like any existing file
            except OSError:
                raise StoreError("WRITE_FAILED", "the initial state file could not be created")
        return self.load()

    def append(self, record):
        """Write one trade and force it to disk. Raises StoreError if it cannot be made durable or the file is not as this store left it
        (the caller then must not apply the trade)."""
        if self._lock_handle is None or self._size is None:
            raise StoreError("NOT_LOCKED")
        try:
            actual = self.path.stat().st_size if self.path.exists() else 0
        except OSError:
            raise StoreError("WRITE_FAILED")
        if actual != self._size:
            raise StoreError("CHANGED", "the state file was changed by something else while the dashboard was running")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = b""
            if not self._header_written and self._size == 0:
                data += _line({"kind": "header", "schema": SCHEMA, "starting_balance": str(self.starting_balance),
                               **({"account": self.account} if self.account is not None else {}),
                               **({"binding": self.binding} if self.binding is not None else {})})
            data += _line({"kind": "trade", **{k: v for k, v in record.items() if k not in DROP}})
            with open(self.path, "ab") as handle:
                handle.write(data)                                   # header and trade in one write
                handle.flush()
                os.fsync(handle.fileno())
        except (OSError, TypeError, ValueError):
            raise StoreError("WRITE_FAILED")
        self._header_written = True
        self._size += len(data)


def _line(obj):
    return (json.dumps(obj, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def _no_constants(name):
    raise ValueError(name)
