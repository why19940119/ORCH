#!/usr/bin/env bash
# ORCH v0.21.1 restore from scripts/backup.sh output. REPLACES the current
# state/ uploads/ data/ artifacts/ (and output/ in Docker); a safety backup
# is taken first (backups/pre-restore/).
#
# The archive is validated on the host FIRST (gzip integrity, tar listing,
# expected members, safe paths, SQLite header + integrity_check of the DB
# snapshot). A bad archive aborts before the service is stopped and before
# the safety backup is taken, so nothing changes.
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

# Reads the archive from STDIN into a private temp file, checks it, then
# replaces the data folders. Nothing is deleted unless the archive is valid.
INNER='set -e
umask 077
TMP="$(mktemp -d "${TMPDIR:-/tmp}/orch-restore.XXXXXX")"
trap "rm -rf \"$TMP\"" EXIT
trap "exit 130" INT TERM
cat > "$TMP/in.tgz"
tar -tzf "$TMP/in.tgz" > "$TMP/list"
grep -x "./state/.snapshot/orch.db" "$TMP/list" >/dev/null || { echo "not an ORCH backup (no state/.snapshot/orch.db)" >&2; exit 1; }
for d in $DIRS; do mkdir -p "$d"; find "$d" -mindepth 1 -delete; done
tar -xzf "$TMP/in.tgz"
mv state/.snapshot/orch.db state/orch.db
rm -rf state/.snapshot
chmod 600 state/orch.db state/secret_key 2>/dev/null || true
python orch_db.py --state-dir state check'

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
  DIRS="$DIRS" PATH="$(dirname "$PY"):$PATH" bash -c "$INNER" < "$ARCHIVE"
else
  command -v docker >/dev/null || { echo "docker not found (use --local)" >&2; exit 1; }
  BACKUP_DIR="$ROOT/backups/pre-restore" "$ROOT/scripts/backup.sh"
  docker compose stop orch || true
  if ! docker compose run --rm --no-deps -T -e DIRS="$DIRS" --entrypoint sh orch -c "$INNER" < "$ARCHIVE"; then
    echo "RESTORE FAILED - the service is left stopped. The data from before the restore" >&2
    echo "is in $ROOT/backups/pre-restore/ (scripts/restore.sh <that archive>)." >&2
    exit 1
  fi
  docker compose up -d orch
fi
echo "Restored from $ARCHIVE. Schema upgrades (if any) run automatically on start."
