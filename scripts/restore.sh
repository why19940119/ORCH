#!/usr/bin/env bash
# ORCH v0.21.1 restore from scripts/backup.sh output. REPLACES the current
# state/ uploads/ data/ artifacts/ (and output/ in Docker); a safety backup
# is taken first (backups/pre-restore/).
#
# The archive is validated on the host FIRST (gzip integrity, tar listing,
# expected members, safe paths, regular files and folders ONLY - symlinks,
# hardlinks, devices and FIFOs are refused - SQLite header + integrity_check
# of the DB snapshot). A bad archive aborts before the service is stopped and
# before the safety backup is taken, so nothing changes.
#
# The restore itself never deletes live data first: every member is checked
# again and extracted into a staging folder inside each data folder (same
# filesystem / Docker volume), the DB is verified there, then the live
# content is renamed aside, the staged content renamed in, and the old
# content removed only after `orch_db.py check` succeeds. Any failure rolls
# the swap back, so the live data stays as it was.
#
#   scripts/restore.sh backups/orch-backup-XXXX.tar.gz            # Docker
#   scripts/restore.sh --local backups/orch-backup-XXXX.tar.gz    # plain checkout
#
# The app must be stopped (Docker mode stops and restarts the service). The
# archive (0600, owned by you) is streamed into the one-off container on
# stdin, so the container user (uid 10001) never needs to read host files.
set -euo pipefail

MODE=docker
if [[ "${1:-}" == "--local" ]]; then MODE=local; shift; fi
ARCHIVE="${1:?usage: restore.sh [--local] <backup.tar.gz>}"
[[ -f "$ARCHIVE" ]] || { echo "no such file: $ARCHIVE" >&2; exit 1; }
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE="$(cd "$(dirname "$ARCHIVE")" && pwd)/$(basename "$ARCHIVE")"
umask 077

if [[ "$MODE" == local ]]; then
  DIRS="state uploads data artifacts"
else
  DIRS="state uploads data artifacts output"
fi

# Runs with python (the image's, or $PY locally) in the app directory and
# reads the archive from STDIN. Exit 1 = refused / failed and rolled back
# (live data unchanged); exit 3 = the rollback itself was incomplete.
INNER="$(cat <<'PYINNER'
import os, shutil, signal, sqlite3, subprocess, sys, tarfile, tempfile

ALLOWED = {"state", "uploads", "data", "artifacts", "output"}
DIRS = os.environ["DIRS"].split()
os.umask(0o077)
stage, old, originals, moved_old = {}, {}, {}, {}


def on_term(signum, frame):
    raise SystemExit(130)


signal.signal(signal.SIGTERM, on_term)


def split_member(member):
    if not (member.isfile() or member.isdir()):
        raise ValueError("refusing a link or special member: " + member.name)
    name = member.name
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if name.startswith("/") or ".." in parts or "\\" in name:
        raise ValueError("unsafe path in the archive: " + name)
    if parts and parts[-1].startswith("._"):
        return []                         # macOS AppleDouble metadata: skipped
    if parts and parts[0] not in ALLOWED:
        raise ValueError("unexpected path in the archive: " + name)
    return parts


def set_mode(path, mode):
    # never follows a link (none can exist: links are refused above)
    if os.path.isfile(path) and not os.path.islink(path):
        os.chmod(path, mode)


def extract(fileobj):
    with tarfile.open(fileobj=fileobj, mode="r|gz") as tar:
        for member in tar:
            parts = split_member(member)
            if not parts or parts[0] not in stage:
                continue                  # "./" itself, or output/ in local mode
            target = os.path.join(stage[parts[0]], *parts[1:])
            if member.isdir():
                os.makedirs(target, mode=0o700, exist_ok=True)
                continue
            if len(parts) < 2:
                raise ValueError("a file where a data folder is expected: " + member.name)
            os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(target, flags, 0o600)
            with os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(tar.extractfile(member), out)
            set_mode(target, 0o600 | (member.mode & 0o100))


def verify_staged_db():
    sdir = stage["state"]
    snap = os.path.join(sdir, ".snapshot", "orch.db")
    if not os.path.isfile(snap) or os.path.islink(snap):
        raise ValueError("not an ORCH backup (no state/.snapshot/orch.db)")
    with open(snap, "rb") as fh:
        if fh.read(16) != b"SQLite format 3\x00":
            raise ValueError("state/.snapshot/orch.db is not a SQLite database")
    conn = sqlite3.connect(snap)
    try:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if ok != "ok":
        raise ValueError("state/.snapshot/orch.db failed PRAGMA integrity_check")
    for name in ("orch.db", "orch.db-wal", "orch.db-shm"):
        path = os.path.join(sdir, name)
        if os.path.lexists(path):
            os.remove(path)
    os.rename(snap, os.path.join(sdir, "orch.db"))
    shutil.rmtree(os.path.join(sdir, ".snapshot"))
    set_mode(os.path.join(sdir, "orch.db"), 0o600)
    set_mode(os.path.join(sdir, "secret_key"), 0o600)


def swap():
    for d in DIRS:
        old[d] = tempfile.mkdtemp(prefix=".restore-old-", dir=d)
        originals[d] = {e for e in os.listdir(d) if not e.startswith(".restore-")}
        moved_old[d] = set()
        for entry in sorted(originals[d]):
            os.rename(os.path.join(d, entry), os.path.join(old[d], entry))
            moved_old[d].add(entry)
        for entry in sorted(os.listdir(stage[d])):
            os.rename(os.path.join(stage[d], entry), os.path.join(d, entry))


def rollback():
    clean = True
    for d in old:
        for entry in os.listdir(d):
            if entry.startswith(".restore-"):
                continue
            if entry in originals.get(d, ()) and entry not in moved_old.get(d, ()):
                continue                  # never moved: still the live original
            try:
                os.rename(os.path.join(d, entry), os.path.join(stage[d], entry))
            except OSError:
                clean = False
        for entry in moved_old.get(d, ()):
            try:
                os.rename(os.path.join(old[d], entry), os.path.join(d, entry))
            except OSError:
                clean = False
    return clean


def cleanup():
    for path in list(stage.values()) + list(old.values()):
        shutil.rmtree(path, ignore_errors=True)


try:
    for d in DIRS:
        os.makedirs(d, mode=0o700, exist_ok=True)
        stage[d] = tempfile.mkdtemp(prefix=".restore-new-", dir=d)
    extract(sys.stdin.buffer)
    verify_staged_db()
    swap()
    check = subprocess.run([sys.executable, "orch_db.py", "--state-dir", "state", "check"])
    if check.returncode != 0:
        raise ValueError("orch_db.py check failed on the restored database")
except BaseException as exc:
    clean = rollback()
    if clean:
        cleanup()
    detail = str(exc) if not isinstance(exc, SystemExit) else "interrupted"
    sys.stderr.write("RESTORE FAILED: %s: %s\n" % (type(exc).__name__, detail))
    if clean:
        sys.stderr.write("The live data was left unchanged.\n")
        sys.exit(1)
    sys.stderr.write("ROLLBACK INCOMPLETE: the previous data is in the .restore-old-* folders "
                     "inside each data folder; restore backups/pre-restore/ to recover.\n")
    sys.exit(3)
cleanup()
PYINNER
)"

fail() {
  echo "RESTORE ABORTED: $1." >&2
  echo "Nothing was changed: the service was not stopped and no safety backup was taken." >&2
  exit 1
}

PY="${PYTHON:-python3}"
[[ -x "$ROOT/.venv/bin/python" ]] && PY="$ROOT/.venv/bin/python"

# Host-side validation, before anything is stopped or backed up.
validate_archive() {
  CHECK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/orch-restore-check.XXXXXX")"
  trap 'rm -rf "$CHECK_DIR"' EXIT
  local list="$CHECK_DIR/list" db="$CHECK_DIR/orch.db"
  # gzip exit 2 is only a warning (bsdtar pads its stream with zero blocks).
  gzip -t < "$ARCHIVE" 2>/dev/null || [[ $? -eq 2 ]] \
    || fail "$ARCHIVE is not a valid gzip file (corrupt or truncated)"
  tar -tzf "$ARCHIVE" > "$list" 2>/dev/null || fail "$ARCHIVE is not a readable tar archive"
  grep -x "./state/.snapshot/orch.db" "$list" >/dev/null \
    || fail "not an ORCH backup (no state/.snapshot/orch.db)"
  grep -Ev '^\./((state|uploads|data|artifacts|output)(/.*)?)?$' "$list" > "$CHECK_DIR/bad" || true
  grep -E '(^|/)\.\.(/|$)' "$list" >> "$CHECK_DIR/bad" || true
  if [[ -s "$CHECK_DIR/bad" ]]; then
    fail "unexpected or unsafe path in the archive: $(head -n 1 "$CHECK_DIR/bad")"
  fi
  # Regular files and folders only (works without python: the first column
  # of a verbose listing is the type: l = symlink, h = hardlink, c/b/p ...).
  tar -tvzf "$ARCHIVE" > "$CHECK_DIR/verbose" 2>/dev/null || fail "$ARCHIVE is not a readable tar archive"
  grep -Ev '^[-d]' "$CHECK_DIR/verbose" > "$CHECK_DIR/links" || true
  if [[ -s "$CHECK_DIR/links" ]]; then
    fail "the archive contains a link or special file (only regular files and folders are allowed)"
  fi
  if command -v "$PY" >/dev/null 2>&1; then
    "$PY" - "$ARCHIVE" <<'PY' || fail "the archive contains a link, special file or unsafe path"
import sys, tarfile
try:
    with tarfile.open(sys.argv[1], "r:gz") as tar:
        for m in tar:
            parts = [p for p in m.name.split("/") if p not in ("", ".")]
            if not (m.isfile() or m.isdir()) or m.name.startswith("/") or ".." in parts:
                sys.exit(1)
except (tarfile.TarError, OSError, EOFError):
    sys.exit(1)
PY
  fi
  tar -xzOf "$ARCHIVE" ./state/.snapshot/orch.db > "$db" 2>/dev/null \
    || fail "cannot extract state/.snapshot/orch.db"
  [[ "$(head -c 15 "$db")" == "SQLite format 3" ]] \
    || fail "state/.snapshot/orch.db is not a SQLite database"
  if command -v "$PY" >/dev/null 2>&1; then
    "$PY" - "$db" <<'PY' || fail "state/.snapshot/orch.db failed PRAGMA integrity_check"
import sqlite3, sys
try:
    ok = sqlite3.connect(sys.argv[1]).execute("PRAGMA integrity_check").fetchone()[0]
except sqlite3.Error:
    sys.exit(1)
sys.exit(0 if ok == "ok" else 1)
PY
  fi
  rm -rf "$CHECK_DIR"
  trap - EXIT
}

cd "$ROOT"
echo "Validating $ARCHIVE..."
validate_archive
echo "Taking a safety backup of the current data first..."
if [[ "$MODE" == local ]]; then
  BACKUP_DIR="$ROOT/backups/pre-restore" "$ROOT/scripts/backup.sh" --local
  if ! DIRS="$DIRS" "$PY" -c "$INNER" < "$ARCHIVE"; then
    echo "RESTORE FAILED - see above. A safety backup is in $ROOT/backups/pre-restore/." >&2
    exit 1
  fi
else
  command -v docker >/dev/null || { echo "docker not found (use --local)" >&2; exit 1; }
  BACKUP_DIR="$ROOT/backups/pre-restore" "$ROOT/scripts/backup.sh"
  docker compose stop orch || true
  status=0
  docker compose run --rm --no-deps -T -e DIRS="$DIRS" --entrypoint python orch -c "$INNER" < "$ARCHIVE" || status=$?
  if [[ "$status" -ne 0 ]]; then
    if [[ "$status" -eq 1 ]]; then
      echo "RESTORE FAILED - the live data was rolled back unchanged; restarting the service." >&2
      docker compose up -d orch || true
    else
      echo "RESTORE FAILED - the service is left stopped. The data from before the restore" >&2
      echo "is in $ROOT/backups/pre-restore/ (scripts/restore.sh <that archive>)." >&2
    fi
    exit 1
  fi
  docker compose up -d orch
fi
echo "Restored from $ARCHIVE. Schema upgrades (if any) run automatically on start."
