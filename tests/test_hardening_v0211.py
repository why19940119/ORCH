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

import deploy_config
import orch_auth
import orch_db
import orch_ui
from auth_testing import use_temp_auth
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


PASSWORD = "Sup3r-secret-pass"


class SetupTokenByDefaultTests(unittest.TestCase):
    """Item 5: /setup requires a token by default (generated + logged when
    ORCH_SETUP_TOKEN is unset); ORCH_SETUP_LOCAL_NO_TOKEN=1 is the only way
    to open it, and only when set."""

    def setUp(self):
        orch_ui.app.config["TESTING"] = True
        self.client = orch_ui.app.test_client()
        use_temp_auth(self, users=())
        p = patch.object(deploy_config, "_generated_setup_token", None)
        p.start()
        self.addCleanup(p.stop)

    def env(self, **values):
        base = {"ORCH_SETUP_TOKEN": "", "ORCH_TRUSTED_HOSTS": "", "ORCH_PROXY_FIX": "",
                "ORCH_SETUP_LOCAL_NO_TOKEN": ""}
        base.update(values)
        return patch.dict(os.environ, base)

    def post(self, **fields):
        html = self.client.get("/setup").get_data(as_text=True)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        data = {"csrf_token": csrf, "username": "First Admin", "password": PASSWORD,
                "password_confirm": PASSWORD}
        data.update(fields)
        return self.client.post("/setup", data=data)

    def startup_log(self):
        err = io.StringIO()
        with redirect_stderr(err):
            orch_ui.setup_warnings()
        return err.getvalue()

    def test_default_localhost_install_requires_a_token(self):
        with self.env():                          # nothing exposed, nothing set
            self.assertFalse(deploy_config.exposed_install())
            log = self.startup_log()
            token = re.search(r"one-time token: (\S+)", log).group(1)
            self.assertGreaterEqual(len(token), 20)
            self.assertIn('name="setup_token"', self.client.get("/setup").get_data(as_text=True))
            self.assertEqual(self.post().status_code, 403)                  # no token
            self.assertEqual(self.post(setup_token="guess").status_code, 403)
            self.assertFalse(orch_auth.has_users())
            # the logged token works
            self.assertEqual(self.post(setup_token=token).status_code, 302)
        self.assertTrue(orch_auth.has_users())

    def test_token_is_printed_once_and_stable(self):
        with self.env():
            first = self.startup_log()
            second = self.startup_log()
            self.assertEqual(first.count("one-time token:"), 1)
            self.assertNotIn("one-time token:", second)           # printed once per process
            token = re.search(r"one-time token: (\S+)", first).group(1)
            self.assertEqual(deploy_config.effective_setup_token(), (token, "generated"))

    def test_opt_out_works_only_when_set(self):
        for value in ("", "0", "no", "false", "off", "2"):
            with self.subTest(value=value), self.env(ORCH_SETUP_LOCAL_NO_TOKEN=value):
                self.assertEqual(deploy_config.effective_setup_token()[1], "generated")
                self.assertEqual(self.post().status_code, 403)
        for value in ("1", "true", "YES", "on"):
            with self.subTest(value=value), self.env(ORCH_SETUP_LOCAL_NO_TOKEN=value):
                self.assertEqual(deploy_config.effective_setup_token(), ("", "opt-out"))
        with self.env(ORCH_SETUP_LOCAL_NO_TOKEN="1"):
            log = self.startup_log()
            self.assertIn("ORCH WARNING: ORCH_SETUP_LOCAL_NO_TOKEN=1", log)
            self.assertNotIn("one-time token", log)
            self.assertNotIn('name="setup_token"', self.client.get("/setup").get_data(as_text=True))
            self.assertEqual(self.post().status_code, 302)
        self.assertTrue(orch_auth.has_users())

    def test_opt_out_ignored_on_exposed_install(self):
        for exposed in ({"ORCH_TRUSTED_HOSTS": "orch.example.com"}, {"ORCH_PROXY_FIX": "1"}):
            with self.subTest(exposed), self.env(ORCH_SETUP_LOCAL_NO_TOKEN="1", **exposed):
                deploy_config._generated_setup_token = None
                log = self.startup_log()
                self.assertIn("ORCH_SETUP_LOCAL_NO_TOKEN is IGNORED", log)
                self.assertIn("one-time token:", log)
                self.assertEqual(self.post().status_code, 403)

    def test_configured_token_wins_over_opt_out(self):
        with self.env(ORCH_SETUP_TOKEN="tok-abcdef-123", ORCH_SETUP_LOCAL_NO_TOKEN="1"):
            self.assertEqual(deploy_config.effective_setup_token(), ("tok-abcdef-123", "env"))
            self.assertEqual(self.post().status_code, 403)
            self.assertEqual(self.post(setup_token="tok-abcdef-123").status_code, 302)
        self.assertIsNone(deploy_config._generated_setup_token)

    def test_docs_describe_default_token_and_opt_out(self):
        guide = (ROOT / "docs" / "安裝指南.md").read_text(encoding="utf-8")
        row = next(l for l in guide.splitlines() if l.startswith("| `ORCH_SETUP_TOKEN`"))
        self.assertIn("一律必填", row)
        self.assertIn("one-time token", row)
        self.assertIn("| `ORCH_SETUP_LOCAL_NO_TOKEN` |", guide)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("ORCH_SETUP_LOCAL_NO_TOKEN=1", readme)
        self.assertIn("a setup token is ALWAYS required", readme)
        self.assertIn("ORCH_SETUP_LOCAL_NO_TOKEN=1", (ROOT / ".env.example").read_text())


class SmokeEnvIsolationTests(unittest.TestCase):
    """Item 1: smoke.sh never loads the deployment's real .env (nor inherits
    deployment settings / API keys from the shell). The script is run in
    Docker mode against a fake ``docker`` that records its arguments, its
    environment and the env file it was given; a sentinel .env next to the
    compose file must never reach it."""

    SENTINEL = "SENTINEL-must-not-leak-7f3a"

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_smoke_v0211_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "app"
        (self.root / "scripts").mkdir(parents=True)
        shutil.copy2(ROOT / "scripts" / "smoke.sh", self.root / "scripts" / "smoke.sh")
        shutil.copy2(ROOT / "docker-compose.yml", self.root / "docker-compose.yml")
        # a stand-in for a real deployment's .env (written by this test only)
        (self.root / ".env").write_text(
            f"OPENROUTER_API_KEY=sk-or-v1-{self.SENTINEL}\n"
            f"ORCH_TRUSTED_HOSTS={self.SENTINEL}.example\nORCH_PROXY_FIX=1\n"
            f"ORCH_SETUP_TOKEN={self.SENTINEL}\n", encoding="utf-8")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "docker.log"
        self.copy = self.tmp / "envfile.copy"
        fake = self.bin / "docker"
        fake.write_text(f"""#!/bin/sh
echo "ARGS $*" >> '{self.log}'
env | grep -E '^(OPENROUTER_|ORCH_|COMPOSE_)' | sed 's/^/ENV /' >> '{self.log}'
prev=""
for a in "$@"; do
  if [ "$prev" = "--env-file" ]; then cp "$a" '{self.copy}'; fi
  prev="$a"
done
case " $* " in *" up "*) exit 1;; esac
exit 0
""")
        os.chmod(fake, 0o755)
        (self.tmp / "work").mkdir()

    def run_smoke(self):
        env = dict(os.environ)
        env.update({"PATH": f"{self.bin}{os.pathsep}{env.get('PATH', '')}",
                    "TMPDIR": str(self.tmp / "work"), "SMOKE_PORT": "5999",
                    # deployment settings exported in the caller's shell
                    "OPENROUTER_API_KEY": f"sk-shell-{self.SENTINEL}",
                    "ORCH_TRUSTED_HOSTS": f"shell-{self.SENTINEL}",
                    "ORCH_PROXY_FIX": "1", "ORCH_SETUP_TOKEN": f"shell-{self.SENTINEL}",
                    "ORCH_SETUP_LOCAL_NO_TOKEN": "1"})
        return subprocess.run(["bash", str(self.root / "scripts" / "smoke.sh")],
                              cwd=self.root, env=env, capture_output=True, text=True,
                              timeout=60)

    def test_docker_mode_uses_generated_env_file_only(self):
        result = self.run_smoke()
        self.assertNotEqual(result.returncode, 0)          # fake `up` fails on purpose
        log = self.log.read_text()
        calls = [l[5:] for l in log.splitlines() if l.startswith("ARGS ")]
        self.assertTrue(calls[0].startswith("compose -p orch-smoke-"), calls)
        self.assertIn(" up -d --build", calls[0])
        self.assertTrue(any(c.startswith("compose -p orch-smoke-") and " down -v" in c
                            for c in calls))                 # teardown still runs
        env_file = re.search(r"--env-file (\S+)", calls[0]).group(1)
        self.assertTrue(env_file.endswith("/smoke.env"))
        self.assertNotEqual(Path(env_file).resolve(), (self.root / ".env").resolve())
        self.assertIn(f"ENV ORCH_ENV_FILE={env_file}", log)   # compose env_file target
        self.assertIn("ENV ORCH_IMAGE=orch-smoke:", log)
        self.assertNotIn(self.SENTINEL, log)                   # nothing from .env / shell
        self.assertNotIn("ENV OPENROUTER_API_KEY", log)
        self.assertNotIn("ENV ORCH_TRUSTED_HOSTS", log)
        generated = self.copy.read_text()
        self.assertNotIn(self.SENTINEL, generated)
        for line in ("OPENROUTER_API_KEY=", "OPENROUTER_VISION_MODEL=", "ORCH_TRUSTED_HOSTS=",
                     "ORCH_TRUSTED_PROXY=", "ORCH_PROXY_FIX=0", "ORCH_SETUP_LOCAL_NO_TOKEN=",
                     "ORCH_DEMO_FORCE_MOCK=1", "ORCH_UI_SECRET_KEY="):
            self.assertIn(line, generated.splitlines())
        self.assertRegex(generated, r"(?m)^ORCH_SETUP_TOKEN=smoke-[0-9a-f]{32}$")
        self.assertFalse(Path(env_file).exists())              # temp dir cleaned up
        self.assertNotIn(self.SENTINEL, result.stdout + result.stderr)

    def test_static_isolation(self):
        text = (ROOT / "scripts" / "smoke.sh").read_text(encoding="utf-8")
        self.assertNotRegex(text, r'ROOT[^\n]*/\.env\b')   # never references the real .env
        self.assertIn('docker compose -p "$PROJECT" --env-file "$SMOKE_ENV"', text)
        self.assertIn('ORCH_ENV_FILE="$SMOKE_ENV"', text)
        local = text[text.index('git -C "$ROOT" archive HEAD'):text.index("serve.py >")]
        self.assertIn('env "${UNSET_ARGS[@]}" OPENROUTER_API_KEY=', local)
        self.assertIn('ORCH_SETUP_TOKEN="$SMOKE_SETUP_TOKEN"', local)
        for var in ("OPENROUTER_API_KEY", "OPENROUTER_VISION_MODEL", "ORCH_TRUSTED_HOSTS",
                    "ORCH_PROXY_FIX", "ORCH_SETUP_TOKEN", "ORCH_SETUP_LOCAL_NO_TOKEN",
                    "ORCH_UI_SECRET_KEY", "ORCH_AUTH_DIR"):
            self.assertIn(var, text[text.index("ISOLATE_VARS="):text.index("UNSET_ARGS=()")])
        self.assertIn('--data-urlencode "setup_token=$SMOKE_SETUP_TOKEN"', text)
        self.assertIn("setup POST without token must be 403", text)
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("- path: ${ORCH_ENV_FILE:-.env}", compose)


if __name__ == "__main__":
    unittest.main()
