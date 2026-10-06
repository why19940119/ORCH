#!/usr/bin/env bash
# ORCH v0.21.0 restore from scripts/backup.sh output. REPLACES the current
# state/ uploads/ data/ artifacts/ (and output/ in Docker); a safety backup
# is taken first (backups/pre-restore/).
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

cd "$ROOT"
echo "Taking a safety backup of the current data first..."
if [[ "$MODE" == local ]]; then
  BACKUP_DIR="$ROOT/backups/pre-restore" "$ROOT/scripts/backup.sh" --local
  PY="${PYTHON:-python3}"
  [[ -x "$ROOT/.venv/bin/python" ]] && PY="$ROOT/.venv/bin/python"
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
