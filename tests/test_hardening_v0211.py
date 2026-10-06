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
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import deploy_config
import orch_auth
import orch_chat
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


class RestoreFixture(unittest.TestCase):
    """Temp app (scripts + orch_db.py + live data) and a fake ``docker`` on
    PATH that logs every call."""

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

    def run_restore(self, archive, *flags, python=sys.executable):
        env = {k: v for k, v in os.environ.items() if not k.startswith("ORCH_")}
        env.update({"PYTHON": python,
                    "PATH": f"{self.fakebin}{os.pathsep}{env.get('PATH', '')}"})
        return subprocess.run(["bash", "scripts/restore.sh", *flags, str(archive)],
                              cwd=self.app, env=env, capture_output=True, text=True,
                              timeout=120)

    def make_tar(self, name, members):
        import tarfile
        path = self.app / name
        with tarfile.open(path, "w:gz") as tar:
            for top in ("./state", "./uploads", "./data", "./artifacts"):   # complete archive
                info = tarfile.TarInfo(top)
                info.type = tarfile.DIRTYPE
                info.mode = 0o700
                tar.addfile(info)
            for arcname, data in members.items():
                info = tarfile.TarInfo(arcname)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return path

    def good_db_bytes(self):
        src = self.app / "snap.db"
        orch_db.backup(self.state / "task_status.json", src)
        return src.read_bytes()



class RestoreValidatesFirstTests(RestoreFixture):
    """Item 4: restore.sh validates the archive before it stops the service
    or takes the safety backup. Docker is stubbed, so "service untouched"
    means "docker never called"."""

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
        self.assertIn("設定權杖必填", row)
        self.assertIn("兩種方式擇一", row)
        self.assertIn("one-time token", row)
        self.assertIn("| `ORCH_SETUP_LOCAL_NO_TOKEN` |", guide)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("ORCH_SETUP_LOCAL_NO_TOKEN=1", readme)
        self.assertIn("/setup always requires a setup token - either set", readme)
        self.assertIn("use the one-time token printed in", readme)
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


class VisionModelTests(unittest.TestCase):
    """Image chat: the retired default is gone, the env override wins, the
    chat model is used for images when it is known to accept them. No
    network: nothing here reaches OpenRouter."""

    def env(self, **values):
        base = {"OPENROUTER_API_KEY": "sk-or-v1-test-key-0123456789abcdef",
                "OPENROUTER_VISION_MODEL": "", "OPENROUTER_CHAT_MODEL": "",
                "OPENROUTER_MODEL": ""}
        base.update(values)
        return patch.dict(os.environ, base)

    def test_default_is_no_longer_the_retired_gemini_model(self):
        self.assertNotEqual(orch_chat.DEFAULT_VISION_MODEL, "google/gemini-2.0-flash-001")
        self.assertNotIn("gemini-2.0-flash-001", orch_chat.DEFAULT_VISION_MODEL)
        self.assertEqual(orch_chat.DEFAULT_VISION_MODEL, "mistralai/mistral-medium-3.1")
        self.assertTrue(orch_chat.model_accepts_images(orch_chat.DEFAULT_VISION_MODEL))

    def test_env_override_wins(self):
        with self.env(OPENROUTER_VISION_MODEL="  openai/gpt-4o-mini ",
                      OPENROUTER_MODEL="anthropic/claude-sonnet-4"):
            self.assertEqual(orch_chat.get_chat_config(use_vision=True)["model"],
                             "openai/gpt-4o-mini")
        with self.env(OPENROUTER_VISION_MODEL="some/text-only-model"):
            self.assertEqual(orch_chat.resolve_vision_model(), "some/text-only-model")

    def test_unset_or_empty_uses_default(self):
        with self.env():                                   # empty = unset
            self.assertEqual(orch_chat.get_chat_config(use_vision=True)["model"],
                             orch_chat.DEFAULT_VISION_MODEL)
        env = {k: v for k, v in os.environ.items()
               if k not in ("OPENROUTER_VISION_MODEL", "OPENROUTER_CHAT_MODEL",
                            "OPENROUTER_MODEL")}
        env["OPENROUTER_API_KEY"] = "sk-or-v1-test-key-0123456789abcdef"
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(orch_chat.resolve_vision_model(), orch_chat.DEFAULT_VISION_MODEL)

    def test_falls_back_to_image_capable_chat_model(self):
        with self.env(OPENROUTER_MODEL="openai/gpt-4o"):
            self.assertEqual(orch_chat.resolve_vision_model(), "openai/gpt-4o")
        with self.env(OPENROUTER_MODEL="mistralai/mistral-large",
                      OPENROUTER_CHAT_MODEL="anthropic/claude-sonnet-4.5"):
            self.assertEqual(orch_chat.resolve_vision_model(), "anthropic/claude-sonnet-4.5")
        # a chat model not known to take images -> the default vision model
        with self.env(OPENROUTER_MODEL="deepseek/deepseek-chat"):
            self.assertEqual(orch_chat.resolve_vision_model(), orch_chat.DEFAULT_VISION_MODEL)
            self.assertEqual(orch_chat.get_chat_config()["model"], "deepseek/deepseek-chat")

    def test_image_capable_list_is_exact(self):
        for model in ("mistralai/mistral-medium-3.1", "openai/gpt-4o", "openai/gpt-4o-mini",
                      "openai/gpt-4o-2024-11-20", "openai/gpt-4.1-mini", "openai/gpt-5",
                      "anthropic/claude-3-haiku", "anthropic/claude-3.5-sonnet",
                      "anthropic/claude-sonnet-4", "anthropic/claude-sonnet-4.5",
                      "anthropic/claude-opus-4.1", "google/gemini-2.5-flash",
                      "x-ai/grok-4", "meta-llama/llama-4-maverick", " OpenAI/GPT-4o "):
            with self.subTest(model):
                self.assertTrue(orch_chat.model_accepts_images(model))
        for model in ("openai/gpt-4o-audio-preview", "openai/gpt-4o-search-preview",
                      "openai/gpt-4o-mini-tts", "openai/gpt-4o:extended",
                      "anthropic/claude-3.5-haiku", "anthropic/claude-3-5-haiku",
                      "anthropic/claude-3.5-haiku-20241022", "anthropic/claude-sonnet-4-x",
                      "mistralai/mistral-medium-3.1-experimental", "openai/gpt-5-chat-audio",
                      "google/gemini-2.5-flash-image-preview-tts", "deepseek/deepseek-chat",
                      "", "x-ai/grok-4-fake"):
            with self.subTest(model):
                self.assertFalse(orch_chat.model_accepts_images(model))
        with self.env(OPENROUTER_MODEL="openai/gpt-4o-audio-preview"):
            self.assertEqual(orch_chat.resolve_vision_model(), orch_chat.DEFAULT_VISION_MODEL)
        with self.env(OPENROUTER_MODEL="anthropic/claude-3.5-haiku"):
            self.assertEqual(orch_chat.resolve_vision_model(), orch_chat.DEFAULT_VISION_MODEL)

    def test_image_request_uses_resolved_model_and_json_object(self):
        captured = {}

        class FakeResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            captured.update(json.loads(request.data.decode("utf-8")))
            reply = {"answer": "紅色", "referenced_task_ids": [], "referenced_artifact_ids": [],
                     "limitations": [], "execution_authority": "none"}
            return FakeResponse(json.dumps({"id": "x", "model": captured["model"], "choices": [
                {"message": {"content": json.dumps(reply, ensure_ascii=False)}}]}).encode())

        with self.env(), patch.object(orch_chat.urllib.request, "urlopen", fake_urlopen):
            result = orch_chat.ask_orch("What colour?", "general", {}, [], attachments=[
                {"name": "red.png", "kind": "image", "mime": "image/png",
                 "data_base64": "iVBORw0KGgo="}])
        self.assertEqual(captured["model"], "mistralai/mistral-medium-3.1")
        self.assertEqual(captured["response_format"], {"type": "json_object"})
        self.assertTrue(result["used_vision"])
        self.assertEqual(result["chat"]["answer"], "紅色")

    def test_env_example_and_docs(self):
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("OPENROUTER_VISION_MODEL=", example)
        self.assertNotIn("gemini-2.0-flash-001\n", example.split("OPENROUTER_VISION_MODEL=")[1][:80])
        self.assertIn("No endpoints found", example)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("OPENROUTER_VISION_MODEL", readme)
        self.assertIn("No endpoints found", readme)
        guide = (ROOT / "docs" / "安裝指南.md").read_text(encoding="utf-8")
        row = next(l for l in guide.splitlines() if l.startswith("| `OPENROUTER_VISION_MODEL`"))
        self.assertIn("mistralai/mistral-medium-3.1", row)
        self.assertIn("No endpoints found", row)


class ProviderFailureLoggingTests(unittest.TestCase):
    """Provider failures are logged server-side with code, HTTP status,
    model and request kind - never the key, Authorization header, prompt,
    image data or raw response body."""

    KEY = "sk-or-v1-SECRETKEY0123456789abcdefSECRET"
    PROMPT = "PROMPT-TEXT-what-is-in-my-private-photo"
    IMAGE = "iVBORw0KGgoIMAGEDATA" + "A" * 64

    def failing_urlopen(self, status, body):
        from email.message import Message

        def fake(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, status, "error", Message(),
                                         io.BytesIO(body))
        return fake

    def ask(self, urlopen, image=True):
        env = {"OPENROUTER_API_KEY": self.KEY, "OPENROUTER_VISION_MODEL": "",
               "OPENROUTER_CHAT_MODEL": "", "OPENROUTER_MODEL": ""}
        attachments = ([{"name": "p.png", "kind": "image", "mime": "image/png",
                         "data_base64": self.IMAGE}] if image else None)
        with patch.dict(os.environ, env), \
                patch.object(orch_chat.urllib.request, "urlopen", urlopen), \
                self.assertLogs("orch.chat", "WARNING") as logged, \
                self.assertRaises(orch_chat.ChatProviderError) as ctx:
            orch_chat.ask_orch(self.PROMPT, "general", {}, [], attachments=attachments)
        return ctx.exception, logged

    def assert_clean(self, logged):
        text = "\n".join(logged.output)
        for secret in (self.KEY, "SECRETKEY", self.PROMPT, self.IMAGE[:24], "Authorization",
                       "Bearer"):
            self.assertNotIn(secret, text)

    def test_mocked_404_logs_status_model_kind_without_secrets(self):
        body = json.dumps({"error": {"code": 404, "message":
                                     "No endpoints found for google/gemini-2.0-flash-001."},
                           "echo": {"prompt": self.PROMPT, "auth": f"Bearer {self.KEY}"}}).encode()
        exc, logged = self.ask(self.failing_urlopen(404, body))
        self.assertEqual((exc.code, exc.params.get("status")), ("http", 404))
        self.assertEqual(len(logged.records), 1)
        record = logged.records[0]
        self.assertEqual(record.levelname, "WARNING")
        message = record.getMessage()
        self.assertIn("status=404", message)
        self.assertIn("model=mistralai/mistral-medium-3.1", message)
        self.assertIn("kind=vision", message)
        self.assertIn("code=http", message)
        self.assertIn("No endpoints found for google/gemini-2.0-flash-001.", message)
        self.assertIn("retired", message)                    # actionable hint
        self.assertIn("OPENROUTER_VISION_MODEL", message)
        self.assert_clean(logged)

    def test_key_echoed_in_provider_message_is_redacted_and_short(self):
        body = json.dumps({"error": {"message": f"bad key {self.KEY} for {self.PROMPT} "
                                                + "x" * 500}}).encode()
        exc, logged = self.ask(self.failing_urlopen(401, body), image=False)
        message = logged.records[0].getMessage()
        self.assertIn("status=401", message)
        self.assertIn("kind=chat", message)
        self.assertIn("[redacted]", message)
        self.assertNotIn(self.KEY, message)
        self.assertLess(len(message), 400)
        self.assertNotIn(self.IMAGE[:24], message)

    def test_non_json_body_is_not_logged(self):
        exc, logged = self.ask(self.failing_urlopen(502, f"<html>{self.PROMPT}</html>".encode()))
        message = logged.records[0].getMessage()
        self.assertIn("status=502", message)
        self.assertIn("error=''", message)
        self.assert_clean(logged)

    def test_connection_failure_logged(self):
        def fake(request, timeout=None):
            raise urllib.error.URLError(TimeoutError("timed out"))
        exc, logged = self.ask(fake)
        self.assertEqual(exc.code, "connection")
        message = logged.records[0].getMessage()
        self.assertIn("code=connection", message)
        self.assertIn("status=-", message)
        self.assertIn("TimeoutError", message)
        self.assert_clean(logged)

    def broken_body_urlopen(self, body=None, incomplete=False):
        test = self

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                if incomplete:
                    import http.client
                    raise http.client.IncompleteRead(
                        f"partial {test.PROMPT} {test.KEY}".encode(), 4096)
                return body

        return lambda request, timeout=None: Response()

    def test_incomplete_read_is_logged_redacted(self):
        exc, logged = self.ask(self.broken_body_urlopen(incomplete=True))
        self.assertEqual(exc.code, "bad_response")
        message = logged.records[0].getMessage()
        self.assertIn("code=bad_response", message)
        self.assertIn("status=200", message)
        self.assertIn("kind=vision", message)
        self.assertIn("model=mistralai/mistral-medium-3.1", message)
        self.assertIn("IncompleteRead", message)
        self.assert_clean(logged)

    def test_non_utf8_body_is_logged_redacted(self):
        body = b"\xff\xfe" + self.PROMPT.encode() + b"\xc3\x28" + self.KEY.encode()
        exc, logged = self.ask(self.broken_body_urlopen(body=body), image=False)
        self.assertEqual(exc.code, "bad_response")
        message = logged.records[0].getMessage()
        self.assertIn("code=bad_response", message)
        self.assertIn("status=200", message)
        self.assertIn("kind=chat", message)
        self.assertIn("not valid UTF-8", message)
        self.assert_clean(logged)

    def test_serve_configures_orch_logger(self):
        import logging
        log = logging.getLogger("orch")
        saved = (list(log.handlers), log.level, log.propagate)
        try:
            log.handlers.clear()
            deploy_config.configure_app_logging()
            self.assertEqual(len(log.handlers), 1)
            deploy_config.configure_app_logging()             # idempotent
            self.assertEqual(len(log.handlers), 1)
        finally:
            log.handlers[:] = saved[0]
            log.setLevel(saved[1])
            log.propagate = saved[2]
        self.assertIn("deploy_config.configure_app_logging()",
                      (ROOT / "serve.py").read_text(encoding="utf-8"))


class RestoreLinkAndSwapTests(RestoreFixture):
    """Review blocker: link members are refused (host validation AND the
    in-container extractor), and the restore never deletes live data before
    a successful extraction: it stages, verifies, swaps and rolls back."""

    def setUp(self):
        super().setUp()
        (self.state / "secret_key").write_text("live-secret-" + "k" * 30)
        os.chmod(self.state / "secret_key", 0o600)
        (self.app / "uploads" / "nested").mkdir()
        (self.app / "uploads" / "nested" / "b.bin").write_bytes(bytes(range(256)))
        self.outside = self.tmp_outside = Path(self.app.parent / (self.app.name + "-outside"))
        self.outside.mkdir()
        import shutil
        self.addCleanup(shutil.rmtree, self.outside, True)
        self.victim = self.outside / "victim"
        self.victim.write_text("victim content")
        os.chmod(self.victim, 0o644)

    # -- helpers -----------------------------------------------------------
    def live_snapshot(self):
        import hashlib
        snap = {}
        for top in ("state", "uploads", "data", "artifacts"):
            base = self.app / top
            if not base.exists():
                continue
            for path in sorted(base.rglob("*")):
                st = path.lstat()
                digest = (hashlib.sha256(path.read_bytes()).hexdigest()
                          if stat.S_ISREG(st.st_mode) else None)
                snap[str(path.relative_to(self.app))] = (stat.S_IFMT(st.st_mode),
                                                         stat.S_IMODE(st.st_mode), digest)
        return snap

    def victim_state(self):
        return (stat.S_IMODE(self.victim.stat().st_mode), self.victim.read_text(),
                sorted(p.name for p in self.outside.iterdir()))

    def build(self, name, members):
        """members: list of (TarInfo kwargs, data-or-None)."""
        import tarfile
        path = self.app / name
        with tarfile.open(path, "w:gz", format=tarfile.GNU_FORMAT) as tar:
            for fields, data in members:
                info = tarfile.TarInfo(fields.pop("name"))
                for key, value in fields.items():
                    setattr(info, key, value)
                if data is not None:
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
                else:
                    tar.addfile(info)
        return path

    def regular(self, name, data):
        import tarfile
        return ({"name": name, "type": tarfile.REGTYPE, "mode": 0o600}, data)

    def folder(self, name):
        import tarfile
        return ({"name": name, "type": tarfile.DIRTYPE, "mode": 0o700}, None)

    def base_members(self, uploads_text=b"restored upload"):
        return [self.folder("./state"), self.folder("./state/.snapshot"),
                self.regular("./state/.snapshot/orch.db", self.good_db_bytes()),
                self.regular("./state/secret_key", b"restored-secret-" + b"r" * 30),
                self.folder("./data"), self.folder("./artifacts"),
                self.folder("./uploads"), self.regular("./uploads/a.txt", uploads_text)]

    def link_archives(self):
        import tarfile
        rel_escape = os.path.relpath(self.victim, self.app / "uploads")
        return {
            "symlink to outside file": self.build("l1.tar.gz", [
                m for m in self.base_members() if m[0]["name"] != "./state/secret_key"] + [
                ({"name": "./state/secret_key", "type": tarfile.SYMTYPE,
                  "linkname": str(self.victim)}, None)]),
            "symlinked directory": self.build("l2.tar.gz", self.base_members()[:6] + [
                ({"name": "./uploads", "type": tarfile.SYMTYPE,
                  "linkname": str(self.outside)}, None),
                self.regular("./uploads/victim", b"overwritten through the dir link")]),
            "hardlink to absolute path": self.build("l3.tar.gz", self.base_members() + [
                ({"name": "./state/secret_key2", "type": tarfile.LNKTYPE,
                  "linkname": str(self.victim)}, None)]),
            "hardlink to relative path outside": self.build("l4.tar.gz", self.base_members() + [
                ({"name": "./uploads/h", "type": tarfile.LNKTYPE,
                  "linkname": rel_escape}, None)]),
            "fifo member": self.build("l5.tar.gz", self.base_members() + [
                ({"name": "./uploads/pipe", "type": tarfile.FIFOTYPE}, None)]),
        }

    def inner_code(self):
        text = (ROOT / "scripts" / "restore.sh").read_text(encoding="utf-8")
        return re.search(r"<<'PYINNER'\n(.*?)\nPYINNER\n", text, re.S).group(1)

    def run_inner(self, archive, prelude="", sha=None):
        """The extractor alone (as `docker compose run ... python -c "$INNER"`
        runs it), bypassing the host-side validation. ``sha`` defaults to the
        archive's real sha256 (what restore.sh passes in)."""
        import hashlib
        code = prelude + "\nexec(compile(INNER, 'inner', 'exec'))\n"
        env = {k: v for k, v in os.environ.items() if not k.startswith("ORCH_")}
        if sha is None:
            sha = hashlib.sha256(Path(archive).read_bytes()).hexdigest()
        env.update({"DIRS": "state uploads data artifacts", "INNER": self.inner_code(),
                    "EXPECTED_SHA256": sha})
        code = "import os\nINNER = os.environ['INNER']\n" + code
        with open(archive, "rb") as stdin:
            return subprocess.run([sys.executable, "-c", code], cwd=self.app, env=env,
                                  stdin=stdin, capture_output=True, text=True, timeout=120)

    def assert_no_leftovers(self):
        for top in ("state", "uploads", "data", "artifacts"):
            base = self.app / top
            if base.exists():
                self.assertEqual([p.name for p in base.iterdir()
                                  if p.name.startswith(".restore-")], [], top)

    # -- full script, both modes ------------------------------------------
    def test_link_archives_are_refused_before_anything_happens(self):
        for label, archive in self.link_archives().items():
            for flags in ((), ("--local",)):
                with self.subTest(label=label, mode=flags or "docker"):
                    before, victim = self.live_snapshot(), self.victim_state()
                    result = self.run_restore(archive, *flags)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("RESTORE ABORTED", result.stderr)
                    self.assertFalse(self.docker_log.exists(), "docker was called")
                    self.assertFalse((self.app / "backups").exists())
                    self.assertEqual(self.live_snapshot(), before)      # byte-for-byte
                    self.assertEqual(self.victim_state(), victim)       # mode + content

    def test_host_listing_check_works_without_python(self):
        # Docker hosts may have no python3: the `tar -tv` type column check
        # alone must refuse every link archive.
        for label, archive in self.link_archives().items():
            with self.subTest(label):
                before = self.live_snapshot()
                result = self.run_restore(archive, python="/nonexistent/python3")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("link or special file", result.stderr)
                self.assertFalse(self.docker_log.exists())
                self.assertEqual(self.live_snapshot(), before)

    # -- the extractor alone (defence in depth) ----------------------------
    def test_extractor_refuses_links_and_keeps_live_data(self):
        for label, archive in self.link_archives().items():
            with self.subTest(label):
                before, victim = self.live_snapshot(), self.victim_state()
                result = self.run_inner(archive)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("RESTORE ABORTED", result.stderr)          # refused pre-swap
                self.assertIn("left unchanged", result.stderr)
                self.assertEqual(self.live_snapshot(), before)
                self.assertEqual(self.victim_state(), victim)
                self.assert_no_leftovers()

    def test_mid_extraction_failure_leaves_live_data_intact(self):
        good = self.build("good.tar.gz", self.base_members() + [
            self.regular("./uploads/big.bin", os.urandom(200000))])
        truncated = self.app / "truncated.tar.gz"
        truncated.write_bytes(good.read_bytes()[: len(good.read_bytes()) // 2])
        bad_db = self.build("baddb.tar.gz", [
            self.folder("./state"), self.folder("./state/.snapshot"),
            self.regular("./state/.snapshot/orch.db", b"SQLite format 3\x00" + b"\xff" * 4000),
            self.folder("./uploads"), self.regular("./uploads/a.txt", b"x")])
        for label, archive in (("truncated stream", truncated), ("corrupt staged DB", bad_db)):
            with self.subTest(label):
                before = self.live_snapshot()
                result = self.run_inner(archive)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(self.live_snapshot(), before)
                self.assert_no_leftovers()

    def test_failure_during_swap_rolls_back(self):
        archive = self.build("ok.tar.gz", self.base_members())
        # 8 renames on success: staged DB, 2 + 2 in state/, 2 + 1 in uploads/
        for fail_at in range(1, 9):
            with self.subTest(fail_at=fail_at):
                before = self.live_snapshot()
                prelude = ("real_rename = os.rename\ncalls = [0]\n"
                           "def rename(a, b):\n"
                           "    calls[0] += 1\n"
                           f"    if calls[0] == {fail_at}:\n"
                           "        raise OSError('injected rename failure')\n"
                           "    return real_rename(a, b)\n"
                           "os.rename = rename\n")
                result = self.run_inner(archive, prelude)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("injected rename failure", result.stderr)
                self.assertEqual(self.live_snapshot(), before)
                self.assert_no_leftovers()

    def test_failed_db_check_after_swap_rolls_back(self):
        archive = self.build("ok.tar.gz", self.base_members())
        before = self.live_snapshot()
        trace = ("Traceback (most recent call last):\\n  File orch_db.py, line 1\\n"
                 "sqlite3.DatabaseError: file is not a database\\n")
        prelude = ("import subprocess, types\n"
                   "subprocess.run = lambda *a, **k: types.SimpleNamespace("
                   f"returncode=1, stdout='', stderr=\"{trace}\")\n")
        result = self.run_inner(archive, prelude)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("RESTORE FAILED", result.stderr)                     # post-swap
        self.assertIn("orch_db.py check failed on the restored database "
                      "(sqlite3.DatabaseError: file is not a database)", result.stderr)
        self.assertNotIn("Traceback", result.stderr)                       # one clean line
        self.assertEqual(len(result.stderr.strip().splitlines()), 2, result.stderr)
        self.assertEqual(self.live_snapshot(), before)
        self.assert_no_leftovers()

    def test_successful_swap_replaces_data_and_cleans_up(self):
        archive = self.build("ok.tar.gz", self.base_members(b"from the archive"))
        result = self.run_inner(archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.app / "uploads" / "a.txt").read_bytes(), b"from the archive")
        self.assertFalse((self.app / "uploads" / "nested").exists())     # old content gone
        self.assertTrue((self.state / "secret_key").read_text().startswith("restored-secret-"))
        self.assertEqual(orch_db.load(self.state / "task_status.json"),
                         {"task_a": {"status": "done"}})
        for name in ("orch.db", "secret_key"):
            self.assertEqual(stat.S_IMODE((self.state / name).stat().st_mode), 0o600)
        self.assertFalse((self.state / ".snapshot").exists())
        self.assert_no_leftovers()

    # -- truncated / partial / altered archives (re-review round 2) ----------
    def real_backup(self):
        """A real scripts/backup.sh --local archive of the fixture app."""
        (self.state / "tasks.json").write_text('{"t": 1}')
        (self.app / "data").mkdir(exist_ok=True)
        (self.app / "data" / "orders.csv").write_text("id,total\n1,10\n")
        (self.app / "artifacts").mkdir(exist_ok=True)
        (self.app / "artifacts" / "draft.md").write_text("draft " * 300)
        env = {k: v for k, v in os.environ.items() if not k.startswith("ORCH_")}
        env.update({"PYTHON": sys.executable, "BACKUP_DIR": str(self.app / "bk")})
        result = subprocess.run(["bash", "scripts/backup.sh", "--local"], cwd=self.app, env=env,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        archive = Path(result.stdout.strip().splitlines()[-1])
        moved = self.app / "real-backup.tar.gz"
        moved.write_bytes(archive.read_bytes())
        import shutil
        shutil.rmtree(self.app / "bk")
        return moved

    def gzip_stream_end(self, data):
        import zlib
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        d.decompress(data)
        return len(data) - len(d.unused_data)

    def member_boundaries(self, raw_tar):
        import tarfile
        offsets = []
        with tarfile.open(fileobj=io.BytesIO(raw_tar), mode="r:") as tar:
            for member in tar:
                offsets.append(member.offset)
                end = member.offset_data + ((member.size + 511) // 512) * 512
        offsets.append(end)
        return offsets

    def test_backup_manifest_lists_every_member_in_archive_order(self):
        import tarfile
        (self.app / "uploads" / "._a.txt").write_bytes(b"macOS metadata")
        (self.app / "uploads" / ".restore-old-left").mkdir()
        (self.app / "uploads" / ".restore-old-left" / "x").write_text("leftover")
        archive = self.real_backup()
        with tarfile.open(archive) as tar:
            names = [m.name.rstrip("/") for m in tar]
            required = tar.extractfile("./state/.snapshot/required.txt").read().decode()
        lines = [l.rstrip("/") for l in required.splitlines() if l and not l.startswith("#")]
        self.assertEqual(lines, names)                    # every member, same order
        self.assertEqual(names[:4], ["./state", "./state/.snapshot",
                                     "./state/.snapshot/required.txt", "./state/.snapshot/orch.db"])
        for needed in ("./uploads", "./data", "./artifacts", "./state/.snapshot/status.json",
                       "./state/secret_key", "./state/tasks.json", "./uploads/a.txt",
                       "./uploads/nested/b.bin", "./data/orders.csv", "./artifacts/draft.md"):
            self.assertIn(needed, lines)
        for skipped in ("./state/orch.db", "./uploads/._a.txt", "./uploads/.restore-old-left",
                        "./uploads/.restore-old-left/x"):
            self.assertNotIn(skipped, names)
        text = (ROOT / "scripts" / "backup.sh").read_text(encoding="utf-8")
        self.assertIn("tar -czf - --no-recursion -T -", text)

    def test_backup_refuses_a_backslash_file_name(self):
        (self.app / "uploads" / "bad\\name.txt").write_text("x")
        env = {k: v for k, v in os.environ.items() if not k.startswith("ORCH_")}
        env.update({"PYTHON": sys.executable, "BACKUP_DIR": str(self.app / "bk")})
        result = subprocess.run(["bash", "scripts/backup.sh", "--local"], cwd=self.app, env=env,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("contains a backslash", result.stderr)
        self.assertEqual(list((self.app / "bk").iterdir()), [])   # no .partial / .list left
        self.assertFalse((self.state / ".snapshot").exists())

    def test_non_ascii_names_back_up_and_restore_in_the_c_locale(self):
        """tar -t escapes non-ASCII bytes as \\ooo in the C locale: the host
        backslash rule must not mistake that for a backslash in the name."""
        (self.app / "uploads" / "你好 报告.txt").write_text("zh upload", encoding="utf-8")
        archive = self.real_backup()
        (self.app / "uploads" / "你好 报告.txt").write_text("changed", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ORCH_", "LC_", "LANG"))}
        env.update({"PYTHON": sys.executable, "LC_ALL": "C"})
        result = subprocess.run(["bash", "scripts/restore.sh", "--local", str(archive)],
                                cwd=self.app, env=env, capture_output=True, text=True,
                                timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.app / "uploads" / "你好 报告.txt").read_text(encoding="utf-8"),
                         "zh upload")
        self.assert_no_leftovers()

    def test_every_compressed_cut_aborts(self):
        """Every byte offset inside the gzip stream, with the checksum of the
        cut bytes passed in (so the gzip CRC / end-of-archive / required
        member checks must catch it), in one process for speed."""
        import hashlib
        archive = self.real_backup()
        data = archive.read_bytes()
        gz_end = self.gzip_stream_end(data)
        before = self.live_snapshot()
        driver = (
            "import hashlib, io, json, os, sys\n"
            "INNER = os.environ['INNER']\n"
            "data = open(sys.argv[1], 'rb').read()\n"
            "results = {}\n"
            "real_stderr = sys.stderr\n"
            "for cut in range(1, int(sys.argv[2])):\n"
            "    chunk = data[:cut]\n"
            "    os.environ['EXPECTED_SHA256'] = hashlib.sha256(chunk).hexdigest()\n"
            "    sys.stdin = io.TextIOWrapper(io.BytesIO(chunk))\n"
            "    sys.stderr = io.StringIO()\n"
            "    try:\n"
            "        exec(compile(INNER, 'inner', 'exec'), {'__name__': 'inner'})\n"
            "        code = 0\n"
            "    except SystemExit as exc:\n"
            "        code = exc.code\n"
            "    results[cut] = [code, sys.stderr.getvalue().splitlines()[:1]]\n"
            "sys.stderr = real_stderr\n"
            "print(json.dumps(results))\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("ORCH_")}
        env.update({"DIRS": "state uploads data artifacts", "INNER": self.inner_code()})
        result = subprocess.run([sys.executable, "-c", driver, str(archive), str(gz_end)],
                                cwd=self.app, env=env, capture_output=True, text=True,
                                timeout=600)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        results = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(len(results), gz_end - 1)
        passed = {cut: r for cut, r in results.items() if r[0] != 1}
        self.assertEqual(passed, {}, "cuts that did not abort")
        self.assertTrue(all(r[1] and r[1][0].startswith("RESTORE ABORTED")
                            for r in results.values()))
        self.assertEqual(self.live_snapshot(), before)            # byte-identical
        self.assert_no_leftovers()
        # sanity: the complete archive restores with the same driver logic
        full = self.run_inner(archive)
        self.assertEqual(full.returncode, 0, full.stderr)

    def test_every_member_boundary_cut_aborts(self):
        """A tar cut at every member boundary, re-gzipped into a well-formed
        stream - with and without an end-of-archive marker appended - is
        refused by the host check (full script, stubbed docker) and by the
        extractor alone."""
        import gzip
        archive = self.real_backup()
        raw = gzip.decompress(archive.read_bytes())
        boundaries = self.member_boundaries(raw)
        self.assertGreater(len(boundaries), 10)
        before, victim_free = self.live_snapshot(), True
        checked = 0
        for i, cut in enumerate(boundaries[:-1]):
            for suffix, label in ((b"", "no EOF marker"), (b"\0" * 1024, "EOF marker added")):
                partial = self.app / f"cut-{i}-{len(suffix)}.tar.gz"
                partial.write_bytes(gzip.compress(raw[:cut] + suffix))
                with self.subTest(cut=cut, variant=label):
                    inner = self.run_inner(partial)
                    self.assertEqual(inner.returncode, 1, inner.stderr)
                    self.assertIn("RESTORE ABORTED", inner.stderr)
                    if suffix:      # full script for the well-formed partial archives
                        script = self.run_restore(partial)
                        self.assertEqual(script.returncode, 1, script.stderr)
                        self.assertIn("RESTORE ABORTED", script.stderr)
                        self.assertFalse(self.docker_log.exists())
                        self.assertFalse((self.app / "backups").exists())
                    self.assertEqual(self.live_snapshot(), before)
                    checked += 1
                partial.unlink()
        self.assertEqual(checked, 2 * (len(boundaries) - 1))
        self.assert_no_leftovers()

    def test_checksum_mismatch_aborts(self):
        archive = self.real_backup()
        before = self.live_snapshot()
        for label, sha in (("wrong", "0" * 64), ("missing", "")):
            with self.subTest(label):
                result = self.run_inner(archive, sha=sha)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("RESTORE ABORTED", result.stderr)
                self.assertIn("checksum", result.stderr)
                self.assertEqual(self.live_snapshot(), before)
        # a stream cut in transit: the host's sha256 of the whole file vs a short read
        import hashlib
        cut = self.app / "cut-in-transit.tar.gz"
        data = archive.read_bytes()
        cut.write_bytes(data[: len(data) * 2 // 3])
        result = self.run_inner(cut, sha=hashlib.sha256(data).hexdigest())
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("checksum mismatch", result.stderr)
        self.assertEqual(self.live_snapshot(), before)
        self.assert_no_leftovers()
        text = (ROOT / "scripts" / "restore.sh").read_text(encoding="utf-8")
        self.assertIn('-e EXPECTED_SHA256="$SHA"', text)                    # docker
        self.assertIn('EXPECTED_SHA256="$SHA" DIRS="$DIRS" "$PY" -c "$INNER"', text)  # local

    def test_missing_required_member_aborts(self):
        import gzip
        import tarfile
        archive = self.real_backup()
        before = self.live_snapshot()
        for drop in ("./state/secret_key", "./state/tasks.json", "./data", "./uploads/a.txt",
                     "./state/.snapshot/required.txt+./artifacts"):
            dropped = set(drop.split("+"))
            partial = self.app / "partial.tar.gz"
            with tarfile.open(archive) as src, tarfile.open(partial, "w:gz") as dst:
                for member in src:
                    if member.name.rstrip("/") in dropped or any(
                            member.name.startswith(d + "/") and d in ("./data", "./artifacts")
                            for d in dropped):
                        continue
                    dst.addfile(member, src.extractfile(member) if member.isfile() else None)
            with self.subTest(drop):
                script = self.run_restore(partial)
                self.assertEqual(script.returncode, 1, script.stderr)
                self.assertIn("the archive is incomplete", script.stderr)
                self.assertFalse(self.docker_log.exists())
                self.assertFalse((self.app / "backups").exists())
                inner = self.run_inner(partial)
                self.assertEqual(inner.returncode, 1, inner.stderr)
                self.assertIn("the archive is incomplete", inner.stderr)
                self.assertEqual(self.live_snapshot(), before)
        self.assert_no_leftovers()

    def test_reserved_restore_names_are_refused(self):
        archive = self.build("reserved.tar.gz", self.base_members() + [
            self.regular("./uploads/.restore-evil.txt", b"survives a rollback?"),
            self.regular("./uploads/zzz-late.txt", b"x")])
        before = self.live_snapshot()
        script = self.run_restore(archive)
        self.assertEqual(script.returncode, 1, script.stderr)
        self.assertIn("RESTORE ABORTED", script.stderr)
        self.assertFalse(self.docker_log.exists())
        for prelude in ("", "real_rename = os.rename\ncalls = [0]\n"
                            "def rename(a, b):\n    calls[0] += 1\n"
                            "    if calls[0] == 5:\n        raise OSError('injected')\n"
                            "    return real_rename(a, b)\nos.rename = rename\n"):
            inner = self.run_inner(archive, prelude)
            self.assertEqual(inner.returncode, 1, inner.stderr)
            self.assertIn("reserved name in the archive: ./uploads/.restore-evil.txt",
                          inner.stderr)
            self.assertFalse((self.app / "uploads" / ".restore-evil.txt").exists())
            self.assertEqual(self.live_snapshot(), before)
        self.assert_no_leftovers()

    def test_host_check_rejects_backslashes_before_backup_and_stop(self):
        archive = self.build("backslash.tar.gz", self.base_members() + [
            self.regular("./state/..\\..\\x", b"windows-style traversal")])
        before = self.live_snapshot()
        for flags in ((), ("--local",)):
            with self.subTest(mode=flags or "docker"):
                result = self.run_restore(archive, *flags)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("RESTORE ABORTED: unexpected or unsafe path", result.stderr)
                self.assertRegex(result.stderr, r"\.\.\\+\.\.\\+x")   # tar escapes "\\" in listings
                self.assertNotIn("Taking a safety backup", result.stdout)
                self.assertFalse(self.docker_log.exists())
                self.assertFalse((self.app / "backups").exists())
                self.assertEqual(self.live_snapshot(), before)

    def test_no_link_following_chmod_and_no_delete_before_extract(self):
        text = (ROOT / "scripts" / "restore.sh").read_text(encoding="utf-8")
        self.assertNotIn("find \"$d\" -mindepth 1 -delete", text)
        self.assertNotIn("chmod 600 state/orch.db state/secret_key", text)
        self.assertIn("if not (member.isfile() or member.isdir()):", text)
        self.assertIn("O_NOFOLLOW", text)
        self.assertIn("not os.path.islink(path)", text)
        inner = self.inner_code()
        order = [inner.index("archive = receive()"), inner.index("extract(archive)"),
                 inner.index("check_required()\n    verify"), inner.index("verify_staged_db()\n"),
                 inner.index("swap()\n    check")]
        self.assertEqual(order, sorted(order))      # all verification before any swap
        self.assertIn('-c "$INNER" < "$ARCHIVE"', text)                 # both modes
        self.assertEqual(text.count('-c "$INNER" < "$ARCHIVE"'), 2)


if __name__ == "__main__":
    unittest.main()
