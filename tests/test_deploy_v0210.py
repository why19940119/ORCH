"""v0.21.0 (WP-ORCH-12): deployment pieces - /healthz, /setup wizard,
persisted session key, branding, proxy options, Docker files, docs."""

import json
import os
import re
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import commerce_demo
import deploy_config
import orch_auth
import orch_db
import orch_ui
from auth_testing import signed_in, use_temp_auth
from orch_ui import PROJECT_ROOT, app
from ui_i18n import SUPPORTED_LOCALES, ui_strings

try:
    import yaml
except ImportError:          # PyYAML is a dev-only dependency
    yaml = None

PASSWORD = "Setup-Wizard-Pass-1"


class TempStateMixin:
    def temp_state(self):
        tmp = Path(tempfile.mkdtemp(prefix="orch_deploy_test_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        state = tmp / "state"
        state.mkdir()
        p = patch.object(orch_ui, "STATUS_FILE", state / "task_status.json")
        p.start()
        self.addCleanup(p.stop)
        return state


class HealthzTests(TempStateMixin, unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.state = self.temp_state()

    def test_healthz_unauthenticated_in_setup_mode_and_after(self):
        use_temp_auth(self, users=())
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(),
                         {"ok": True, "version": "v0.21.2", "db": "ok"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertTrue((self.state / "orch.db").is_file())
        orch_auth.bootstrap_admin("First Admin", PASSWORD)
        self.assertEqual(self.client.get("/healthz").status_code, 200)   # no login needed

    def test_healthz_reports_db_error(self):
        use_temp_auth(self, users=())
        with patch.object(orch_db, "ping", side_effect=RuntimeError("disk")):
            response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["db"], "error")
        self.assertNotIn("disk", response.get_data(as_text=True))


class SetupWizardTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()
        use_temp_auth(self, users=())
        # v0.21.1: /setup needs a token by default; these wizard tests use the
        # explicit local-dev opt-out (token behaviour: test_hardening_v0211).
        env = patch.dict(os.environ, {"ORCH_SETUP_LOCAL_NO_TOKEN": "1", "ORCH_SETUP_TOKEN": "",
                                      "ORCH_TRUSTED_HOSTS": "", "ORCH_PROXY_FIX": ""})
        env.start()
        self.addCleanup(env.stop)

    def token(self):
        page = self.client.get("/setup")
        self.assertEqual(page.status_code, 503)
        html = page.get_data(as_text=True)
        self.assertIn("data-setup-form", html)
        return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

    def post(self, **fields):
        data = {"csrf_token": self.token(), "username": "First Admin",
                "password": PASSWORD, "password_confirm": PASSWORD}
        data.update(fields)
        return self.client.post("/setup", data=data)

    def test_creates_first_admin_once_and_closes(self):
        response = self.post()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))
        user = orch_auth.get_user("First Admin")
        self.assertEqual(user["role"], "admin")
        record = orch_auth.read_audit()[0]
        self.assertEqual((record["event"], record["actor"]), ("bootstrap_admin", "web_setup"))
        self.assertNotIn(PASSWORD, json.dumps(orch_auth.read_audit()))
        # closed for good: GET redirects, POST refused, no second admin
        self.assertEqual(self.client.get("/setup").status_code, 302)
        with self.client.session_transaction() as stored:
            token = stored["csrf_token"]
        again = self.client.post("/setup", data={"csrf_token": token, "username": "Mallory X",
                                                 "password": PASSWORD, "password_confirm": PASSWORD})
        self.assertIn(again.status_code, (302, 403))
        self.assertIsNone(orch_auth.get_user("Mallory X"))
        login = self.client.get("/login").get_data(as_text=True)
        self.assertIn(ui_strings("zh-Hant")["auth_notice_setup_done"], login)

    def test_csrf_required(self):
        self.client.get("/setup")
        response = self.client.post("/setup", data={"username": "First Admin", "password": PASSWORD,
                                                    "password_confirm": PASSWORD})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(orch_auth.has_users())

    def test_validation_errors(self):
        self.assertEqual(self.post(password_confirm="Other-Pass-2222").status_code, 400)
        self.assertEqual(self.post(password="short", password_confirm="short").status_code, 400)
        self.assertEqual(self.post(username="x").status_code, 400)
        self.assertFalse(orch_auth.has_users())

    def test_setup_token(self):
        with patch.dict(os.environ, {"ORCH_SETUP_TOKEN": "tok-123456"}):
            self.assertIn('name="setup_token"', self.client.get("/setup").get_data(as_text=True))
            self.assertEqual(self.post(setup_token="wrong").status_code, 403)
            self.assertFalse(orch_auth.has_users())
            self.assertEqual(self.post(setup_token="tok-123456").status_code, 302)
        self.assertTrue(orch_auth.has_users())

    def test_cli_create_admin_still_documented(self):
        self.assertIn("orch_auth.py create-admin", self.client.get("/setup").get_data(as_text=True))


class SecretKeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_persisted_once_0600_and_env_wins(self):
        env = {"ORCH_UI_SECRET_KEY": "", "ORCH_PERSIST_SECRET_KEY": "1"}
        with patch.dict(os.environ, env):
            key, source = deploy_config.resolve_secret_key(self.tmp)
            again, _ = deploy_config.resolve_secret_key(self.tmp)
        self.assertEqual(source, "file")
        self.assertEqual(key, again)
        self.assertGreaterEqual(len(key), 32)
        self.assertEqual(stat.S_IMODE((self.tmp / "secret_key").stat().st_mode), 0o600)
        with patch.dict(os.environ, {"ORCH_UI_SECRET_KEY": "from-env-" + "x" * 30,
                                     "ORCH_PERSIST_SECRET_KEY": "1"}):
            self.assertEqual(deploy_config.resolve_secret_key(self.tmp)[1], "env")

    def test_default_is_ephemeral_and_writes_nothing(self):
        with patch.dict(os.environ, {"ORCH_UI_SECRET_KEY": "", "ORCH_PERSIST_SECRET_KEY": ""}):
            self.assertEqual(deploy_config.resolve_secret_key(self.tmp), (None, "ephemeral"))
        self.assertFalse((self.tmp / "secret_key").exists())


class BrandingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        clear = {k: "" for k in ("ORCH_CLIENT_NAME", "ORCH_LOGO", "ORCH_TARGET_MARKET",
                                 "ORCH_BRANDING_FILE")}
        p = patch.dict(os.environ, clear)
        p.start()
        self.addCleanup(p.stop)

    def test_file_then_env_override(self):
        (self.tmp / "branding").mkdir()
        (self.tmp / "branding" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (self.tmp / "branding.json").write_text(json.dumps(
            {"client_name": "示範商店", "logo": "branding/logo.png", "target_market": "香港"}),
            encoding="utf-8")
        b = deploy_config.load_branding(self.tmp)
        self.assertEqual((b["client_name"], b["target_market"], b["logo_src"]),
                         ("示範商店", "香港", "/branding/logo"))
        with patch.dict(os.environ, {"ORCH_CLIENT_NAME": "Env Shop\x07\n",
                                     "ORCH_LOGO": "https://cdn.example.com/l.png"}):
            b = deploy_config.load_branding(self.tmp)
        self.assertEqual(b["client_name"], "Env Shop")
        self.assertEqual(b["logo_src"], "https://cdn.example.com/l.png")

    def test_logo_path_cannot_escape_state(self):
        (self.tmp / "secret.png").write_bytes(b"x")
        inner = self.tmp / "state"
        inner.mkdir()
        self.assertIsNone(deploy_config.logo_file("../secret.png", inner))
        self.assertIsNone(deploy_config.logo_file("/etc/passwd", inner))
        (inner / "notes.txt").write_text("x")
        self.assertIsNone(deploy_config.logo_file("notes.txt", inner))   # images only
        self.assertEqual(deploy_config.logo_src("javascript:alert(1)", inner), "")
        self.assertEqual(deploy_config.logo_src("http://insecure/x.png", inner), "")

    def test_header_footer_and_logo_route(self):
        app.config["TESTING"] = True
        client = app.test_client()
        signed_in(self, client)
        (self.tmp / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        with patch.object(deploy_config, "STATE_DIR", self.tmp), \
                patch.dict(os.environ, {"ORCH_CLIENT_NAME": "Harbour <b>Shop</b>",
                                        "ORCH_TARGET_MARKET": "Hong Kong",
                                        "ORCH_LOGO": "logo.png"}):
            html = client.get("/tasks").get_data(as_text=True)
            logo = client.get("/branding/logo")
        self.assertIn("Harbour &lt;b&gt;Shop&lt;/b&gt;", html)            # escaped
        t = ui_strings("zh-Hant")
        self.assertIn(f'data-brand-market>{t["brand_market_label"]}: Hong Kong', html)
        self.assertIn('src="/branding/logo"', html)
        self.assertIn(f"data-app-version>ORCH · {t['footer_version']} v0.21.2", html)
        self.assertEqual(logo.status_code, 200)
        self.assertEqual(logo.headers["X-Content-Type-Options"], "nosniff")
        with patch.object(deploy_config, "STATE_DIR", self.tmp):
            self.assertEqual(client.get("/branding/logo").status_code, 404)   # none configured

    def test_no_branding_by_default(self):
        app.config["TESTING"] = True
        client = app.test_client()
        signed_in(self, client)
        with patch.object(deploy_config, "STATE_DIR", self.tmp):
            html = client.get("/tasks").get_data(as_text=True)
        self.assertNotIn("data-brand-client", html)
        self.assertNotIn("data-brand-logo", html)
        self.assertIn("data-app-version", html)


class ProxyOptionTests(unittest.TestCase):
    def test_proxy_hops_parsing(self):
        for value, hops in (("", 0), ("0", 0), ("off", 0), ("1", 1), ("true", 1),
                            ("2", 2), ("99", 5), ("junk", 0)):
            with patch.dict(os.environ, {"ORCH_PROXY_FIX": value}):
                self.assertEqual(deploy_config.proxy_hops(), hops, value)

    def test_trusted_hosts_env(self):
        with patch.dict(os.environ, {"ORCH_TRUSTED_HOSTS": " orch.example.com, .corp.hk ,"}):
            self.assertEqual(deploy_config.extra_trusted_hosts(), ["orch.example.com", ".corp.hk"])

    def test_session_cookie_hardening(self):
        self.assertTrue(app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(app.config["SESSION_COOKIE_SAMESITE"], "Lax")


class DockerFilesTests(unittest.TestCase):
    def read(self, name):
        return (PROJECT_ROOT / name).read_text(encoding="utf-8")

    def test_dockerfile(self):
        text = self.read("Dockerfile")
        self.assertRegex(text, r"(?m)^FROM python:3\.\d+-slim$")
        self.assertRegex(text, r"(?m)^USER orch$")
        self.assertIn("HEALTHCHECK", text)
        self.assertIn("/healthz", text)
        self.assertIn('CMD ["python", "serve.py"]', text)
        self.assertNotRegex(text, r"(?i)(OPENROUTER_API_KEY|ORCH_UI_SECRET_KEY)\s*=")
        self.assertNotRegex(text, r"(?m)^COPY .*\.env")
        ignore = self.read(".dockerignore")
        for entry in (".env", "state/*", ".git", ".venv", "*.db"):
            self.assertIn(entry, ignore.splitlines())

    @unittest.skipIf(yaml is None, "PyYAML not installed (dev-only)")
    def test_compose_structure(self):
        compose = yaml.safe_load(self.read("docker-compose.yml"))
        service = compose["services"]["orch"]
        self.assertEqual(service["build"], ".")
        mounts = {v.split(":")[1] for v in service["volumes"]}
        self.assertEqual(mounts, {"/app/state", "/app/uploads", "/app/data", "/app/artifacts",
                                  "/app/output"})
        for v in service["volumes"]:
            self.assertIn(v.split(":")[0], compose["volumes"])
        self.assertIn("/healthz", " ".join(service["healthcheck"]["test"]))
        self.assertEqual(service["environment"]["ORCH_PERSIST_SECRET_KEY"], "1")
        self.assertTrue(all(str(p).startswith("127.0.0.1:") for p in service["ports"]))
        # v0.21.1: .env by default; scripts/smoke.sh points ORCH_ENV_FILE elsewhere
        self.assertEqual(service["env_file"][0]["path"], "${ORCH_ENV_FILE:-.env}")
        self.assertIs(service["env_file"][0]["required"], False)
        self.assertNotIn("OPENROUTER_API_KEY", service["environment"])

    def test_compose_text_without_yaml(self):
        text = self.read("docker-compose.yml")
        for needle in ("orch_state:/app/state", "orch_uploads:/app/uploads",
                       "orch_data:/app/data", "orch_artifacts:/app/artifacts", "/healthz"):
            self.assertIn(needle, text)
        self.assertNotIn("\t", text)

    def test_scripts_present_and_executable(self):
        for name in ("backup.sh", "restore.sh", "smoke.sh"):
            path = PROJECT_ROOT / "scripts" / name
            self.assertTrue(os.access(path, os.X_OK), name)
            self.assertTrue(path.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash"))
        backup = self.read("scripts/backup.sh")
        self.assertIn("orch_db.py --state-dir state backup", backup)   # SQLite backup API
        # the live DB files are never archived (v0.21.1: tar takes the manifest
        # list, which skips them; the snapshot is archived instead)
        self.assertIn("! -path ./state/orch.db ! -path ./state/orch.db-wal", backup)
        self.assertIn("! -path ./state/orch.db-shm", backup)
        self.assertIn("--no-recursion -T -", backup)
        self.assertIn("tar -czf - ", backup)       # streamed (review fix 2)

    def test_zh_hant_docs(self):
        docs = PROJECT_ROOT / "docs"
        for name in ("安裝指南.md", "使用手冊.md", "SOP.md", "反向代理與HTTPS.md"):
            self.assertTrue((docs / name).is_file(), name)
        sop = (docs / "SOP.md").read_text(encoding="utf-8")
        self.assertIn("權限清單", sop)
        self.assertIn("/admin/permissions", sop)
        proxy = (docs / "反向代理與HTTPS.md").read_text(encoding="utf-8")
        for needle in ("SESSION_COOKIE_SECURE=1", "ORCH_PROXY_FIX=1", "Caddyfile", "nginx"):
            self.assertIn(needle, proxy)


class VersionAndI18nTests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(commerce_demo.DEMO_VERSION, "v0.21.2")
        self.assertEqual(orch_ui.APP_VERSION, "v0.21.2")
        self.assertIn("v0.21.2", (PROJECT_ROOT / "README.md").read_text(encoding="utf-8"))

    def test_i18n_parity(self):
        keys = {loc: set(ui_strings(loc)) for loc in SUPPORTED_LOCALES}
        base = keys[SUPPORTED_LOCALES[0]]
        for loc, k in keys.items():
            self.assertEqual(k, base, loc)
        for key in ("setup_button", "setup_password_confirm", "auth_notice_setup_done",
                    "brand_market_label", "footer_version", "auth_err_setup_token"):
            self.assertIn(key, base)


if __name__ == "__main__":
    unittest.main()
