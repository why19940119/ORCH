#!/usr/bin/env bash
# ORCH v0.21.0 restore from scripts/backup.sh output. REPLACES the current
# state/ uploads/ data/ artifacts/ (a safety backup is taken first).
#
#   scripts/restore.sh backups/orch-backup-XXXX.tar.gz            # Docker
#   scripts/restore.sh --local backups/orch-backup-XXXX.tar.gz    # plain checkout
#
# The app must be stopped (Docker mode stops and restarts the service).
set -euo pipefail

MODE=docker
if [[ "${1:-}" == "--local" ]]; then MODE=local; shift; fi
ARCHIVE="${1:?usage: restore.sh [--local] <backup.tar.gz>}"
[[ -f "$ARCHIVE" ]] || { echo "no such file: $ARCHIVE" >&2; exit 1; }
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE_DIR="$(cd "$(dirname "$ARCHIVE")" && pwd)"
ARCHIVE_NAME="$(basename "$ARCHIVE")"

INNER='set -e
tar -tzf "$IN" | grep -q "^./state/.snapshot/orch.db$" || { echo "not an ORCH backup (no state/.snapshot/orch.db)" >&2; exit 1; }
for d in state uploads data artifacts; do mkdir -p "$d"; find "$d" -mindepth 1 -delete; done
tar -xzf "$IN"
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
  IN="$ARCHIVE_DIR/$ARCHIVE_NAME" PATH="$(dirname "$PY"):$PATH" bash -c "$INNER"
else
  command -v docker >/dev/null || { echo "docker not found (use --local)" >&2; exit 1; }
  BACKUP_DIR="$ROOT/backups/pre-restore" "$ROOT/scripts/backup.sh"
  docker compose stop orch || true
  docker compose run --rm --no-deps -T -v "$ARCHIVE_DIR:/restore:ro" \
    -e IN="/restore/$ARCHIVE_NAME" --entrypoint sh orch -c "$INNER"
  docker compose up -d orch
fi
echo "Restored from $ARCHIVE. Schema upgrades (if any) run automatically on start."
