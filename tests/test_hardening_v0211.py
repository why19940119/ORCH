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


if __name__ == "__main__":
    unittest.main()
