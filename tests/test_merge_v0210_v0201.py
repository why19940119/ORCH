"""v0.21.0 + v0.20.1 merge: the PR #6 account-change sweep runs on the
SQLite store - auto-close audit rows are written to the auth_audit table
inside the same transaction as the store change (never to a JSON file)."""

import unittest
from pathlib import Path
from unittest.mock import patch

import orch_auth
import orch_db
from auth_testing import use_temp_auth


class SweepOnSqliteTests(unittest.TestCase):
    def setUp(self):
        self.dir = use_temp_auth(self, users=(("Ann Admin", "admin"), ("Bob Admin", "admin"),
                                              ("Cy Admin", "admin"), ("Ed Editor", "editor")))
        store = orch_auth.load_store()
        store["bootstrap"] = {"complete": True}
        orch_auth._save_store(store)

    def demote(self, name):
        store = orch_auth.load_store()
        store["users"][orch_auth._key(name)]["role"] = "editor"
        orch_auth._save_store(store)

    def test_demoted_requester_closed_by_sweep_audited_in_db(self):
        stale = orch_auth.request_change("Ann Admin", "change_role", "Ed Editor", role="approver")
        other = orch_auth.request_change("Bob Admin", "disable", "Ed Editor")
        self.demote("Ann Admin")
        # skipped in the duplicate check: a new identical request is allowed
        again = orch_auth.request_change("Cy Admin", "change_role", "Ed Editor", role="approver")
        orch_auth.decide_change("Cy Admin", other["id"], "approved")
        changes = {c["id"]: c for c in orch_auth.pending_changes()}
        self.assertEqual((changes[stale["id"]]["status"], changes[stale["id"]]["auto_close_reason"]),
                         ("auto_closed", "requester_not_admin"))
        self.assertNotEqual(changes[again["id"]]["status"], "pending")   # target now disabled
        rows = [r for r in orch_db.read_log(orch_auth.audit_file())
                if r["event"] == "account_change_auto_closed"]
        self.assertIn(stale["id"], [r["change_id"] for r in rows])
        self.assertTrue(all(r["audit_version"] == "v0.21.1" for r in rows))
        # SQLite only: no legacy JSON audit/store files are written
        self.assertFalse((Path(self.dir) / "auth_audit.jsonl").exists())
        self.assertFalse((Path(self.dir) / "auth.json").exists())
        self.assertTrue((Path(self.dir) / "orch.db").is_file())

    def test_audit_failure_rolls_back_the_decision(self):
        stale = orch_auth.request_change("Ann Admin", "change_role", "Ed Editor", role="approver")
        other = orch_auth.request_change("Bob Admin", "disable", "Ed Editor")
        self.demote("Ann Admin")
        before = len(orch_db.read_log(orch_auth.audit_file()))
        real = orch_auth.audit

        def failing(event, actor, **details):
            if event == "account_change_auto_closed":
                raise RuntimeError("audit write failed")
            return real(event, actor, **details)

        with patch.object(orch_auth, "audit", failing), self.assertRaises(RuntimeError):
            orch_auth.decide_change("Cy Admin", other["id"], "approved")
        changes = {c["id"]: c for c in orch_auth.pending_changes()}
        self.assertEqual(changes[other["id"]]["status"], "pending")      # rolled back
        self.assertEqual(changes[stale["id"]]["status"], "pending")
        self.assertFalse(orch_auth.get_user("Ed Editor").get("disabled"))
        self.assertEqual(len(orch_db.read_log(orch_auth.audit_file())), before)

    def test_auto_close_on_approve_is_one_transaction(self):
        change = orch_auth.request_change("Ann Admin", "disable", "Ed Editor")
        self.demote("Ann Admin")
        with patch.object(orch_auth, "audit", side_effect=RuntimeError("x")), \
                self.assertRaises(RuntimeError):
            orch_auth.decide_change("Bob Admin", change["id"], "approved")
        self.assertEqual({c["id"]: c for c in orch_auth.pending_changes()}[change["id"]]["status"],
                         "pending")
        result = orch_auth.decide_change("Bob Admin", change["id"], "approved")
        self.assertEqual(result["auto_close_reason"], "requester_not_admin")
        rows = [r for r in orch_auth.read_audit() if r["event"] == "account_change_auto_closed"]
        self.assertEqual((len(rows), rows[0]["actor"]), (1, "Bob Admin"))


if __name__ == "__main__":
    unittest.main()
