"""PR #5 review fixes (v0.20.0): logout revocation, absolute session
expiry, first-run-only single-admin exception, login enumeration / DoS,
admin-only governance audit, CSV hardening, self-rejection and CLI OS
user. Model calls are mocked (ORCH_DEMO_FORCE_MOCK=1)."""

import getpass
import io
import json
import os
import re
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import commerce_demo
import mini_orch
import orch_auth
import orch_db
from auth_testing import TEST_PASSWORD, seed_users, sign_in, use_temp_auth
from orch_ui import app
from test_commerce_demo import DemoSandbox
from ui_i18n import SUPPORTED_LOCALES, ui_strings

COOKIE = app.config.get("SESSION_COOKIE_NAME", "orch_session")


def csrf(client, path="/login"):
    client.get(path)
    with client.session_transaction() as stored:
        return stored["csrf_token"]


def web_login(client, username, password=TEST_PASSWORD):
    token = csrf(client)
    return client.post("/login", data={"csrf_token": token, "username": username,
                                       "password": password, "next": "/tasks"})


class SessionTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        use_temp_auth(self, users=(("Ann Admin", "admin"), ("Eve Editor", "editor")))
        self.client = app.test_client()

    def test_1_replayed_cookie_fails_after_logout(self):
        self.assertEqual(web_login(self.client, "Eve Editor").status_code, 302)
        stolen = self.client.get_cookie(COOKIE).value
        thief = app.test_client()
        thief.set_cookie(COOKIE, stolen)
        self.assertEqual(thief.get("/tasks").status_code, 200)     # copy works while signed in
        self.client.get("/tasks")                                   # new CSRF token after login
        with self.client.session_transaction() as stored:
            token = stored["csrf_token"]
        self.client.post("/logout", data={"csrf_token": token})
        replay = app.test_client()
        replay.set_cookie(COOKIE, stolen)
        response = replay.get("/tasks")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        self.assertEqual(orch_auth.get_user("Eve Editor")["session_epoch"], 2)

    def test_2_absolute_expiry(self):
        sign_in(self.client, "Eve Editor")
        self.assertEqual(self.client.get("/tasks").status_code, 200)
        with self.client.session_transaction() as stored:
            stored["auth_login_at"] = time.time() - 13 * 3600   # default 12h
            stored["auth_seen"] = time.time()                   # active, not idle
        self.assertEqual(self.client.get("/tasks").status_code, 302)
        record = orch_auth.read_audit()[0]
        self.assertEqual((record["event"], record["kind"]), ("session_expired", "absolute"))
        with patch.dict(os.environ, {"ORCH_SESSION_MAX_HOURS": "1"}):
            sign_in(self.client, "Eve Editor")
            with self.client.session_transaction() as stored:
                stored["auth_login_at"] = time.time() - 2 * 3600
            self.assertEqual(self.client.get("/tasks").status_code, 302)
            sign_in(self.client, "Eve Editor")
            self.assertEqual(self.client.get("/tasks").status_code, 200)
        self.assertEqual(orch_auth.max_session_seconds(), 12 * 3600)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        use_temp_auth(self, users=(("Ann Admin", "admin"),))

    def create(self, actor, name, role):
        return orch_auth.request_change(actor, "create_user", name, role=role,
                                        password="Strong-Pass-123")

    def disable_directly(self, name):
        store = orch_auth.load_store()
        store["users"][orch_auth._key(name)]["disabled"] = True
        orch_auth._save_store(store)

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = orch_auth.main(list(args))
        return code, out.getvalue()

    def test_3_exception_is_first_run_only(self):
        self.assertTrue(orch_auth.bootstrap_open())
        self.assertEqual(self.create("Ann Admin", "Ed Editor", "editor")["status"], "applied")
        self.assertTrue(orch_auth.bootstrap_open())         # non-admins keep it open
        self.assertEqual(self.create("Ann Admin", "Bob Admin", "admin")["status"], "applied")
        self.assertFalse(orch_auth.bootstrap_open())        # second admin closes it
        self.assertIn("bootstrap_complete", [r["event"] for r in orch_auth.read_audit()])
        # Back to one active admin: no automatic shortcut any more.
        self.disable_directly("Bob Admin")
        change = self.create("Ann Admin", "Amy Approver", "approver")
        self.assertEqual(change["status"], "pending")
        self.assertTrue(change["awaiting_second_admin"])
        self.assertIsNone(orch_auth.get_user("Amy Approver"))
        # Explicit, audited CLI step re-opens it.
        code, out = self.run_cli("allow-single-admin")
        self.assertEqual(code, 0, out)
        record = orch_auth.read_audit()[0]
        self.assertEqual(record["event"], "single_admin_reenabled")
        self.assertEqual(record["os_user"], getpass.getuser())
        self.assertEqual(self.create("Ann Admin", "Amy Approver", "approver")["status"], "applied")

    def test_3_cli_refused_with_two_admins(self):
        self.create("Ann Admin", "Bob Admin", "admin")
        code, out = self.run_cli("allow-single-admin")
        self.assertEqual(code, 1)
        self.assertIn("exactly one active admin", out)
        self.assertFalse(orch_auth.bootstrap_open())

    def test_3_migration_of_old_stores(self):
        path = orch_auth.auth_file()      # v0.21.0: docs['auth'] in orch.db
        data = orch_db.load(path, {})
        data.pop("bootstrap")
        orch_db.save(path, data)
        self.assertTrue(orch_auth.bootstrap_open())          # one admin, never a second
        seed_users((("Old Admin", "admin"),))
        data = orch_db.load(path, {})
        data.pop("bootstrap", None)
        data["users"][orch_auth._key("Old Admin")]["disabled"] = True
        orch_db.save(path, data)
        self.assertFalse(orch_auth.bootstrap_open())         # a second admin existed


class AdminUsersPageTests(DemoSandbox):
    def test_3_closed_note_and_flash(self):
        seed_users((("Zed Admin", "admin"),))
        store = orch_auth.load_store()
        store["bootstrap"] = {"complete": True}
        store["users"][orch_auth._key("Zed Admin")]["disabled"] = True
        orch_auth._save_store(store)
        self.as_user("Admin One")
        html = self.client.get("/admin/users").get_data(as_text=True)
        self.assertIn("data-single-admin-closed", html)
        self.assertIn("allow-single-admin", html)
        self.client.post("/admin/users/request", data={
            "csrf_token": self.token, "kind": "create_user", "target": "New Person",
            "role": "editor", "password": "New-Person-123"})
        html = self.client.get("/admin/users").get_data(as_text=True)
        self.assertIn('data-admin-flash="awaiting_second_admin"', html)

    def test_4_admin_unlock_button(self):
        with patch.dict(os.environ, {"ORCH_LOGIN_MAX_FAILURES": "2"}):
            for _ in range(2):
                orch_auth.authenticate("Ben Lee", "wrong-wrong-1")
        self.assertTrue(orch_auth.is_locked(orch_auth.get_user("Ben Lee")))
        self.as_user("Amy Chan")
        self.assertEqual(self.client.post("/admin/users/unlock", data={
            "csrf_token": self.token, "target": "Ben Lee"}).status_code, 403)
        self.as_user("Admin One")
        html = self.client.get("/admin/users").get_data(as_text=True)
        self.assertIn("data-unlock-form", html)
        self.assertEqual(self.client.post("/admin/users/unlock", data={"target": "Ben Lee"}).status_code, 400)
        self.client.post("/admin/users/unlock", data={"csrf_token": self.token, "target": "Ben Lee"})
        self.assertFalse(orch_auth.is_locked(orch_auth.get_user("Ben Lee")))
        record = orch_auth.read_audit()[0]
        self.assertEqual((record["event"], record["actor"], record["via"], record["target"]),
                         ("account_unlocked", "Admin One", "web", "Ben Lee"))


class LoginEnumerationTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        use_temp_auth(self, users=(("Ann Admin", "admin"), ("Dee Disabled", "editor")))
        store = orch_auth.load_store()
        store["users"][orch_auth._key("Dee Disabled")]["disabled"] = True
        orch_auth._save_store(store)
        self.client = app.test_client()

    def test_4_generic_message_for_every_failure(self):
        messages = set()
        for name, password in (("Nobody Here", "whatever-pass"), ("Ann Admin", "wrong-pass-1"),
                               ("Dee Disabled", TEST_PASSWORD)):
            html = web_login(self.client, name, password).get_data(as_text=True)
            messages.add(re.search(r'data-login-error="([a-z]+)"', html).group(1))
        self.assertEqual(messages, {"invalid"})

    def test_4_unknown_usernames_counted_and_locked(self):
        with patch.dict(os.environ, {"ORCH_LOGIN_MAX_FAILURES": "3"}):
            reasons = [orch_auth.authenticate("Ghost User", "x-x-x-x-x-1")[1] for _ in range(4)]
        self.assertEqual(reasons, ["invalid", "invalid", "locked", "locked"])
        text = json.dumps(orch_db.read_log(orch_auth.audit_file()), ensure_ascii=False)
        self.assertNotIn("Ghost User", text)                   # 5: typed name not stored raw
        self.assertNotIn("ghost user", text)
        record = orch_auth.read_audit()[0]
        self.assertEqual(record["actor"], "(unknown)")
        self.assertEqual(record["typed_username_sha256"], orch_auth.typed_username_ref("ghost user"))

    def test_4_hash_checked_on_every_path(self):
        real = orch_auth.check_password_hash
        with patch.object(orch_auth, "check_password_hash", side_effect=real) as checker, \
                patch.dict(os.environ, {"ORCH_LOGIN_MAX_FAILURES": "1"}):
            orch_auth.authenticate("Nobody", "p-p-p-p-p-1")          # unknown
            orch_auth.authenticate("Dee Disabled", TEST_PASSWORD)    # disabled
            orch_auth.authenticate("Ann Admin", "wrong-pass-1")      # -> locked
            orch_auth.authenticate("Ann Admin", TEST_PASSWORD)       # locked
        self.assertEqual(checker.call_count, 4)


class AuditVisibilityTests(DemoSandbox):
    def test_5_governance_audit_admin_only(self):
        for user in ("Amy Chan", "Ben Lee"):
            self.as_user(user)
            html = self.client.get("/audit").get_data(as_text=True)
            self.assertNotIn("data-governance-audit", html, user)
        self.as_user("Admin One")
        self.assertIn("data-governance-audit", self.client.get("/audit").get_data(as_text=True))

    def test_8_author_cannot_reject_own_draft(self):
        task_id = self.create_one()
        self.as_user("Cara Wong")
        self.client.post(f"/inbox/{task_id}/revise", data={
            "csrf_token": self.token, "version": "1", "body": "Edited copy"})
        orch_auth.request_change("Admin One", "change_role", "Cara Wong", role="approver")
        with self.assertRaises(commerce_demo.DemoError) as caught:
            commerce_demo.decide(task_id, "rejected", "Cara Wong", 2, note="no")
        self.assertEqual(caught.exception.code, "self_rejection")
        commerce_demo.decide(task_id, "rejected", "Ben Lee", 2, note="off-brand")
        self.assertEqual(self.statuses()[task_id]["approval_status"], "rejected")
        result = mini_orch.decide_approval(
            "any", "rejected", "amy chan", requested_by=["Amy Chan"])
        self.assertEqual(result, {"ok": False, "reason": "self_rejection"})


class CsvTests(DemoSandbox):
    def test_6_formula_cells_neutralised(self):
        for raw in ("=1+1", "+SUM(A1)", "-2", "@evil", "\tx", "\rx"):
            self.assertEqual(orch_auth.csv_safe(raw), "'" + raw)
        self.assertEqual(orch_auth.csv_safe("Ben Lee"), "Ben Lee")
        seed_users((("@evil", "editor"), ("-1 cmd", "approver")))
        self.as_user("Admin One")
        text = self.client.get("/admin/permissions.csv").get_data().decode("utf-8")
        lines = text.lstrip("\ufeff").splitlines()
        self.assertTrue(any(line.startswith("'@evil,") for line in lines), lines)
        self.assertTrue(any(line.startswith("'-1 cmd,") for line in lines), lines)
        self.assertFalse(any(line.startswith(("@", "-", "=", "+")) for line in lines))

    def test_7_content_type_and_hkt_times(self):
        self.as_user("Admin One")
        response = self.client.get("/admin/permissions.csv")
        self.assertEqual(response.headers["Content-Type"], "text/csv; charset=utf-8")
        text = response.get_data().decode("utf-8")
        self.assertRegex(text, r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \+08:00")
        self.assertNotIn("+00:00", text)
        self.assertEqual(orch_auth.display_time("2026-09-29T10:00:00+00:00"),
                         "2026-09-29 18:00:00 +08:00")


class CliOsUserTests(unittest.TestCase):
    def setUp(self):
        use_temp_auth(self, users=(("Ann Admin", "admin"),))

    def run_cli(self, *args, passwords=()):
        answers = iter(passwords)
        out = io.StringIO()
        with patch.object(orch_auth.getpass, "getpass", lambda prompt="": next(answers)), \
                redirect_stdout(out), redirect_stderr(out):
            return orch_auth.main(list(args))

    def test_9_cli_events_record_os_user(self):
        me = getpass.getuser()
        self.assertEqual(self.run_cli("unlock", "Ann Admin"), 0)
        self.assertEqual(orch_auth.read_audit()[0]["os_user"], me)
        self.assertEqual(self.run_cli("reset-password", "Ann Admin",
                                      passwords=("Brand-New-Pass-1",) * 2), 0)
        self.assertEqual(orch_auth.read_audit()[0]["os_user"], me)

    def test_9_mini_orch_cli_approve_records_os_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, status, events = (Path(tmp) / n for n in ("q.json", "s.json", "e.jsonl"))
            queue.write_text(json.dumps([{"id": "task_x", "title": "x", "command": ["true"],
                                          "priority": 1, "depends_on": [], "max_retries": 0,
                                          "requires_approval": True}]))
            result = mini_orch.decide_approval("task_x", "approved", "local_terminal_user",
                                               queue_file=queue, status_file=status,
                                               events_file=events, os_user="alice")
            self.assertEqual(result["state"]["decided_os_user"], "alice")
            self.assertEqual(json.loads(events.read_text().splitlines()[-1])["os_user"], "alice")
        with patch.object(mini_orch, "decide_approval", return_value={"ok": True}) as gate, \
                patch.object(mini_orch, "state_lock", MagicMock()), redirect_stdout(io.StringIO()):
            mini_orch.approve_task("task_plain_1")
        self.assertEqual(gate.call_args.kwargs["os_user"], getpass.getuser())

    def test_9_readme_documents_trust(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
        self.assertIn("allow-single-admin", readme)
        self.assertIn("shell access", readme.lower())
        self.assertIn("ORCH_SESSION_MAX_HOURS", readme)


class I18nTests(unittest.TestCase):
    def test_new_keys_in_every_locale(self):
        keys = ("adm_unlock", "adm_single_admin_closed_note", "gov_msg_unlocked",
                "gov_msg_awaiting_second_admin", "gov_ev_single_admin_reenabled",
                "gov_ev_bootstrap_complete", "demo_err_self_rejection",
                "auth_err_single_admin_not_applicable")
        for key in keys:
            values = [ui_strings(code)[key] for code in SUPPORTED_LOCALES]
            self.assertEqual(len(set(values)), len(values), key)
        for code in SUPPORTED_LOCALES:
            self.assertNotIn("auth_err_login_locked", ui_strings(code))


if __name__ == "__main__":
    unittest.main()
