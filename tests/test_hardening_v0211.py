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


class RestoreValidatesFirstTests(unittest.TestCase):
    """Item 4: restore.sh validates the archive before it stops the service
    or takes the safety backup. Docker is stubbed: a fake ``docker`` on PATH
    logs every call, so "service untouched" means "docker never called"."""

    def setUp(self):
        import shutil
        import tempfile
        self.app = Path(tempfile.mkdtemp(prefix="orch_restore_v0211_"))
        self.addCleanup(shutil.rmtree, self.app, True)
        (self.app / "scripts").mkdir()
        for name in ("backup.sh", "restore.sh"):
            shutil.copy2(ROOT / "scripts" / name, self.app / "scripts" / name)
        shutil.copy2(ROOT / "orch_db.py", self.app / "orch_db.py")
        self.state = self.app / "state"
        self.state.mkdir()
        orch_db.save(self.state / "task_status.json", {"task_a": {"status": "done"}})
        (self.app / "uploads").mkdir()
        (self.app / "uploads" / "a.txt").write_text("upload")
        self.fakebin = self.app / "fakebin"
        self.fakebin.mkdir()
        self.docker_log = self.app / "docker.log"
        fake = self.fakebin / "docker"
        fake.write_text(f"#!/bin/sh\necho \"$*\" >> '{self.docker_log}'\nexit 0\n")
        os.chmod(fake, 0o755)

    def run_restore(self, archive, *flags):
        env = {k: v for k, v in os.environ.items() if not k.startswith("ORCH_")}
        env.update({"PYTHON": sys.executable,
                    "PATH": f"{self.fakebin}{os.pathsep}{env.get('PATH', '')}"})
        return subprocess.run(["bash", "scripts/restore.sh", *flags, str(archive)],
                              cwd=self.app, env=env, capture_output=True, text=True,
                              timeout=120)

    def make_tar(self, name, members):
        import tarfile
        path = self.app / name
        with tarfile.open(path, "w:gz") as tar:
            for arcname, data in members.items():
                info = tarfile.TarInfo(arcname)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return path

    def good_db_bytes(self):
        src = self.app / "snap.db"
        orch_db.backup(self.state / "task_status.json", src)
        return src.read_bytes()

    def bad_archives(self):
        garbage = self.app / "garbage.tar.gz"
        garbage.write_bytes(b"this is not a gzip file at all")
        good = self.make_tar("good.tar.gz", {"./state/.snapshot/orch.db": self.good_db_bytes(),
                                             "./uploads/a.txt": b"x"})
        truncated = self.app / "truncated.tar.gz"
        truncated.write_bytes(good.read_bytes()[:-40])
        return {
            "garbage": garbage,
            "truncated": truncated,
            "no snapshot": self.make_tar("nosnap.tar.gz", {"./uploads/a.txt": b"x"}),
            "unsafe path": self.make_tar("unsafe.tar.gz", {
                "./state/.snapshot/orch.db": self.good_db_bytes(),
                "./state/../../escape.txt": b"x"}),
            "foreign dir": self.make_tar("foreign.tar.gz", {
                "./state/.snapshot/orch.db": self.good_db_bytes(), "./etc/passwd": b"x"}),
            "not sqlite": self.make_tar("notsqlite.tar.gz", {
                "./state/.snapshot/orch.db": b"definitely not a database" * 40}),
            "corrupt sqlite": self.make_tar("corrupt.tar.gz", {
                "./state/.snapshot/orch.db": b"SQLite format 3\x00" + b"\xff" * 4000}),
        }

    def assert_untouched(self, result, label):
        self.assertNotEqual(result.returncode, 0, label)
        self.assertIn("RESTORE ABORTED", result.stderr, label)
        self.assertIn("no safety backup was taken", result.stderr, label)
        self.assertNotIn("Taking a safety backup", result.stdout, label)
        self.assertFalse(self.docker_log.exists(),
                         f"{label}: docker was called: "
                         f"{self.docker_log.read_text() if self.docker_log.exists() else ''}")
        self.assertFalse((self.app / "backups").exists(), label)
        self.assertEqual(orch_db.load(self.state / "task_status.json"),
                         {"task_a": {"status": "done"}}, label)
        self.assertEqual((self.app / "uploads" / "a.txt").read_text(), "upload", label)

    def test_bad_archive_docker_mode_never_stops_service_or_backs_up(self):
        for label, archive in self.bad_archives().items():
            with self.subTest(label):
                self.assert_untouched(self.run_restore(archive), label)

    def test_bad_archive_local_mode_makes_no_safety_backup(self):
        for label, archive in self.bad_archives().items():
            with self.subTest(label):
                self.assert_untouched(self.run_restore(archive, "--local"), label)

    def test_good_archive_passes_validation_then_backs_up_then_stops(self):
        archive = self.make_tar("ok.tar.gz", {"./state/.snapshot/orch.db": self.good_db_bytes(),
                                              "./uploads/a.txt": b"restored"})
        result = self.run_restore(archive)      # fake docker: the backup step fails
        self.assertIn("Validating", result.stdout)
        self.assertIn("Taking a safety backup", result.stdout)
        self.assertNotIn("RESTORE ABORTED", result.stderr)
        calls = self.docker_log.read_text().splitlines()
        self.assertTrue(calls[0].startswith("compose run"), calls)   # the safety backup
        result = self.run_restore(archive, "--local")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.app / "uploads" / "a.txt").read_text(), "restored")
        self.assertEqual(len(list((self.app / "backups" / "pre-restore").iterdir())), 1)

    def test_script_order(self):
        text = (ROOT / "scripts" / "restore.sh").read_text(encoding="utf-8")
        body = text[text.index('cd "$ROOT"\necho "Validating'):]
        order = [body.index("validate_archive"), body.index("Taking a safety backup"),
                 body.index('scripts/backup.sh"'), body.index("docker compose stop orch")]
        self.assertEqual(order, sorted(order))
        for check in ("gzip -t", "tar -tzf", './state/.snapshot/orch.db',
                      "SQLite format 3", "PRAGMA integrity_check"):
            self.assertIn(check, text)


if __name__ == "__main__":
    unittest.main()
