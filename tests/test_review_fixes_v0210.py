"""v0.21.0 review fixes (PR #7): audit commit, setup token, proxy/host
check, Docker persistence and scripts. Migration / permission fixes are in
test_orch_db_v0210.py."""

import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask, jsonify, request

import artifact_store
import commerce_demo
import deploy_config
import mini_orch
import orch_db
import orch_ui
from test_commerce_demo import DemoSandbox


class AuditCommitTests(DemoSandbox):
    """Review fix 3: no audit record for a decision that did not commit."""

    def approve(self, task_id):
        return self.client.post(f"/inbox/{task_id}/approve", data={
            "csrf_token": self.token, "operator": "Ben Lee", "version": "1",
            "channel": "online_store_product_page", "note": "ok"})

    def audit_manifests(self):
        return sorted(Path(artifact_store.MANIFESTS_DIR).glob("artifact_ecom_audit_*.json"))

    def test_failure_after_artifact_write_leaves_no_audit_and_draft_pending(self):
        task_id = self.create_one()
        self.as_user("Ben Lee")
        real_write_event = mini_orch.write_event

        def failing_write_event(event, *args, **kwargs):
            if event == commerce_demo.AUDIT_COMMIT_EVENT:
                # the audit artifact is already on disk at this point
                self.assertEqual(len(self.audit_manifests()), 1)
                raise RuntimeError("disk full")
            return real_write_event(event, *args, **kwargs)

        with patch.object(mini_orch, "write_event", failing_write_event):
            with self.assertRaises(RuntimeError):
                commerce_demo.decide(task_id, "approved", "Ben Lee", 1,
                                     channel="online_store_product_page")
        self.assertEqual(self.audit_manifests(), [])           # artifact removed
        self.assertFalse((Path(artifact_store.LATEST_DIR) / "ecom_audit.json").exists())
        state = self.statuses()[task_id]
        self.assertEqual(state["approval_status"], "waiting_approval")   # rolled back
        self.assertNotIn("decision", state["ecom"])
        self.assertEqual(commerce_demo.audit_records(), [])
        html = self.client.get("/audit").get_data(as_text=True)
        self.assertNotIn("artifact_ecom_audit_", html)
        # the draft can still be approved normally afterwards
        self.assertEqual(self.approve(task_id).status_code, 302)
        records = commerce_demo.audit_records()
        self.assertEqual(len(records), 1)
        events = [e for e in self.events() if e["event"] == commerce_demo.AUDIT_COMMIT_EVENT]
        self.assertEqual([e["audit_artifact_id"] for e in events],
                         [records[0]["audit_artifact_id"]])

    def test_leftover_artifact_is_never_listed(self):
        """Even if cleanup cannot remove the file (or COMMIT itself fails),
        /audit only lists artifacts confirmed by committed DB state."""
        task_id = self.create_one()
        self.as_user("Ben Lee")
        with patch.object(commerce_demo, "_discard_audit_artifact", lambda *a: None), \
                patch.object(commerce_demo, "_save", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                commerce_demo.decide(task_id, "approved", "Ben Lee", 1,
                                     channel="online_store_product_page")
        self.assertEqual(len(self.audit_manifests()), 1)      # orphan left on disk
        self.assertEqual(commerce_demo.audit_records(), [])
        self.assertNotIn(self.audit_manifests()[0].stem,
                         self.client.get("/audit").get_data(as_text=True))

    def test_pre_fix_orphan_for_pending_draft_is_hidden(self):
        """Orphans written by the old code (no commit_event marker) for a
        draft that is still pending are hidden too."""
        task_id = self.create_one()
        commerce_demo._publish_json(commerce_demo.AUDIT_LOGICAL_NAME, {
            "artifact_type": "ecom_audit", "task_id": task_id, "decision": "approved",
            "approver": "Ben Lee", "decided_at_utc": "2026-09-29T00:00:00+00:00"}, task_id)
        self.assertEqual(len(self.audit_manifests()), 1)
        self.assertEqual(commerce_demo.audit_records(), [])

    def test_committed_rejection_and_approval_are_listed(self):
        first, second = self.create_one(), self.create_one(sku="SAMPLE-002")
        commerce_demo.decide(first, "rejected", "Ben Lee", 1, note="wrong price")
        commerce_demo.decide(second, "approved", "Ben Lee", 1,
                             channel="online_store_product_page")
        decisions = sorted(r["decision"] for r in commerce_demo.audit_records())
        self.assertEqual(decisions, ["approved", "rejected"])
        # still listed after retention purged the decided drafts' status
        orch_db.save(commerce_demo.STATUS_FILE, {})
        self.assertEqual(len(commerce_demo.audit_records()), 2)


class ProxyHeaderTests(unittest.TestCase):
    """Review fix 6: X-Forwarded-* only from a trusted proxy peer."""

    def client(self, hops, trusted=None):
        inner = Flask("proxy_test")
        inner.config["TRUSTED_HOSTS"] = ["127.0.0.1", "localhost", "orch.example.com"]

        @inner.get("/who")
        def who():
            return jsonify(host=request.host, scheme=request.scheme, ip=request.remote_addr)

        inner.wsgi_app = deploy_config.ProxyHeadersMiddleware(inner.wsgi_app, hops=hops,
                                                              trusted=trusted)
        return inner.test_client()

    SPOOF = {"X-Forwarded-Host": "orch.example.com", "X-Forwarded-For": "198.51.100.1",
             "X-Forwarded-Proto": "https"}

    def get(self, client, peer, host, headers=None):
        return client.get("/who", headers={"Host": host, **(headers or {})},
                          environ_base={"REMOTE_ADDR": peer})

    def test_spoofed_forwarded_host_from_untrusted_client_cannot_pass_host_check(self):
        client = self.client(hops=1)                          # default trust: 127.0.0.1, ::1
        self.assertEqual(self.get(client, "203.0.113.7", "evil.example", self.SPOOF).status_code, 400)
        ok = self.get(client, "203.0.113.7", "127.0.0.1:5050", self.SPOOF).get_json()
        self.assertEqual(ok, {"host": "127.0.0.1:5050", "scheme": "http", "ip": "203.0.113.7"})

    def test_trusted_proxy_headers_are_used(self):
        client = self.client(hops=1)
        for peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            body = self.get(client, peer, "127.0.0.1:5050", self.SPOOF).get_json()
            self.assertEqual(body, {"host": "orch.example.com", "scheme": "https",
                                    "ip": "198.51.100.1"}, peer)
        docker = self.client(hops=1, trusted=["172.16.0.0/12"])
        self.assertEqual(self.get(docker, "172.18.0.1", "x:5050", self.SPOOF).get_json()["host"],
                         "orch.example.com")
        self.assertEqual(self.get(docker, "127.0.0.1", "evil.example", self.SPOOF).status_code, 400)

    def test_without_proxy_fix_headers_are_always_ignored(self):
        client = self.client(hops=0)
        self.assertEqual(self.get(client, "127.0.0.1", "evil.example", self.SPOOF).status_code, 400)

    def test_real_app_strips_forwarded_host(self):
        self.assertIsInstance(orch_ui.app.wsgi_app, deploy_config.ProxyHeadersMiddleware)
        response = orch_ui.app.test_client().get(
            "/healthz", headers={"Host": "evil.example", "X-Forwarded-Host": "localhost"})
        self.assertEqual(response.status_code, 400)

    def test_trusted_proxy_env_parsing(self):
        with patch.dict(os.environ, {"ORCH_TRUSTED_PROXY": ""}):
            self.assertEqual(deploy_config.trusted_proxies(), ["127.0.0.1", "::1"])
        with patch.dict(os.environ, {"ORCH_TRUSTED_PROXY": " 10.0.0.5, 172.16.0.0/12 ,"}):
            self.assertEqual(deploy_config.trusted_proxies(), ["10.0.0.5", "172.16.0.0/12"])
            self.assertTrue(deploy_config.peer_is_trusted_proxy("172.31.255.1"))
            self.assertFalse(deploy_config.peer_is_trusted_proxy("8.8.8.8"))
            self.assertFalse(deploy_config.peer_is_trusted_proxy("not-an-ip"))

    def test_waitress_passes_headers_to_the_app(self):
        import serve
        calls = {}
        fake = types.SimpleNamespace(serve=lambda app, **kw: calls.update(kw))
        with patch.dict(sys.modules, {"waitress": fake}), \
                patch.object(orch_ui, "startup", lambda: None):
            serve.main()
        self.assertIs(calls["clear_untrusted_proxy_headers"], False)

    def test_docs(self):
        text = (orch_ui.PROJECT_ROOT / "docs" / "反向代理與HTTPS.md").read_text(encoding="utf-8")
        self.assertIn("ORCH_TRUSTED_PROXY", text)


if __name__ == "__main__":
    unittest.main()
