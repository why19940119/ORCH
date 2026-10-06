"""v0.21.1 hardening (follow-up to the PR #7 re-review)."""

import io
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import orch_db
from test_orch_db_v0210 import TempState

ROOT = Path(__file__).resolve().parents[1]


class StrayJsonReadTests(TempState):
    """Item 2: a refused stray JSON never makes reads take the write lock."""

    def seed(self):
        orch_db.save(self.status, {"task_a": {"status": "done"}})
        orch_db.append(self.events, {"event": "e", "task_id": "task_a"})
        self.status.write_text(json.dumps({"stray": {}}), encoding="utf-8")   # refused file
        orch_db._refusal_warned.clear()

    def test_concurrent_reads_while_another_connection_holds_the_write_lock(self):
        self.seed()
        errors, results = [], []
        holder = sqlite3.connect(str(self.state / "orch.db"), isolation_level=None)
        try:
            holder.execute("BEGIN IMMEDIATE")          # a writer is busy right now
            holder.execute("INSERT INTO docs VALUES ('busy', '1', 'x')")

            def reader():
                try:
                    for _ in range(5):
                        results.append(orch_db.load(self.status))
                        orch_db.read_log(self.events)
                        orch_db.summary(self.state)
                except Exception as exc:        # 'database is locked' before the fix
                    errors.append(exc)

            with patch.object(orch_db, "BUSY_TIMEOUT_MS", 300), \
                    self.assertLogs("orch.db", "ERROR") as logged:
                threads = [threading.Thread(target=reader) for _ in range(8)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(30)
                self.assertEqual(orch_db.auto_migrate(self.state), [])
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 40)
        self.assertTrue(all(r == {"task_a": {"status": "done"}} for r in results))
        self.assertEqual(len(logged.records), 1)                 # warned once per process
        self.assertIn("REFUSED", logged.output[0])
        self.assertTrue(self.status.is_file())                   # file left untouched

    def test_empty_db_still_migrates(self):
        self.status.write_text(json.dumps({"task_new": {"status": "todo"}}), encoding="utf-8")
        self.assertEqual(orch_db.load(self.status), {"task_new": {"status": "todo"}})
        self.assertFalse(self.status.exists())


class JsonBackupModeTests(TempState):
    """Item 3: json-backup-*/ is 0700 and the moved files are 0600."""

    def test_backup_dir_and_moved_files_are_owner_only(self):
        old = os.umask(0o022)
        try:
            self.status.write_text(json.dumps({"t": {"status": "todo"}}), encoding="utf-8")
            self.events.write_text(json.dumps({"event": "e"}) + "\n", encoding="utf-8")
            audit = self.state / "auth_audit.jsonl"
            audit.write_text(json.dumps({"event": "login"}) + "\n", encoding="utf-8")
            for path in (self.status, self.events, audit):
                os.chmod(path, 0o644)
            report = orch_db.migrate(self.state)
        finally:
            os.umask(old)
        self.assertEqual({row["file"] for row in report},
                         {"task_status.json", "events.jsonl", "auth_audit.jsonl"})
        backups = list(self.state.glob("json-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o700)
        moved = sorted(p.name for p in backups[0].iterdir())
        self.assertEqual(moved, ["auth_audit.jsonl", "events.jsonl", "task_status.json"])
        for path in backups[0].iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)


class MigrateCliErrorTests(TempState):
    """Nit (b): a failed migrate prints one clean line and exits 1."""

    def test_bad_json_gives_one_line_error_and_exit_1(self):
        self.status.write_text("{not json", encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
                patch.dict(os.environ, {"ORCH_AUTH_DIR": str(self.state)}):
            code = orch_db.main(["--state-dir", str(self.state), "migrate"])
        self.assertEqual(code, 1)
        lines = err.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1, err.getvalue())
        self.assertTrue(lines[0].startswith("ERROR: migrate failed for "))
        self.assertIn("task_status.json is not valid JSON", lines[0])
        self.assertNotIn("Traceback", err.getvalue())
        self.assertTrue(self.status.is_file())          # moved back, untouched

    def test_cli_subprocess_exit_code(self):
        self.status.write_text("{not json", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(ROOT / "orch_db.py"), "--state-dir", str(self.state), "migrate"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=60,
            env={**os.environ, "ORCH_AUTH_DIR": str(self.state)})
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(len(proc.stderr.strip().splitlines()), 1, proc.stderr)


if __name__ == "__main__":
    unittest.main()
