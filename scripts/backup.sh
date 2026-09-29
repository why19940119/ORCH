#!/usr/bin/env bash
# ORCH v0.21.0 backup: one .tar.gz with state/ (consistent SQLite snapshot via
# the SQLite backup API, never a raw copy of a live orch.db), uploads/, data/
# and artifacts/.
#
#   scripts/backup.sh                 # Docker (docker compose service "orch")
#   scripts/backup.sh --local         # a plain checkout (./state ...), no Docker
#   BACKUP_DIR=/path scripts/backup.sh
#
# Restore with scripts/restore.sh <archive>.
set -euo pipefail

MODE=docker
[[ "${1:-}" == "--local" ]] && MODE=local
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$ROOT/backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="orch-backup-$STAMP.tar.gz"
mkdir -p "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"

# Inside the app directory: snapshot the DB, then tar everything except the
# live database files (the snapshot replaces them on restore).
INNER='set -e
rm -rf state/.snapshot && mkdir -p state/.snapshot
python orch_db.py --state-dir state backup state/.snapshot/orch.db >/dev/null
python orch_db.py --state-dir state status > state/.snapshot/status.json
tar -czf "$OUT" --exclude="./state/orch.db" --exclude="./state/orch.db-wal" \
    --exclude="./state/orch.db-shm" ./state ./uploads ./data ./artifacts
rm -rf state/.snapshot'

cd "$ROOT"
if [[ "$MODE" == local ]]; then
  PY="${PYTHON:-python3}"
  [[ -x "$ROOT/.venv/bin/python" ]] && PY="$ROOT/.venv/bin/python"
  mkdir -p state uploads data artifacts
  OUT="$BACKUP_DIR/$NAME" PATH="$(dirname "$PY"):$PATH" bash -c "$INNER"
else
  command -v docker >/dev/null || { echo "docker not found (use --local for a plain checkout)" >&2; exit 1; }
  # Runs in a one-off container of the same image with the same volumes, so
  # it works whether or not the app is running (WAL + backup API = consistent).
  docker compose run --rm --no-deps -T -v "$BACKUP_DIR:/backup" -e OUT="/backup/$NAME" \
    --entrypoint sh orch -c "$INNER"
fi
echo "$BACKUP_DIR/$NAME"
