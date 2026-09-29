"""v0.20.0 (WP-ORCH-11) accounts, roles and governance tests.

State, artifacts and the account store are sandboxed; drafts use the
mock generator (ORCH_DEMO_FORCE_MOCK=1), so no model is ever called.
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import chat_attachments
import commerce_demo
import mini_orch
import orch_auth
from auth_testing import TEST_PASSWORD, seed_users, sign_in, use_temp_auth
from orch_ui import PROJECT_ROOT, app
from test_commerce_demo import DemoSandbox
from ui_i18n import SUPPORTED_LOCALES, ui_strings


class NoAccountsTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        use_temp_auth(self, users=())
        self.client = app.test_client()

    def test_every_page_points_to_bootstrap(self):
        for path in ("/", "/tasks", "/inbox", "/admin/users", "/login"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302, path)
            self.assertTrue(response.headers["Location"].endswith("/setup"), path)
        page = self.client.get("/setup")
        self.assertEqual(page.status_code, 503)
        html = page.get_data(as_text=True)
        self.assertIn("orch_auth.py create-admin", html)
        self.assertIn("data-setup-required", html)
        self.assertNotIn("<nav>", html)

    def test_no_open_signup(self):
        rules = {rule.rule for rule in app.url_map.iter_rules()}
        self.assertFalse(any("signup" in r or "register" in r for r in rules))

    def test_json_clients_get_401(self):
        response = self.client.get("/chat?format=json")
        self.assertEqual(response.status_code, 401)


class LoginTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        use_temp_auth(self, users=(("Ann Admin", "admin"), ("Eve Editor", "editor")))
        self.client = app.test_client()
        self.client.get("/login")
        with self.client.session_transaction() as stored:
            self.token = stored["csrf_token"]

    def login(self, username, password, token=None):
        return self.client.post("/login", data={
            "csrf_token": self.token if token is None else token,
            "username": username, "password": password, "next": "/inbox"})

    def test_login_logout_and_redirect(self):
        self.assertEqual(self.client.get("/tasks").status_code, 302)
        response = self.login("eve editor", TEST_PASSWORD)  # case-insensitive
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/inbox"))
        page = self.client.get("/tasks").get_data(as_text=True)
        self.assertIn('data-current-user="Eve Editor"', page)
        with self.client.session_transaction() as stored:
            token = stored["csrf_token"]
        self.assertNotEqual(token, self.token)            # session rotated
        self.assertEqual(self.client.post("/logout", data={}).status_code, 400)
        self.client.post("/logout", data={"csrf_token": token})
        self.assertEqual(self.client.get("/tasks").status_code, 302)
        events = [r["event"] for r in orch_auth.read_audit()]
        self.assertIn("login_success", events)
        self.assertIn("logout", events)

    def test_login_requires_csrf(self):
        self.assertEqual(self.login("Eve Editor", TEST_PASSWORD, token="x").status_code, 400)

    def test_wrong_password_generic_message(self):
        response = self.login("Eve Editor", "nope-nope-nope")
        self.assertEqual(response.status_code, 401)
        self.assertIn('data-login-error="invalid"', response.get_data(as_text=True))
        response = self.login("Nobody Here", "nope-nope-nope")
        self.assertIn('data-login-error="invalid"', response.get_data(as_text=True))

    def test_lockout_and_time_based_unlock(self):
        with patch.dict(os.environ, {"ORCH_LOGIN_MAX_FAILURES": "3",
                                     "ORCH_LOGIN_LOCKOUT_MINUTES": "10"}):
            # Review fix: the page shows one generic message, locked or not.
            for _ in range(3):
                self.assertIn('data-login-error="invalid"',
                              self.login("Eve Editor", "bad-password-1").get_data(as_text=True))
            locked = self.login("Eve Editor", TEST_PASSWORD).get_data(as_text=True)
            self.assertIn('data-login-error="invalid"', locked)   # right password, still refused
            self.assertEqual(self.client.get("/tasks").status_code, 302)
            self.assertEqual(orch_auth.authenticate("Eve Editor", TEST_PASSWORD)[1], "locked")
            later = datetime.now(timezone.utc) + timedelta(minutes=11)
            user, reason = orch_auth.authenticate("Eve Editor", TEST_PASSWORD, now=later)
            self.assertEqual(reason, "ok")
        events = [r["event"] for r in orch_auth.read_audit()]
        self.assertIn("account_locked", events)
        self.assertIn("login_refused_locked", events)

    def test_idle_expiry(self):
        self.login("Eve Editor", TEST_PASSWORD)
        with patch.dict(os.environ, {"ORCH_SESSION_IDLE_MINUTES": "5"}):
            with self.client.session_transaction() as stored:
                stored["auth_seen"] = time.time() - 6 * 60
            response = self.client.get("/tasks")
            self.assertEqual(response.status_code, 302)
            login = self.client.get("/login").get_data(as_text=True)
            self.assertIn(ui_strings("zh-Hant")["auth_notice_expired"], login)

    def test_disabled_user_session_revoked(self):
        self.login("Eve Editor", TEST_PASSWORD)
        orch_auth.request_change("Ann Admin", "disable", "Eve Editor")   # single admin: applies
        self.assertEqual(self.client.get("/tasks").status_code, 302)
        user, reason = orch_auth.authenticate("Eve Editor", TEST_PASSWORD)
        self.assertEqual(reason, "disabled")

    def test_passwords_never_stored_or_logged_in_clear(self):
        self.login("Eve Editor", TEST_PASSWORD)
        self.login("Eve Editor", "Some-Wrong-Pass-9")
        for path in (orch_auth.auth_file(), orch_auth.audit_file()):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(TEST_PASSWORD, text)
            self.assertNotIn("Some-Wrong-Pass-9", text)
        self.assertNotIn("password_hash", orch_auth.audit_file().read_text(encoding="utf-8"))

    def test_secure_cookie_env(self):
        code = "import orch_ui; print(orch_ui.app.config['SESSION_COOKIE_SECURE'], orch_ui.app.config['SESSION_COOKIE_HTTPONLY'])"
        env = {**os.environ, "SESSION_COOKIE_SECURE": "1", "ORCH_UI_SECRET_KEY": "x" * 32}
        out = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT, env=env,
                             capture_output=True, text=True, check=True).stdout
        self.assertIn("True True", out)
        env["SESSION_COOKIE_SECURE"] = ""
        out = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT, env=env,
                             capture_output=True, text=True, check=True).stdout
        self.assertIn("False True", out)


class RoleMatrixTests(DemoSandbox):
    def test_matrix(self):
        task_id = self.create_one()                       # Amy (editor)
        approve = {"csrf_token": self.token, "version": "1", "channel": "brand_site_faq"}
        cases = {
            # user: (draft, approve, admin page, import toggle)
            "Amy Chan": (302, 403, 403, 302),
            "Ben Lee": (403, 302, 403, 403),
            "Admin One": (403, 403, 200, 302),
        }
        for user, (draft, _approve, admin_page, toggle) in cases.items():
            self.as_user(user)
            self.assertEqual(self.draft().status_code, draft, user)
            self.assertEqual(self.client.get("/admin/users").status_code, admin_page, user)
            self.assertEqual(self.client.post("/import/toggle", data={
                "csrf_token": self.token, "active": "1"}).status_code, toggle, user)
        self.as_user("Amy Chan")
        self.assertEqual(self.client.post(f"/inbox/{task_id}/approve", data=approve).status_code, 403)
        self.as_user("Admin One")
        self.assertEqual(self.client.post(f"/inbox/{task_id}/approve", data=approve).status_code, 403)
        self.as_user("Ben Lee")
        self.assertEqual(self.client.post(f"/inbox/{task_id}/revise", data={
            "csrf_token": self.token, "version": "1", "body": "x"}).status_code, 403)
        # Shared path refuses too (not only the route).
        with self.assertRaises(commerce_demo.DemoError) as caught:
            commerce_demo.create_draft("market_insight", {}, "Ben Lee")
        self.assertEqual(caught.exception.code, "forbidden_role")
        with self.assertRaises(commerce_demo.DemoError) as caught:
            commerce_demo.decide(task_id, "approved", "Amy Chan", 1, channel="brand_site_faq")
        self.assertEqual(caught.exception.code, "forbidden_role")

    def test_operator_and_approver_come_from_accounts(self):
        task_id = self.create_one(operator="Somebody Typed")
        self.assertEqual(self.queue()[-1]["ecom_draft"]["created_by"], "Amy Chan")
        self.assertEqual(self.queue()[-1]["ecom_draft"]["identity"], "account")
        self.as_user("Ben Lee")
        self.client.post(f"/inbox/{task_id}/approve", data={
            "csrf_token": self.token, "version": "1", "channel": "brand_site_faq",
            "operator": "Typed Approver"})
        audit = commerce_demo.audit_records()[0]
        self.assertEqual((audit["operator"], audit["approver"]), ("Amy Chan", "Ben Lee"))
        self.assertEqual(audit["identity"], "account")
        html = self.client.get("/audit").get_data(as_text=True)
        self.assertIn("Ben Lee", html)
        self.assertNotIn("Typed Approver", html)
        for page in ("/sales", "/content", "/inbox"):
            self.assertNotIn('name="operator"', self.client.get(page).get_data(as_text=True), page)

    def test_legacy_typed_records_stay_readable(self):
        task_id = self.create_one()
        queue = self.queue()
        queue[-1]["ecom_draft"].pop("identity")
        queue[-1]["ecom_draft"].pop("due_at_utc")
        self.demo_queue_file.write_text(json.dumps(queue), encoding="utf-8")
        self.as_user("Ben Lee")
        html = self.client.get("/inbox").get_data(as_text=True)
        self.assertIn(ui_strings("zh-Hant")["gov_legacy_typed"], html)
        self.client.post(f"/inbox/{task_id}/approve", data={
            "csrf_token": self.token, "version": "1", "channel": "brand_site_faq"})
        self.assertEqual(commerce_demo.audit_records()[0]["draft_identity"], "typed")


class SelfApprovalTests(DemoSandbox):
    def test_editor_turned_approver_cannot_approve_own_draft(self):
        task_id = self.create_one()                               # Amy writes v1
        self.as_user("Cara Wong")
        self.client.post(f"/inbox/{task_id}/revise", data={
            "csrf_token": self.token, "version": "1", "body": "Edited copy"})
        orch_auth.request_change("Admin One", "change_role", "Cara Wong", role="approver")
        sign_in(self.client, "Cara Wong")
        html = self.client.get("/inbox").get_data(as_text=True)
        self.assertIn('data-decide-block="self"', html)
        self.client.post(f"/inbox/{task_id}/approve", data={
            "csrf_token": self.token, "version": "2", "channel": "brand_site_faq"})
        self.assertEqual(self.statuses()[task_id]["approval_status"], "waiting_approval")
        with self.assertRaises(commerce_demo.DemoError) as caught:
            commerce_demo.decide(task_id, "approved", "cara wong", 2, channel="brand_site_faq")
        self.assertEqual(caught.exception.code, "self_approval")
        # Another approver can.
        commerce_demo.decide(task_id, "approved", "Ben Lee", 2, channel="brand_site_faq")
        self.assertEqual(self.statuses()[task_id]["approval_status"], "approved")

    def test_gate_itself_refuses_self_approval(self):
        task_id = self.create_one()
        result = mini_orch.decide_approval(
            task_id, "approved", "AMY CHAN", requested_by=["Amy Chan"],
            queue_file=self.demo_queue_file, status_file=self.tmp / "state" / "task_status.json",
            events_file=self.tmp / "state" / "events.jsonl")
        self.assertEqual(result, {"ok": False, "reason": "self_approval"})


class ModuleApproverTests(DemoSandbox):
    def test_assignment_limits_approvers(self):
        task_id = self.create_one()                               # content_studio
        orch_auth.set_module_approvers("Admin One", "content_studio", ["Dan Ho"])
        self.as_user("Ben Lee")
        self.assertIn('data-decide-block="not_assigned"', self.client.get("/inbox").get_data(as_text=True))
        with self.assertRaises(commerce_demo.DemoError) as caught:
            commerce_demo.decide(task_id, "approved", "Ben Lee", 1, channel="brand_site_faq")
        self.assertEqual(caught.exception.code, "not_assigned")
        self.as_user("Dan Ho")
        self.client.post(f"/inbox/{task_id}/approve", data={
            "csrf_token": self.token, "version": "1", "channel": "brand_site_faq"})
        self.assertEqual(self.statuses()[task_id]["approved_by"], "Dan Ho")
        with self.assertRaises(orch_auth.AuthError):
            orch_auth.set_module_approvers("Admin One", "content_studio", ["Amy Chan"])
        with self.assertRaises(orch_auth.AuthError):
            orch_auth.set_module_approvers("Ben Lee", "content_studio", ["Ben Lee"])
        self.assertIn("module_approvers_set", [r["event"] for r in orch_auth.read_audit()])

    def test_admin_page_saves_assignment(self):
        self.as_user("Admin One")
        self.client.post("/admin/approvers", data={"csrf_token": self.token,
                                                    "lead_desk": ["Ben Lee"]})
        self.assertEqual(orch_auth.module_approvers()["lead_desk"], ["Ben Lee"])
        self.assertEqual(orch_auth.module_approvers()["sales_hub"], [])


class DeadlineTests(DemoSandbox):
    def test_overdue_flag_and_escalation(self):
        task_id = self.create_one(deadline_hours="4")
        meta = self.queue()[-1]["ecom_draft"]
        created = datetime.fromisoformat(meta["created_at_utc"])
        self.assertEqual(datetime.fromisoformat(meta["due_at_utc"]) - created, timedelta(hours=4))
        task = self.queue()[-1]
        state = self.statuses()[task_id]
        self.assertFalse(commerce_demo.is_overdue(task, state))
        self.assertTrue(commerce_demo.is_overdue(task, state, now=created + timedelta(hours=5)))
        # Make it overdue on disk and check Inbox + dashboard reminders.
        queue = self.queue()
        queue[-1]["ecom_draft"]["due_at_utc"] = (created - timedelta(hours=1)).isoformat()
        self.demo_queue_file.write_text(json.dumps(queue), encoding="utf-8")
        self.as_user("Ben Lee")
        inbox = self.client.get("/inbox").get_data(as_text=True)
        self.assertIn("data-escalation", inbox)
        self.assertIn("data-overdue", inbox)
        self.assertIn("Ben Lee", inbox)                   # escalate-to list
        self.assertIn("data-escalation", self.client.get("/").get_data(as_text=True))
        self.assertEqual([d["id"] for d in commerce_demo.overdue_drafts()], [task_id])
        commerce_demo.decide(task_id, "approved", "Ben Lee", 1, channel="brand_site_faq")
        self.assertTrue(commerce_demo.audit_records()[0]["overdue_at_decision"])
        self.assertEqual(commerce_demo.overdue_drafts(), [])

    def test_default_deadline_from_settings_and_invalid(self):
        orch_auth.update_settings("Admin One", default_deadline_hours=72)
        self.create_one()
        meta = self.queue()[-1]["ecom_draft"]
        delta = datetime.fromisoformat(meta["due_at_utc"]) - datetime.fromisoformat(meta["created_at_utc"])
        self.assertEqual(delta, timedelta(hours=72))
        self.draft(deadline_hours="9999")
        self.assertEqual(len(self.demo_task_ids()), 1)


class AdminChangeTests(unittest.TestCase):
    def setUp(self):
        use_temp_auth(self, users=(("Ann Admin", "admin"),))

    def test_single_admin_exception_then_second_admin_required(self):
        change = orch_auth.request_change("Ann Admin", "create_user", "Bob Admin",
                                          role="admin", password="Second-Admin-77")
        self.assertEqual(change["status"], "applied")
        self.assertTrue(change["bootstrap_exception"])
        # Two admins now: changes wait for the other admin.
        change = orch_auth.request_change("Ann Admin", "create_user", "Ed Editor",
                                          role="editor", password="Editor-Pass-88")
        self.assertEqual(change["status"], "pending")
        self.assertIsNone(orch_auth.get_user("Ed Editor"))
        with self.assertRaises(orch_auth.AuthError) as caught:
            orch_auth.decide_change("Ann Admin", change["id"], "approved")
        self.assertEqual(caught.exception.code, "second_admin_required")
        orch_auth.decide_change("Bob Admin", change["id"], "approved")
        self.assertEqual(orch_auth.get_user("Ed Editor")["role"], "editor")
        user, reason = orch_auth.authenticate("Ed Editor", "Editor-Pass-88")
        self.assertEqual(reason, "ok")
        for kind, extra in (("change_role", {"role": "approver"}), ("disable", {}),
                            ("reset_password", {"password": "New-Pass-9999"})):
            pending = orch_auth.request_change("Bob Admin", kind, "Ed Editor", **extra)
            self.assertEqual(pending["status"], "pending")
            orch_auth.decide_change("Ann Admin", pending["id"], "approved")
        user = orch_auth.get_user("Ed Editor")
        self.assertEqual((user["role"], user["disabled"]), ("approver", True))
        rejected = orch_auth.request_change("Bob Admin", "enable", "Ed Editor")
        orch_auth.decide_change("Ann Admin", rejected["id"], "rejected")
        self.assertTrue(orch_auth.get_user("Ed Editor")["disabled"])
        events = [r["event"] for r in orch_auth.read_audit()]
        self.assertIn("account_change_requested", events)
        self.assertIn("account_change_applied", events)
        self.assertIn("account_change_rejected", events)
        self.assertTrue(any(r.get("bootstrap_exception") for r in orch_auth.read_audit()))

    def test_guards(self):
        with self.assertRaises(orch_auth.AuthError) as caught:
            orch_auth.request_change("Ann Admin", "disable", "Ann Admin")
        self.assertEqual(caught.exception.code, "not_on_self")
        with self.assertRaises(orch_auth.AuthError):
            orch_auth.request_change("Ann Admin", "create_user", "Weak", role="editor", password="short")
        orch_auth.request_change("Ann Admin", "create_user", "Ed Editor", role="editor",
                                 password="Editor-Pass-88")
        with self.assertRaises(orch_auth.AuthError) as caught:
            orch_auth.request_change("Ed Editor", "disable", "Ann Admin")
        self.assertEqual(caught.exception.code, "forbidden")
        orch_auth.request_change("Ann Admin", "create_user", "Bob Admin", role="admin",
                                 password="Second-Admin-77")
        pending = orch_auth.request_change("Bob Admin", "disable", "Ann Admin")
        # Review fix: the target of a change cannot decide it.
        with self.assertRaises(orch_auth.AuthError) as caught:
            orch_auth.decide_change("Ann Admin", pending["id"], "approved")
        self.assertEqual(caught.exception.code, "not_on_self")
        with self.assertRaises(orch_auth.AuthError):
            orch_auth.decide_change("Ann Admin", pending["id"], "rejected")
        with self.assertRaises(orch_auth.AuthError) as caught:
            orch_auth.request_change("Bob Admin", "change_role", "Bob Admin", role="editor")
        self.assertEqual(caught.exception.code, "not_on_self")


class AdminPageTests(DemoSandbox):
    def test_users_page_request_and_second_admin_approval(self):
        seed_users((("Zoe Admin", "admin"),))
        self.as_user("Admin One")
        self.client.post("/admin/users/request", data={
            "csrf_token": self.token, "kind": "create_user", "target": "New Editor",
            "role": "editor", "password": "New-Editor-123"})
        self.assertIsNone(orch_auth.get_user("New Editor"))
        html = self.client.get("/admin/users").get_data(as_text=True)
        self.assertIn("data-pending-changes", html)
        self.assertIn(ui_strings("zh-Hant")["adm_waiting_other_admin"], html)
        change_id = orch_auth.pending_changes()[0]["id"]
        self.assertEqual(self.client.post(f"/admin/changes/{change_id}/approved",
                                          data={}).status_code, 400)
        self.as_user("Zoe Admin")
        self.client.post(f"/admin/changes/{change_id}/approved", data={"csrf_token": self.token})
        self.assertEqual(orch_auth.get_user("New Editor")["role"], "editor")


class RetentionTests(DemoSandbox):
    def test_purge_keeps_audit_and_pending(self):
        uploads = self.tmp / "uploads" / "chat"
        (uploads / "old_batch").mkdir(parents=True)
        (uploads / "old_batch" / "a.txt").write_text("x")
        old = time.time() - 10 * 86400
        os.utime(uploads / "old_batch", (old, old))
        (uploads / "new_batch").mkdir()
        decided = self.create_one()
        pending = self.create_one(kind="market_insight")
        commerce_demo.decide(decided, "approved", "Ben Lee", 1, channel="brand_site_faq")
        draft_artifact = self.statuses()[decided]["ecom"]["versions"][0]["artifact_id"]
        audit_before = commerce_demo.audit_records()
        events_before = len(self.events())
        future = datetime.now(timezone.utc) + timedelta(days=400)
        with patch.object(chat_attachments, "CHAT_UPLOADS_ROOT", uploads.resolve()):
            record = orch_auth.purge("Admin One", now=future)
        self.assertEqual(record["drafts_removed"], 1)
        self.assertGreaterEqual(record["upload_batches_removed"], 1)
        self.assertNotIn(decided, self.demo_task_ids())
        self.assertIn(pending, self.demo_task_ids())
        self.assertIsNone(commerce_demo.read_artifact(draft_artifact))
        self.assertEqual(commerce_demo.audit_records(), audit_before)   # audit kept
        self.assertEqual(len(self.events()), events_before)
        self.assertTrue(record["audit_kept"])
        self.assertIn("retention_purge", [r["event"] for r in orch_auth.read_audit()])
        # The audit page still renders the decision record.
        self.as_user("Admin One")
        self.assertIn(audit_before[0]["audit_artifact_id"][:18],
                      self.client.get("/audit").get_data(as_text=True))

    def test_recent_decisions_not_purged_and_purge_page(self):
        decided = self.create_one()
        commerce_demo.decide(decided, "approved", "Ben Lee", 1, channel="brand_site_faq")
        self.as_user("Admin One")
        self.client.post("/admin/retention/purge", data={"csrf_token": self.token})
        self.assertIn(decided, self.demo_task_ids())
        html = self.client.get("/admin/retention").get_data(as_text=True)
        self.assertIn("data-purges", html)
        self.client.post("/admin/retention", data={"csrf_token": self.token,
                                                   "draft_retention_days": "30",
                                                   "upload_retention_days": "2",
                                                   "default_deadline_hours": "24"})
        self.assertEqual(orch_auth.settings()["draft_retention_days"], 30)
        self.assertEqual(chat_attachments.configured_upload_retention_seconds(), 2 * 86400)


class PermissionsListTests(DemoSandbox):
    def test_page_and_csv_export(self):
        orch_auth.set_module_approvers("Admin One", "lead_desk", ["Dan Ho"])
        self.as_user("Admin One")
        html = self.client.get("/admin/permissions").get_data(as_text=True)
        self.assertIn("權限清單", html)
        self.assertIn("data-permissions", html)
        response = self.client.get("/admin/permissions.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        text = response.get_data().decode("utf-8")
        self.assertTrue(text.startswith("\ufeff"))
        lines = text.lstrip("\ufeff").splitlines()
        self.assertEqual(lines[0], "帳戶,角色,狀態,可審批的模組,建立時間,最近登入")
        ben = next(line for line in lines if line.startswith("Ben Lee,"))
        self.assertNotIn(ui_strings("zh-Hant")["mod_lead_desk_title"], ben)
        dan = next(line for line in lines if line.startswith("Dan Ho,"))
        self.assertIn(ui_strings("zh-Hant")["mod_lead_desk_title"], dan)
        self.assertNotIn("password", text.lower())
        self.assertIn("permissions_exported", [r["event"] for r in orch_auth.read_audit()])
        self.as_user("Ben Lee")
        self.assertEqual(self.client.get("/admin/permissions.csv").status_code, 403)

    def test_english_export(self):
        self.as_user("Admin One")
        self.client.post("/locale", data={"csrf_token": self.token, "locale": "en", "next": "/"})
        text = self.client.get("/admin/permissions.csv").get_data().decode("utf-8")
        self.assertIn("Account,Role,Status,Can approve modules", text)


class CliTests(unittest.TestCase):
    def setUp(self):
        use_temp_auth(self, users=())

    def run_cli(self, *args, passwords=()):
        out, err = io.StringIO(), io.StringIO()
        answers = iter(passwords)
        with patch.object(orch_auth.getpass, "getpass", lambda prompt="": next(answers)), \
                redirect_stdout(out), redirect_stderr(err):
            code = orch_auth.main(list(args))
        return code, out.getvalue() + err.getvalue()

    def test_bootstrap_reads_password_via_getpass(self):
        code, out = self.run_cli("create-admin", "--username", "Fat Wong",
                                 passwords=("Admin-Pass-2026", "Admin-Pass-2026"))
        self.assertEqual(code, 0, out)
        self.assertNotIn("Admin-Pass-2026", out)
        self.assertEqual(orch_auth.get_user("fat wong")["role"], "admin")
        code, out = self.run_cli("create-admin", "--username", "Other",
                                 passwords=("Admin-Pass-2026", "Admin-Pass-2026"))
        self.assertEqual(code, 1)
        self.assertIn("already exist", out)
        code, out = self.run_cli("list-users")
        self.assertIn("Fat Wong\tadmin", out)
        self.assertEqual(orch_auth.read_audit()[-1]["event"], "bootstrap_admin")

    def test_bootstrap_mismatch_and_weak(self):
        code, _ = self.run_cli("create-admin", "--username", "Fat", passwords=("Admin-Pass-2026", "x"))
        self.assertEqual(code, 1)
        code, _ = self.run_cli("create-admin", "--username", "Fat", passwords=("short", "short"))
        self.assertEqual(code, 1)
        self.assertFalse(orch_auth.has_users())

    def test_unlock_and_break_glass(self):
        seed_users((("Fat Wong", "admin"),))
        with patch.dict(os.environ, {"ORCH_LOGIN_MAX_FAILURES": "1"}):
            orch_auth.authenticate("Fat Wong", "wrong-wrong-1")
        self.assertIsNotNone(orch_auth.get_user("Fat Wong")["locked_until_utc"])
        self.assertEqual(self.run_cli("unlock", "Fat Wong")[0], 0)
        self.assertIsNone(orch_auth.get_user("Fat Wong")["locked_until_utc"])
        code, _ = self.run_cli("reset-password", "Fat Wong", passwords=("Brand-New-Pass-1",) * 2)
        self.assertEqual(code, 0)
        self.assertEqual(orch_auth.authenticate("Fat Wong", "Brand-New-Pass-1")[1], "ok")
        self.assertIn("password_reset_break_glass", [r["event"] for r in orch_auth.read_audit()])

    def test_cli_non_demo_approve_still_works_without_accounts_logic(self):
        # mini_orch's CLI gate is unchanged for normal tasks (no requested_by).
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "q.json"
            status = Path(tmp) / "s.json"
            queue.write_text(json.dumps([{"id": "task_x", "title": "x", "command": ["true"],
                                          "priority": 1, "depends_on": [], "max_retries": 0,
                                          "requires_approval": True}]))
            result = mini_orch.decide_approval("task_x", "approved", "cli-user",
                                               queue_file=queue, status_file=status,
                                               events_file=Path(tmp) / "e.jsonl")
        self.assertTrue(result["ok"])


class I18nTests(unittest.TestCase):
    def test_parity_and_used_keys(self):
        keys = {code: set(ui_strings(code)) for code in SUPPORTED_LOCALES}
        self.assertEqual(keys["en"], keys["zh-Hant"])
        self.assertEqual(keys["en"], keys["zh-Hans"])
        source = ""
        for name in ("admin_ui.py", "commerce_ui.py", "orch_ui.py"):
            source += (PROJECT_ROOT / name).read_text(encoding="utf-8")
        used = set(re.findall(r"\bt\.((?:auth|gov|adm|perm|role|nav)_[a-z0-9_]+)", source))
        used |= set(re.findall(r"t\[\"((?:auth|gov|adm|perm)_[a-z0-9_]+)\"\]", source))
        for role in orch_auth.ROLES:
            used |= {f"role_{role}", f"role_{role}_desc"}
        for kind in orch_auth.CHANGE_KINDS:
            used.add(f"adm_kind_{kind}")
        used.add("auth_err_login_invalid")        # one generic login failure
        for code in ("expired", "revoked", "logged_out"):
            used.add(f"auth_notice_{code}")
        for block in ("role", "self", "not_assigned"):
            used.add(f"gov_block_{block}")
        audit_source = (PROJECT_ROOT / "orch_auth.py").read_text(encoding="utf-8")
        for event in set(re.findall(r"audit\(\s*\"([a-z_]*[a-z])\"", audit_source)):
            used.add(f"gov_ev_{event}")
        for code in set(re.findall(r"AuthError\(\"([a-z_]+)\"\)", audit_source)):
            used.add(f"auth_err_{code}")
        for outcome in ("requested", "applied", "rejected"):
            used.add(f"gov_ev_account_change_{outcome}")
        for code in SUPPORTED_LOCALES:
            missing = sorted(key for key in used if key not in keys[code])
            self.assertEqual(missing, [], code)

    def test_login_page_in_each_locale(self):
        use_temp_auth(self)
        client = app.test_client()
        client.get("/login")
        with client.session_transaction() as stored:
            token = stored["csrf_token"]
        for code in SUPPORTED_LOCALES:
            client.post("/locale", data={"csrf_token": token, "locale": code, "next": "/login"})
            html = client.post("/login", data={"csrf_token": token, "username": "x",
                                               "password": "y"}).get_data(as_text=True)
            t = ui_strings(code)
            self.assertIn(t["auth_login_title"], html)
            self.assertIn(t["auth_err_login_invalid"], html)

    def test_version(self):
        self.assertEqual(commerce_demo.DEMO_VERSION, "v0.20.1")
        self.assertEqual(orch_auth.AUTH_VERSION, "v0.20.1")
        self.assertIn("v0.20.0", (PROJECT_ROOT / "README.md").read_text(encoding="utf-8"))


class RepoHygieneTests(unittest.TestCase):
    def test_auth_state_gitignored(self):
        ignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        for entry in ("state/auth.json", "state/auth_audit.jsonl", "state/.auth"):
            self.assertIn(entry, ignore)


if __name__ == "__main__":
    unittest.main()
