#!/usr/bin/env bash
# ORCH v0.21.0 smoke test.
#
#   scripts/smoke.sh            # Docker: build, compose up (throw-away project
#                               # + volumes), /healthz, /setup, login, pages, teardown
#   scripts/smoke.sh --local    # no Docker: same checks against serve.py run from
#                               # a clean `git archive` copy (temp state/, SQLite)
#
# Env: SMOKE_PORT (default 5059), SMOKE_PASSWORD, PYTHON (local mode).
set -euo pipefail

MODE=docker
[[ "${1:-}" == "--local" ]] && MODE=local
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${SMOKE_PORT:-5059}"
BASE="http://127.0.0.1:$PORT"
ADMIN="Smoke Admin"
PASSWORD="${SMOKE_PASSWORD:-Smoke-Test-Pass-$RANDOM-x}"
WORK="$(mktemp -d)"
JAR="$WORK/cookies.txt"
PROJECT="orch-smoke-$$"
PID=""

cleanup() {
  if [[ "$MODE" == docker ]]; then
    (cd "$ROOT" && ORCH_PUBLISH_PORT="$PORT" docker compose -p "$PROJECT" down -v --remove-orphans >/dev/null 2>&1) || true
  elif [[ -n "$PID" ]]; then
    kill "$PID" 2>/dev/null || true; wait "$PID" 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

fail() { echo "SMOKE FAIL: $*" >&2; [[ -f "$WORK/server.log" ]] && tail -20 "$WORK/server.log" >&2; exit 1; }
csrf() { sed -n 's/.*name="csrf_token" value="\([^"]*\)".*/\1/p' | head -1; }
code() { curl -s -o "$WORK/body" -w '%{http_code}' -b "$JAR" -c "$JAR" "$@"; }

if [[ "$MODE" == docker ]]; then
  command -v docker >/dev/null || fail "docker not found (run with --local to smoke-test without Docker)"
  cd "$ROOT"
  echo "== build + up ($PROJECT, port $PORT)"
  ORCH_PUBLISH_PORT="$PORT" docker compose -p "$PROJECT" up -d --build
else
  PY="${PYTHON:-python3}"
  [[ -x "$ROOT/.venv/bin/python" ]] && PY="$ROOT/.venv/bin/python"
  echo "== local: git archive HEAD -> $WORK/app, serve.py on $PORT"
  mkdir -p "$WORK/app"
  git -C "$ROOT" archive HEAD | tar -x -C "$WORK/app"
  (cd "$WORK/app" && env -u ORCH_UI_SECRET_KEY -u ORCH_AUTH_DIR ORCH_PERSIST_SECRET_KEY=1 \
     ORCH_DEMO_FORCE_MOCK=1 ORCH_HOST=127.0.0.1 ORCH_PORT="$PORT" \
     "$PY" serve.py >"$WORK/server.log" 2>&1) &
  PID=$!
fi

echo "== wait for /healthz"
for _ in $(seq 1 60); do
  if curl -fsS "$BASE/healthz" -o "$WORK/health" 2>/dev/null; then break; fi
  sleep 2
done
grep -q '"ok": *true' "$WORK/health" 2>/dev/null || fail "/healthz not healthy"
cat "$WORK/health"; echo

echo "== setup mode: every page redirects to /setup"
[[ "$(code "$BASE/")" == 302 ]] || fail "/ should redirect before setup"
[[ "$(code "$BASE/setup")" == 503 ]] || fail "/setup page"
TOKEN="$(csrf < "$WORK/body")"; [[ -n "$TOKEN" ]] || fail "no csrf token on /setup"
[[ "$(code -X POST "$BASE/setup" --data-urlencode "csrf_token=$TOKEN" \
      --data-urlencode "username=$ADMIN" --data-urlencode "password=$PASSWORD" \
      --data-urlencode "password_confirm=$PASSWORD")" == 302 ]] || fail "setup POST"
[[ "$(code "$BASE/setup")" == 302 ]] || fail "/setup must close once an account exists"

echo "== login"
[[ "$(code "$BASE/login")" == 200 ]] || fail "/login"
TOKEN="$(csrf < "$WORK/body")"
[[ "$(code -X POST "$BASE/login" --data-urlencode "csrf_token=$TOKEN" \
      --data-urlencode "username=$ADMIN" --data-urlencode "password=$PASSWORD")" == 302 ]] || fail "login POST"

echo "== main pages"
for path in / /tasks /events /chat /inbox /audit /sales /import /admin/users /admin/permissions /admin/permissions.csv; do
  status="$(code "$BASE$path")"
  [[ "$status" == 200 ]] || fail "$path -> $status"
  echo "  $path 200"
done
grep -q "Smoke Admin" "$WORK/body" || fail "permissions export missing the admin"
code "$BASE/" >/dev/null; grep -q 'data-app-version' "$WORK/body" || fail "no version in footer"
echo "SMOKE OK ($MODE)"
