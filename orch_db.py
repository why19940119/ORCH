"""v0.21.0 (WP-ORCH-12): SQLite state store.

All mutable ORCH state lives in one SQLite database per state directory
(``state/orch.db``, WAL mode, busy_timeout). The rest of the code keeps
addressing state by its historical file path (``state/task_status.json``
...); those paths are *names* here, mapped to tables:

    task_status.json      -> table task_status (one row per task)
    ecom_demo_queue.json  -> docs['ecom_demo_queue']
    ecom_import.json      -> docs['ecom_import']
    auth.json             -> docs['auth']
    events.jsonl          -> table events      (append-only)
    auth_audit.jsonl      -> table auth_audit  (append-only)
    chat_usage.jsonl      -> table chat_usage  (append-only)

The database lives next to the named file (``Path(path).parent/orch.db``),
so tests that point the modules at a temp ``state/`` get their own DB.

Legacy JSON files found next to the DB are imported automatically only
while the DB is still EMPTY (the one-time v0.20 -> v0.21 migration): each
file is first moved into ``json-backup-<UTC timestamp>/`` and then imported
in the same write transaction. Once the DB holds data, a stray JSON file is
left untouched and a loud warning is logged instead - it can never replace
or delete rows. ``python orch_db.py migrate --force`` imports into a
non-empty DB deliberately: it first writes a backup of orch.db, upserts task
rows (never deletes), replaces documents, and appends only log records that
are not already present (deduplicated on their canonical JSON), so running
it twice or re-importing an export adds nothing. A file whose sha256 was
already imported is skipped.

Transactions: ``transaction(path)`` opens ``BEGIN IMMEDIATE`` (one writer
at a time across processes) and is re-entrant per thread; every load/save
inside it uses the same connection, so read-modify-write sequences and
their audit rows commit (or roll back) together.
"""

import argparse
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger("orch.db")

SCHEMA_VERSION = 1
DB_NAME = "orch.db"
BUSY_TIMEOUT_MS = int(os.getenv("ORCH_DB_BUSY_TIMEOUT_MS") or 30000)
PROJECT_ROOT = Path(__file__).resolve().parent

# file name -> (kind, table/doc name)
MANAGED = {
    "task_status.json": ("status", "task_status"),
    "ecom_demo_queue.json": ("doc", "ecom_demo_queue"),
    "ecom_import.json": ("doc", "ecom_import"),
    "auth.json": ("doc", "auth"),
    "events.jsonl": ("log", "events"),
    "auth_audit.jsonl": ("log", "auth_audit"),
    "chat_usage.jsonl": ("log", "chat_usage"),
}
LOG_TABLES = ("events", "auth_audit", "chat_usage")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS docs (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_status (
    task_id TEXT PRIMARY KEY,
    status TEXT,
    approval_status TEXT,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    event TEXT,
    task_id TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id);
CREATE TABLE IF NOT EXISTS auth_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    event TEXT,
    actor TEXT,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS migrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    records INTEGER NOT NULL,
    backup_path TEXT NOT NULL,
    migrated_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS events_append_only_u BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_append_only_d BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS auth_audit_append_only_u BEFORE UPDATE ON auth_audit
BEGIN SELECT RAISE(ABORT, 'auth_audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS auth_audit_append_only_d BEFORE DELETE ON auth_audit
BEGIN SELECT RAISE(ABORT, 'auth_audit is append-only'); END;
"""

_local = threading.local()
_refusal_warned = set()
DATA_TABLES = ("task_status", "docs") + ("events", "auth_audit", "chat_usage")


class MigrationRefused(RuntimeError):
    """Legacy JSON files next to a DB that already holds data."""


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path_for(path):
    """The database that holds ``path`` (a managed name, a lock file, a
    directory or the .db itself)."""
    path = Path(path)
    if path.suffix == ".db":
        return Path(os.path.abspath(path))
    if path.is_dir():
        return Path(os.path.abspath(path / DB_NAME))
    return Path(os.path.abspath(path.parent / DB_NAME))


def is_managed(path):
    return Path(path).name in MANAGED


def _active():
    if not hasattr(_local, "tx"):
        _local.tx = {}
    return _local.tx


def _create_private(path):
    """Create ``path`` as an empty 0600 file unless it exists (an empty file
    is a valid empty SQLite DB; SQLite gives -wal / -shm the DB's mode)."""
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        pass


def restrict_db_files(db_file):
    """orch.db (password hashes, approvals) and its -wal / -shm: owner only."""
    for suffix in ("", "-wal", "-shm"):
        path = f"{db_file}{suffix}"
        try:
            if os.stat(path).st_mode & 0o077:
                os.chmod(path, 0o600)
        except FileNotFoundError:
            pass
        except PermissionError:
            pass            # not our file (e.g. read-only mount); leave it


def _write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.chmod(path, 0o600)


def _connect(db_file):
    db_file.parent.mkdir(parents=True, exist_ok=True)
    _create_private(db_file)
    conn = sqlite3.connect(str(db_file), timeout=BUSY_TIMEOUT_MS / 1000,
                           isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    _enable_wal(conn)
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    restrict_db_files(db_file)
    return conn


def _enable_wal(conn):
    """WAL is persistent, so this only switches once (on a new DB). The
    switch needs an exclusive lock and SQLite does not run the busy handler
    for it, so two processes creating the DB at once retry here."""
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    delay = 0.005
    while True:
        try:
            if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
                return
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.2)


def _ensure_schema(conn):
    if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return
    # executescript commits any open transaction first, so the script
    # carries its own BEGIN IMMEDIATE ... COMMIT (all statements are
    # idempotent: a second process racing here just re-runs them).
    script = (
        "BEGIN IMMEDIATE;" + SCHEMA
        + f"INSERT OR REPLACE INTO meta VALUES ('schema_version', '{SCHEMA_VERSION}');"
        + f"INSERT OR IGNORE INTO meta VALUES ('created_at_utc', '{_utc()}');"
        + f"PRAGMA user_version={SCHEMA_VERSION};COMMIT;"
    )
    try:
        conn.executescript(script)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def legacy_files(directory):
    directory = Path(directory)
    return [directory / name for name in MANAGED if (directory / name).is_file()]


def _remove_db_files(db_file):
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(f"{db_file}{suffix}")
        except FileNotFoundError:
            pass


def _warn_refused(db_file, exc):
    """Log a refused auto-migration once per process and file set."""
    files = []
    for path in legacy_files(db_file.parent):
        try:
            st = path.stat()
            files.append((path.name, st.st_size, st.st_mtime_ns))
        except OSError:
            continue
    key = (str(db_file), tuple(files))
    if key in _refusal_warned:
        return
    _refusal_warned.add(key)
    LOG.error("%s", exc)


def _refusal(directory, files):
    names = ", ".join(path.name for path in files)
    return MigrationRefused(
        f"REFUSED to import legacy JSON ({names}) into {db_path_for(directory)}: "
        "the database already holds data, so the files were left untouched and "
        "NOT imported. Move them away, or run `python orch_db.py --state-dir "
        f"{directory} migrate --force` (backs up orch.db first) to import them "
        "on purpose.")


def _open(db_file):
    created = not db_file.exists()
    conn = _connect(db_file)
    try:
        _ensure_schema(conn)
        files = legacy_files(db_file.parent)
        if files:
            # v0.21.1: decide a refusal with a plain read (WAL reader, no
            # BEGIN IMMEDIATE), so a stray JSON next to a DB with data never
            # makes every read queue for the write lock. Only an empty DB
            # takes the lock (and re-checks under it) to migrate.
            if not _is_empty(conn):
                _warn_refused(db_file, _refusal(db_file.parent, files))
            else:
                try:
                    _migrate_locked(conn, db_file.parent)
                except MigrationRefused as exc:     # filled in meanwhile
                    _warn_refused(db_file, exc)
    except BaseException:
        conn.close()
        if created:
            _remove_db_files(db_file)   # no empty orch.db left behind
        raise
    return conn


@contextmanager
def transaction(path):
    """BEGIN IMMEDIATE on the DB holding ``path``; re-entrant per thread."""
    db_file = db_path_for(path)
    key = str(db_file)
    active = _active()
    if key in active:
        active[key][1] += 1
        try:
            yield active[key][0]
        finally:
            active[key][1] -= 1
        return
    conn = _open(db_file)
    conn.execute("BEGIN IMMEDIATE")
    active[key] = [conn, 1]
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        active.pop(key, None)
        conn.close()


def in_transaction(path):
    return str(db_path_for(path)) in _active()


def _run(path, fn, write=False):
    key = str(db_path_for(path))
    active = _active()
    if key in active:
        return fn(active[key][0])
    if write:
        with transaction(path) as conn:
            return fn(conn)
    db_file = db_path_for(path)
    if not db_file.exists() and not legacy_files(db_file.parent):
        # Pure read of a store that was never created: answer from an empty
        # in-memory schema instead of creating state/orch.db as a side effect.
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
    else:
        conn = _open(db_file)
    try:
        return fn(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Rows <-> records
# ---------------------------------------------------------------------------

def _dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=False)


def _status_row(task_id, state):
    state = state if isinstance(state, dict) else {}
    return (str(task_id), state.get("status"), state.get("approval_status"),
            _dumps(state), _utc())


def _write_statuses(conn, statuses):
    if not isinstance(statuses, dict):
        raise TypeError("task statuses must be a dict")
    existing = {row[0] for row in conn.execute("SELECT task_id FROM task_status")}
    for task_id, state in statuses.items():
        conn.execute(
            "INSERT INTO task_status (task_id, status, approval_status, data, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE SET "
            "status=excluded.status, approval_status=excluded.approval_status, "
            "data=excluded.data, updated_at=excluded.updated_at",
            _status_row(task_id, state),
        )
    gone = existing - {str(key) for key in statuses}
    for task_id in gone:
        conn.execute("DELETE FROM task_status WHERE task_id=?", (task_id,))


def _log_row(table, record):
    if table == "events":
        return ("INSERT INTO events (ts, event, task_id, data) VALUES (?, ?, ?, ?)",
                (record.get("timestamp"), record.get("event"), record.get("task_id"),
                 _dumps(record)))
    if table == "auth_audit":
        return ("INSERT INTO auth_audit (ts, event, actor, data) VALUES (?, ?, ?, ?)",
                (record.get("ts_utc"), record.get("event"), record.get("actor"),
                 _dumps(record)))
    return ("INSERT INTO chat_usage (ts, data) VALUES (?, ?)",
            (record.get("created_at_utc") or record.get("timestamp"), _dumps(record)))


# ---------------------------------------------------------------------------
# Path-addressed API used by the modules
# ---------------------------------------------------------------------------

def _kind(path):
    try:
        return MANAGED[Path(path).name]
    except KeyError:
        raise ValueError(f"not a managed state name: {path}") from None


def load(path, default=None):
    kind, name = _kind(path)

    def read(conn):
        if kind == "status":
            rows = conn.execute("SELECT task_id, data FROM task_status ORDER BY rowid").fetchall()
            if not rows:
                return default
            return {task_id: json.loads(data) for task_id, data in rows}
        if kind == "doc":
            row = conn.execute("SELECT value FROM docs WHERE name=?", (name,)).fetchone()
            return default if row is None else json.loads(row[0])
        return [json.loads(row[0]) for row in
                conn.execute(f"SELECT data FROM {name} ORDER BY id")]

    return _run(path, read)


def save(path, data):
    kind, name = _kind(path)

    def write(conn):
        if kind == "status":
            _write_statuses(conn, data)
        elif kind == "doc":
            conn.execute(
                "INSERT INTO docs (name, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (name, _dumps(data), _utc()))
        else:
            raise ValueError(f"{name} is append-only")

    _run(path, write, write=True)


def exists(path):
    kind, name = _kind(path)

    def check(conn):
        if kind == "status":
            return conn.execute("SELECT 1 FROM task_status LIMIT 1").fetchone() is not None
        if kind == "doc":
            return conn.execute("SELECT 1 FROM docs WHERE name=?", (name,)).fetchone() is not None
        return conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone() is not None

    return _run(path, check)


def delete(path):
    kind, name = _kind(path)

    def remove(conn):
        if kind == "status":
            return conn.execute("DELETE FROM task_status").rowcount > 0
        if kind == "doc":
            return conn.execute("DELETE FROM docs WHERE name=?", (name,)).rowcount > 0
        raise ValueError(f"{name} is append-only")

    return _run(path, remove, write=True)


def append(path, record):
    kind, name = _kind(path)
    if kind != "log":
        raise ValueError(f"{name} is not a log")
    sql, params = _log_row(name, record)
    _run(path, lambda conn: conn.execute(sql, params), write=True)


def read_log(path, limit=None, newest_first=False, where_task_prefix=None):
    kind, name = _kind(path)
    if kind != "log":
        raise ValueError(f"{name} is not a log")
    sql = f"SELECT data FROM {name}"
    params = []
    if where_task_prefix and name == "events":
        sql += " WHERE task_id LIKE ? ESCAPE '\\'"
        params.append(where_task_prefix.replace("\\", "\\\\").replace("%", "\\%")
                      .replace("_", "\\_") + "%")
    sql += " ORDER BY id " + ("DESC" if newest_first else "ASC")
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return _run(path, lambda conn: [json.loads(r[0]) for r in conn.execute(sql, params)])


def load_task_state(path, task_id):
    row = _run(path, lambda conn: conn.execute(
        "SELECT data FROM task_status WHERE task_id=?", (str(task_id),)).fetchone())
    return None if row is None else json.loads(row[0])


def put_task_state_if(path, task_id, state, expected_approval_status):
    """Conditional write of one task's state: succeeds only if the stored
    approval_status still equals ``expected_approval_status`` (None = no
    decision yet / no row). Returns True when exactly one row changed, so
    two concurrent decisions can never both succeed."""
    row = _status_row(task_id, state)

    def write(conn):
        if expected_approval_status is None:
            changed = conn.execute(
                "UPDATE task_status SET status=?, approval_status=?, data=?, updated_at=? "
                "WHERE task_id=? AND approval_status IS NULL",
                (row[1], row[2], row[3], row[4], row[0])).rowcount
            if changed == 0:
                changed = conn.execute(
                    "INSERT OR IGNORE INTO task_status (task_id, status, approval_status, data, "
                    "updated_at) VALUES (?, ?, ?, ?, ?)", row).rowcount
            return changed == 1
        return conn.execute(
            "UPDATE task_status SET status=?, approval_status=?, data=?, updated_at=? "
            "WHERE task_id=? AND approval_status=?",
            (row[1], row[2], row[3], row[4], row[0], expected_approval_status)).rowcount == 1

    return _run(path, write, write=True)


def put_task_state(path, task_id, state):
    row = _status_row(task_id, state)
    _run(path, lambda conn: conn.execute(
        "INSERT INTO task_status (task_id, status, approval_status, data, updated_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE SET status=excluded.status, "
        "approval_status=excluded.approval_status, data=excluded.data, "
        "updated_at=excluded.updated_at", row), write=True)


# ---------------------------------------------------------------------------
# Migration from the JSON files
# ---------------------------------------------------------------------------

def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _parse_legacy(path):
    kind, _ = MANAGED[path.name]
    text = path.read_text(encoding="utf-8")
    if kind == "log":
        records = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue    # a torn last line in an old log is skipped
        return records
    if not text.strip():
        return {} if kind == "status" else None
    return json.loads(text)


def _is_empty(conn):
    return all(conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is None
               for table in DATA_TABLES)


def _canonical(record):
    return json.dumps(record, ensure_ascii=False, sort_keys=True)


def _upsert_statuses(conn, statuses):
    """Migration import: insert/update the file's task rows, never delete."""
    for task_id, state in statuses.items():
        conn.execute(
            "INSERT INTO task_status (task_id, status, approval_status, data, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE SET "
            "status=excluded.status, approval_status=excluded.approval_status, "
            "data=excluded.data, updated_at=excluded.updated_at",
            _status_row(task_id, state),
        )


def _append_new_logs(conn, table, records):
    """Append only records not already in ``table`` (multiset on canonical
    JSON), so re-importing a log or an export never duplicates rows."""
    present = Counter(_canonical(json.loads(row[0]))
                      for row in conn.execute(f"SELECT data FROM {table}"))
    count = skipped = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        key = _canonical(record)
        if present[key] > 0:
            present[key] -= 1
            skipped += 1
            continue
        conn.execute(*_log_row(table, record))
        count += 1
    return count, skipped


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _backup_dir(directory):
    base = Path(directory) / f"json-backup-{_stamp()}"
    candidate, n = base, 1
    while candidate.exists():
        n += 1
        candidate = Path(f"{base}-{n}")
    candidate.mkdir(parents=True, mode=0o700)
    os.chmod(candidate, 0o700)          # owner-only, whatever the umask
    return candidate


def _db_backup_locked(conn, directory):
    """Copy of the DB taken while the migration holds the write lock. A
    second (WAL reader) connection copies the committed state, so nothing
    can change between the backup and the import."""
    target = Path(directory) / f"orch.db.pre-migrate-{_stamp()}"
    n = 1
    while target.exists():
        n += 1
        target = Path(directory) / f"orch.db.pre-migrate-{_stamp()}-{n}"
    os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    reader = sqlite3.connect(str(db_path_for(directory)), timeout=BUSY_TIMEOUT_MS / 1000)
    dest = sqlite3.connect(str(target))
    try:
        reader.backup(dest)
    finally:
        dest.close()
        reader.close()
    os.chmod(target, 0o600)
    return target


def _migrate_locked(conn, directory, force=False):
    """Import every legacy file in ``directory`` (under BEGIN IMMEDIATE).

    Only into an empty DB unless ``force`` (then a DB backup is written
    first). Files are moved into a timestamped backup folder first; if
    anything fails the files are moved back and nothing is committed."""
    conn.execute("BEGIN IMMEDIATE")
    moved = []
    report = []
    backup = None
    try:
        files = legacy_files(directory)     # re-check under the write lock
        if not files:
            conn.execute("COMMIT")
            return report
        db_backup = None
        if not _is_empty(conn):
            if not force:
                raise _refusal(directory, files)
            db_backup = _db_backup_locked(conn, directory)
        backup = _backup_dir(directory)
        for path in files:
            target = backup / path.name
            os.replace(path, target)
            moved.append((target, path))
            os.chmod(target, 0o600)     # raw logs / audit trail: owner-only
        for target, original in moved:
            kind, name = MANAGED[original.name]
            digest = _sha256(target)
            seen = conn.execute("SELECT 1 FROM migrations WHERE name=? AND sha256=?",
                                (original.name, digest)).fetchone()
            count = duplicates = 0
            if not seen:
                try:
                    data = _parse_legacy(target)
                except ValueError as exc:   # name the file in the error
                    raise ValueError(f"{original.name} is not valid JSON ({exc})") from exc
                if kind == "status":
                    data = data if isinstance(data, dict) else {}
                    _upsert_statuses(conn, data)
                    count = len(data)
                elif kind == "doc":
                    if data is not None:
                        conn.execute(
                            "INSERT INTO docs (name, value, updated_at) VALUES (?, ?, ?) "
                            "ON CONFLICT(name) DO UPDATE SET value=excluded.value, "
                            "updated_at=excluded.updated_at", (name, _dumps(data), _utc()))
                        count = 1
                else:
                    count, duplicates = _append_new_logs(conn, name, data)
                conn.execute(
                    "INSERT INTO migrations (name, sha256, records, backup_path, migrated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (original.name, digest, count, str(target), _utc()))
            report.append({"file": original.name, "records": count,
                           "duplicates_skipped": duplicates,
                           "skipped_already_imported": bool(seen), "backup": str(target),
                           "db_backup": str(db_backup) if db_backup else None})
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('last_migration_utc', ?)", (_utc(),))
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        for target, original in moved:
            try:
                os.replace(target, original)
            except OSError:
                pass
        if backup is not None:
            try:
                backup.rmdir()          # only if empty: no stray empty folder
            except OSError:
                pass
        raise
    return report


def migrate(directory, force=False):
    """Explicit migration (CLI / startup); returns the per-file report.
    Raises MigrationRefused when legacy files sit next to a non-empty DB
    and ``force`` is not set."""
    db_file = db_path_for(Path(directory))
    created = not db_file.exists()
    if created and not legacy_files(directory):
        return []                       # nothing to do; do not create a DB
    conn = _connect(db_file)
    try:
        _ensure_schema(conn)
        return _migrate_locked(conn, Path(directory), force=force)
    except MigrationRefused:
        raise
    except BaseException:
        conn.close()
        if created:
            _remove_db_files(db_file)
        raise
    finally:
        conn.close()


def auto_migrate(directory):
    """Startup hook: migrate into an empty DB, otherwise log the refusal
    loudly and keep serving from the DB (the files stay untouched). The
    refusal is decided read-only (no write lock)."""
    directory = Path(directory)
    db_file = db_path_for(directory)
    files = legacy_files(directory)
    if files and db_file.exists():
        conn = _connect(db_file)
        try:
            _ensure_schema(conn)
            if not _is_empty(conn):
                _warn_refused(db_file, _refusal(directory, files))
                return []
        finally:
            conn.close()
    try:
        return migrate(directory)
    except MigrationRefused as exc:
        _warn_refused(db_file, exc)
        return []


def schema_version(path):
    return _run(path, lambda conn: int(conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]))


def migration_history(path):
    return _run(path, lambda conn: [
        {"name": r[0], "sha256": r[1], "records": r[2], "backup_path": r[3], "migrated_at": r[4]}
        for r in conn.execute("SELECT name, sha256, records, backup_path, migrated_at "
                              "FROM migrations ORDER BY id")])


# ---------------------------------------------------------------------------
# Backup / export (rollback to JSON)
# ---------------------------------------------------------------------------

def backup(path, target):
    """Consistent online copy with the SQLite backup API."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    source = _open(db_path_for(path))
    try:
        _create_private(target)
        dest = sqlite3.connect(str(target))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    restrict_db_files(target)
    return target


def export_json(path, out_dir):
    """Write the DB back out as the v0.20 JSON files (for a rollback);
    the files are created 0600 in a 0700 directory."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    base = db_path_for(path).parent
    written = []
    for file_name, (kind, name) in MANAGED.items():
        source = base / file_name
        target = out_dir / file_name
        if kind == "log":
            rows = read_log(source)
            if not rows:
                continue
            _write_private(target, "".join(_dumps(r) + "\n" for r in rows))
        else:
            if not exists(source):
                continue
            _write_private(target, json.dumps(load(source), ensure_ascii=False, indent=2))
        # every exported file is owner-only (auth hashes, customer / order
        # data in ecom_import.json, approvals and audit logs)
        written.append(file_name)
    return written


def ping(path):
    """Open (creating/migrating if needed) the DB and run a trivial query."""
    conn = _open(db_path_for(path))
    try:
        return int(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
    finally:
        conn.close()


def integrity_check(path):
    return _run(path, lambda conn: conn.execute("PRAGMA integrity_check").fetchone()[0])


def summary(path):
    def read(conn):
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("task_status", "docs") + LOG_TABLES}
        docs = [r[0] for r in conn.execute("SELECT name FROM docs ORDER BY name")]
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        return {"db": str(db_path_for(path)), "schema_version": int(conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]),
            "journal_mode": mode, "counts": counts, "docs": docs}
    return _run(path, read)


def default_state_dir():
    return Path(os.getenv("ORCH_STATE_DIR") or (PROJECT_ROOT / "state"))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="orch_db.py", description="ORCH SQLite state store")
    parser.add_argument("--state-dir", default=None,
                        help="state directory (default: ./state or ORCH_STATE_DIR)")
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("migrate", help="import state/*.json into an empty state/orch.db")
    m.add_argument("--force", action="store_true",
                   help="also import into a DB that already holds data (backs up orch.db "
                        "first; task rows upserted, never deleted; log rows deduplicated)")
    sub.add_parser("status", help="schema version, row counts, migrations")
    sub.add_parser("check", help="PRAGMA integrity_check")
    b = sub.add_parser("backup", help="consistent copy of orch.db (SQLite backup API)")
    b.add_argument("target")
    e = sub.add_parser("export", help="write the DB back as v0.20 JSON files (rollback)")
    e.add_argument("out_dir")
    args = parser.parse_args(argv)
    state_dir = Path(args.state_dir) if args.state_dir else default_state_dir()
    auth_dir = Path(os.getenv("ORCH_AUTH_DIR") or state_dir)

    if args.command == "migrate":
        dirs = [state_dir] + ([auth_dir] if auth_dir.resolve() != state_dir.resolve() else [])
        status = 0
        for directory in dirs:
            try:
                report = migrate(directory, force=args.force)
            except MigrationRefused as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                status = 1
                continue
            except (ValueError, OSError, sqlite3.Error) as exc:
                # v0.21.1: one clean line instead of a traceback. The import
                # rolled back and the files were moved back into place.
                detail = " ".join(str(exc).split()) or type(exc).__name__
                print(f"ERROR: migrate failed for {directory}: {detail} "
                      "(nothing imported; legacy files left in place)", file=sys.stderr)
                status = 1
                continue
            if db_path_for(directory).exists():
                print(f"{db_path_for(directory)}: schema_version {schema_version(directory)}")
            if not report:
                print(f"{directory}: nothing to migrate (no legacy JSON files)")
            if report and report[0].get("db_backup"):
                print(f"  database backed up first: {report[0]['db_backup']}")
            for row in report:
                note = " (already imported, skipped)" if row["skipped_already_imported"] else ""
                if row.get("duplicates_skipped"):
                    note += f" ({row['duplicates_skipped']} duplicate(s) skipped)"
                print(f"  {row['file']}: {row['records']} record(s){note}; backup {row['backup']}")
        return status
    if args.command == "status":
        print(json.dumps({"summary": summary(state_dir),
                          "migrations": migration_history(state_dir)},
                         ensure_ascii=False, indent=2))
        return 0
    if args.command == "check":
        result = integrity_check(state_dir)
        print(result)
        return 0 if result == "ok" else 1
    if args.command == "backup":
        print(backup(state_dir, args.target))
        return 0
    if args.command == "export":
        for name in export_json(state_dir, args.out_dir):
            print(Path(args.out_dir) / name)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
