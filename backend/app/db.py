import os, sqlite3
from contextlib import contextmanager
from pathlib import Path

def db_path() -> Path:
    d = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "pantryfifo.db"

def connect():
    # autocommit (isolation_level=None): every write path that reads-then-writes
    # must go through write_txn so the check and the write are one transaction.
    c = sqlite3.connect(db_path(), timeout=30)
    c.row_factory = sqlite3.Row
    c.isolation_level = None
    c.execute("PRAGMA busy_timeout=30000")  # queue behind a concurrent writer instead of SQLITE_BUSY
    c.execute("PRAGMA journal_mode=WAL")    # readers never block the single writer
    return c

@contextmanager
def write_txn(c):
    """Short serialized write transaction.

    BEGIN IMMEDIATE takes the DB write lock up front, so consume / expire-sweep /
    transfer read-check-write sequences cannot interleave with each other; any
    exception rolls everything back (layer, qty_remain, derived alert set move
    together because they all live in the same committed state).
    """
    c.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        if c.in_transaction:
            c.execute("ROLLBACK")
        raise
    else:
        c.execute("COMMIT")
