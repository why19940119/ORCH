#!/usr/bin/env bash
# ORCH v0.21.1 backup: one .tar.gz with state/ (consistent SQLite snapshot via
# the SQLite backup API, never a raw copy of a live orch.db), uploads/, data/,
# artifacts/ (and output/ in Docker).
#
#   scripts/backup.sh                 # Docker (docker compose service "orch")
#   scripts/backup.sh --local         # a plain checkout (./state ...), no Docker
#   BACKUP_DIR=/path scripts/backup.sh
#
# The archive contains state/secret_key and the DB with password hashes: it is
# created 0600 (umask 077). In Docker the archive is streamed to stdout of the
# one-off container and written by THIS shell, so the container user (uid
# 10001) never needs write access to the host backup folder.
# Restore with scripts/restore.sh <archive>.
set -euo pipefail

MODE=docker
[[ "${1:-}" == "--local" ]] && MODE=local
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$ROOT/backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="orch-backup-$STAMP.tar.gz"

umask 077
mkdir -p "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"
OUT="$BACKUP_DIR/$NAME"
PART="$OUT.partial"

if [[ "$MODE" == local ]]; then
  DIRS="./state ./uploads ./data ./artifacts"     # output/ is tracked in git locally
else
  DIRS="./state ./uploads ./data ./artifacts ./output"
fi

# Runs in the app directory and writes the tar.gz to STDOUT. The snapshot
# folder is removed on every exit path (success, error, Ctrl-C).
INNER='set -e
umask 077
export COPYFILE_DISABLE=1   # macOS bsdtar: no ._* AppleDouble members
trap "rm -rf state/.snapshot" EXIT
trap "exit 130" INT TERM
rm -rf state/.snapshot && mkdir -p state/.snapshot
python orch_db.py --state-dir state backup state/.snapshot/orch.db >/dev/null
python orch_db.py --state-dir state status > state/.snapshot/status.json
# The manifest: every member of this archive, in archive order. tar archives
# exactly this list (no recursion; a file that vanished fails the backup) and
# restore.sh refuses an archive that lacks any listed member, so a cut or
# partial archive can never be restored. It is the 3rd member, before the DB
# snapshot, so a cut before it still lacks the snapshot. Skipped: the live DB
# files, macOS ._* metadata, .restore-* staging leftovers.
{
  echo "# members of this backup (scripts/backup.sh) - restore.sh requires every one"
  printf "%s\n" ./state ./state/.snapshot ./state/.snapshot/required.txt \
    ./state/.snapshot/orch.db ./state/.snapshot/status.json
  find $DIRS \( -path ./state/.snapshot -o -name ".restore-*" \) -prune -o \
    \( ! -path ./state ! -path ./state/orch.db ! -path ./state/orch.db-wal \
       ! -path ./state/orch.db-shm ! -name "._*" -print \)
} > state/.snapshot/required.txt
if grep -F "\\" state/.snapshot/required.txt >&2; then
  echo "backup failed: a file name above contains a backslash - rename it first" >&2
  exit 1
fi
grep -v "^#" state/.snapshot/required.txt | tar -czf - --no-recursion -T -'

cleanup() { rm -f "$PART" "$PART.list"; }
trap cleanup EXIT

cd "$ROOT"
if [[ "$MODE" == local ]]; then
  PY="${PYTHON:-python3}"
  [[ -x "$ROOT/.venv/bin/python" ]] && PY="$ROOT/.venv/bin/python"
  mkdir -p state uploads data artifacts
  DIRS="$DIRS" PATH="$(dirname "$PY"):$PATH" bash -c "$INNER" > "$PART"
else
  command -v docker >/dev/null || { echo "docker not found (use --local for a plain checkout)" >&2; exit 1; }
  # One-off container of the same image with the same volumes: works whether
  # or not the app is running (WAL + backup API = consistent snapshot).
  docker compose run --rm --no-deps -T -e DIRS="$DIRS" --entrypoint sh orch -c "$INNER" > "$PART"
fi

# A complete archive always holds the DB snapshot and (checked with python
# when the host has it: raw names, no locale escaping) every member listed in
# state/.snapshot/required.txt.
LC_ALL=C tar -tzf "$PART" > "$PART.list"
if ! grep -x "./state/.snapshot/orch.db" "$PART.list" >/dev/null; then
  echo "backup failed: archive has no state/.snapshot/orch.db" >&2
  exit 1
fi
rm -f "$PART.list"
PY="${PY:-${PYTHON:-python3}}"
if "$PY" -c "import tarfile" >/dev/null 2>&1; then
  "$PY" - "$PART" <<'PYCHECK' || exit 1
import sys, tarfile
with tarfile.open(sys.argv[1], "r:gz") as tar:
    names = {m.name.rstrip("/") for m in tar}
    listed = tar.extractfile("./state/.snapshot/required.txt").read().decode("utf-8", "surrogateescape")
for line in listed.split("\n"):
    if line and not line.startswith("#") and line.rstrip("/") not in names:
        sys.stderr.write("backup failed: %s changed while the backup ran - run it again\n" % line[2:])
        sys.exit(1)
PYCHECK
fi
chmod 600 "$PART"
mv "$PART" "$OUT"
trap - EXIT
echo "$OUT"
