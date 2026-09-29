"""v0.18.0 cross-border e-commerce demo tests.

All state/artifact paths are redirected to a temp directory so the
real task_queue.json, state/ and artifacts/ are never touched.
"""

import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import artifact_store
import commerce_demo
import commerce_ui
import mini_orch
from orch_ui import PROJECT_ROOT, app
from ui_i18n import SUPPORTED_LOCALES, ui_strings


DEMO_PAGES = [
    "/sales",
    "/content",
    "/knowledge",
    "/leads",
    "/campaigns",
    "/market",
    "/inbox",
    "/audit",
]


class DemoSandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_ecom_test_"))
        (self.tmp / "state").mkdir()
        shutil.copy(PROJECT_ROOT / "task_queue.json", self.tmp / "task_queue.json")
        self.original_task_ids = [
            task["id"]
            for task in json.loads(
                (self.tmp / "task_queue.json").read_text(encoding="utf-8")
            )
        ]
        art = self.tmp / "artifacts"
        self.patches = [
            patch.object(commerce_demo, "QUEUE_FILE", self.tmp / "task_queue.json"),
            patch.object(commerce_demo, "STATUS_FILE", self.tmp / "state" / "task_status.json"),
            patch.object(commerce_demo, "EVENTS_FILE", self.tmp / "state" / "events.jsonl"),
            patch.object(commerce_demo, "LOCK_FILE", self.tmp / "state" / ".lock"),
            patch.object(artifact_store, "ARTIFACT_ROOT", art),
            patch.object(artifact_store, "STAGING_DIR", art / "staging"),
            patch.object(artifact_store, "OBJECTS_DIR", art / "objects" / "sha256"),
            patch.object(artifact_store, "MANIFESTS_DIR", art / "manifests"),
            patch.object(artifact_store, "LATEST_DIR", art / "latest"),
            patch.dict(os.environ, {"ORCH_DEMO_FORCE_MOCK": "1"}),
            patch.object(commerce_ui, "DRAFT_MIN_INTERVAL_SECONDS", 0),
        ]
        for item in self.patches:
            item.start()
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.client.get("/inbox")
        with self.client.session_transaction() as stored:
            self.token = stored["csrf_token"]

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers ---------------------------------------------------------
    def queue(self):
        return json.loads((self.tmp / "task_queue.json").read_text(encoding="utf-8"))

    def statuses(self):
        path = self.tmp / "state" / "task_status.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def events(self):
        path = self.tmp / "state" / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def demo_task_ids(self):
        return [t["id"] for t in self.queue() if t["id"].startswith("task_ecom_")]

    def draft(self, **fields):
        data = {
            "csrf_token": self.token,
            "kind": "content",
            "sku": "SAMPLE-001",
            "content_type": "product_page",
            "language": "en",
            "operator": "Amy Chan",
        }
        data.update(fields)
        return self.client.post("/demo/draft", data=data)

    def create_one(self, **fields):
        response = self.draft(**fields)
        self.assertEqual(response.status_code, 302)
        ids = self.demo_task_ids()
        self.assertTrue(ids, "draft task was not created")
        return ids[-1]


class DemoPageTests(DemoSandbox):
    def test_each_demo_page_returns_200_with_sample_banner(self):
        for path in DEMO_PAGES:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            html = response.get_data(as_text=True)
            self.assertIn("data-sample-banner", html, path)
            self.assertIn("示範數據", html, path)

    def test_sample_banner_in_every_locale(self):
        for code in SUPPORTED_LOCALES:
            self.client.post(
                "/locale",
                data={"csrf_token": self.token, "locale": code, "next": "/market"},
            )
            html = self.client.get("/market").get_data(as_text=True)
            self.assertIn(ui_strings(code)["demo_sample_badge"], html)
            self.assertIn(ui_strings(code)["nav_inbox"], html)

    def test_nav_links_present_on_existing_console_pages(self):
        html = self.client.get("/").get_data(as_text=True)
        for path in DEMO_PAGES:
            self.assertIn(f'href="{path}"', html)
        self.assertIn('href="/chat"', html)

    def test_sample_data_file_is_labelled_and_sized(self):
        data = json.loads(
            (PROJECT_ROOT / "demo" / "sample_data.json").read_text(encoding="utf-8")
        )
        self.assertIn("SAMPLE", data["_meta"]["label"])
        self.assertIn("Not real", data["_meta"]["description"])
        self.assertGreaterEqual(len(data["skus"]), 30)
        self.assertTrue(all(s["sku"].startswith("SAMPLE-") for s in data["skus"]))
        self.assertTrue(all(s.get("sample") for s in data["skus"]))
        self.assertIn("Synthetic", data["kpi"]["note"])

    def test_lead_scoring_is_deterministic_and_ranked(self):
        rows = commerce_demo.ranked_inquiries()
        scores = [row["triage"]["score"] for row in rows]
        self.assertEqual(scores, sorted(scores, reverse=True))
        refund = commerce_demo.classify_inquiry({"text": "I want a refund", "channel": "email"})
        self.assertEqual(refund["category"], "refund")
        self.assertTrue(refund["high_risk"])


class DemoCsrfTests(DemoSandbox):
    def test_draft_requires_csrf(self):
        response = self.draft(csrf_token="")
        self.assertEqual(response.status_code, 400)
        response = self.draft(csrf_token="wrong-token-value")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.demo_task_ids(), [])

    def test_approve_reject_revise_require_csrf(self):
        task_id = self.create_one()
        for action in ("approve", "reject", "revise"):
            response = self.client.post(
                f"/inbox/{task_id}/{action}",
                data={
                    "operator": "Ben Lee",
                    "version": "1",
                    "channel": "online_store_product_page",
                    "note": "x",
                    "body": "x",
                },
            )
            self.assertEqual(response.status_code, 400, action)
        state = self.statuses()[task_id]
        self.assertEqual(state["approval_status"], "waiting_approval")
        self.assertEqual(state["ecom"]["current_version"], 1)


class DemoApprovalFlowTests(DemoSandbox):
    def test_draft_starts_pending_through_orch_gate(self):
        task_id = self.create_one()
        task = next(t for t in self.queue() if t["id"] == task_id)
        self.assertTrue(task["requires_approval"])
        self.assertEqual(task["command"][1], "worker_ecom_publish_record.py")
        self.assertEqual(task["requires_policies"][0]["id"], "artifact-exists")

        state = self.statuses()[task_id]
        self.assertEqual(state["status"], "waiting_approval")
        self.assertEqual(state["approval_status"], "waiting_approval")
        self.assertNotIn("approved_by", state)

        events = [e["event"] for e in self.events() if e["task_id"] == task_id]
        self.assertEqual(events, ["ecom_draft_created", "task_waiting_approval"])

        payload = commerce_demo.read_artifact(state["ecom"]["versions"][0]["artifact_id"])
        self.assertEqual(payload["version"], 1)
        self.assertFalse(payload["published"])
        self.assertTrue(payload["sample_data"])
        self.assertEqual(payload["provenance"]["provider"], "mock")
        self.assertEqual(commerce_demo.audit_records(), [])

        # Existing ORCH tasks are untouched.
        self.assertEqual(
            [t["id"] for t in self.queue() if not t["id"].startswith("task_ecom_")],
            self.original_task_ids,
        )

        # The draft is visible in the inbox as pending (no auto-approve).
        html = self.client.get("/inbox").get_data(as_text=True)
        self.assertIn(f'id="{task_id}"', html)
        self.assertIn(f"/inbox/{task_id}/approve", html)

    def test_approve_writes_audit_through_existing_machinery(self):
        task_id = self.create_one()
        response = self.client.post(
            f"/inbox/{task_id}/approve",
            data={
                "csrf_token": self.token,
                "operator": "Ben Lee",
                "version": "1",
                "channel": "online_store_product_page",
                "note": "Checked specs",
            },
        )
        self.assertEqual(response.status_code, 302)

        state = self.statuses()[task_id]
        self.assertEqual(state["status"], "approved")
        self.assertEqual(state["approval_status"], "approved")
        self.assertEqual(state["approved_by"], "Ben Lee")
        self.assertEqual(state["approved_version"], 1)

        records = commerce_demo.audit_records()
        self.assertEqual(len(records), 1)
        audit = records[0]
        self.assertEqual(audit["decision"], "approved")
        self.assertEqual(audit["task_id"], task_id)
        self.assertEqual(audit["version"], 1)
        self.assertEqual(audit["operator"], "Amy Chan")
        self.assertEqual(audit["approver"], "Ben Lee")
        self.assertEqual(audit["publish_channel"], "online_store_product_page")
        self.assertEqual(audit["publish_mode"], "simulated_record_only")
        self.assertFalse(audit["external_call"])
        self.assertIn("SAMPLE-001", audit["source"]["refs"])
        self.assertTrue(audit["decided_at_utc"])
        self.assertEqual(
            audit["draft_artifact_id"], state["ecom"]["versions"][0]["artifact_id"]
        )

        manifest = json.loads(
            (artifact_store.MANIFESTS_DIR / f"{audit['audit_artifact_id']}.json").read_text()
        )
        self.assertEqual(manifest["logical_name"], "ecom_audit")
        self.assertEqual(manifest["producer_task_id"], task_id)
        self.assertTrue(manifest["immutable"])

        names = [e["event"] for e in self.events() if e["task_id"] == task_id]
        self.assertIn("task_approved", names)
        self.assertIn("ecom_publish_recorded", names)
        approved_event = next(e for e in self.events() if e["event"] == "task_approved")
        self.assertEqual(approved_event["operator"], "Ben Lee")

        html = self.client.get("/audit").get_data(as_text=True)
        self.assertIn("Ben Lee", html)
        self.assertIn(audit["audit_artifact_id"][:18], html)

    def test_approve_requires_named_operator_and_valid_channel(self):
        task_id = self.create_one()
        for data in (
            {"operator": "", "channel": "online_store_product_page"},
            {"operator": "Ben Lee", "channel": "tiktok_ads"},
        ):
            self.client.post(
                f"/inbox/{task_id}/approve",
                data={"csrf_token": self.token, "version": "1", **data},
            )
        self.assertEqual(self.statuses()[task_id]["approval_status"], "waiting_approval")
        self.assertEqual(commerce_demo.audit_records(), [])

    def test_reject_needs_reason_and_is_never_dispatched(self):
        task_id = self.create_one(kind="lead_reply", inquiry_id="INQ-S-004")
        self.client.post(
            f"/inbox/{task_id}/reject",
            data={"csrf_token": self.token, "operator": "Ben Lee", "version": "1", "note": ""},
        )
        self.assertEqual(self.statuses()[task_id]["approval_status"], "waiting_approval")

        self.client.post(
            f"/inbox/{task_id}/reject",
            data={"csrf_token": self.token, "operator": "Ben Lee", "version": "1",
                  "note": "Refund wording needs manager"},
        )
        state = self.statuses()[task_id]
        self.assertEqual(state["status"], "rejected")
        self.assertEqual(state["rejected_by"], "Ben Lee")
        audit = commerce_demo.audit_records()[0]
        self.assertEqual(audit["decision"], "rejected")
        self.assertIsNone(audit["publish_channel"])

        # The mini_orch gate refuses to dispatch a rejected task.
        task = next(t for t in self.queue() if t["id"] == task_id)
        statuses = self.statuses()
        with patch.object(mini_orch, "STATUS_FILE", self.tmp / "state" / "task_status.json"), \
             patch.object(mini_orch, "EVENTS_FILE", self.tmp / "state" / "events.jsonl"), \
             patch.object(mini_orch, "evaluate_required_policies", return_value=(True, [])), \
             patch.object(mini_orch.subprocess, "run") as run:
            mini_orch.run_task(task, statuses)
            run.assert_not_called()
        self.assertEqual(statuses[task_id]["status"], "rejected")

    def test_revision_creates_new_version_and_stale_approval_is_refused(self):
        task_id = self.create_one()
        self.client.post(
            f"/inbox/{task_id}/revise",
            data={"csrf_token": self.token, "operator": "Cara Wong", "version": "1",
                  "body": "Edited product copy. Price: [HUMAN TO CONFIRM]"},
        )
        state = self.statuses()[task_id]
        self.assertEqual(state["ecom"]["current_version"], 2)
        v1, v2 = state["ecom"]["versions"]
        manifest = json.loads(
            (artifact_store.MANIFESTS_DIR / f"{v2['artifact_id']}.json").read_text()
        )
        self.assertEqual(manifest["parent_artifact_id"], v1["artifact_id"])

        # Approving the stale v1 is refused.
        self.client.post(
            f"/inbox/{task_id}/approve",
            data={"csrf_token": self.token, "operator": "Ben Lee", "version": "1",
                  "channel": "marketplace_listing"},
        )
        self.assertEqual(self.statuses()[task_id]["approval_status"], "waiting_approval")

        self.client.post(
            f"/inbox/{task_id}/approve",
            data={"csrf_token": self.token, "operator": "Ben Lee", "version": "2",
                  "channel": "marketplace_listing"},
        )
        audit = commerce_demo.audit_records()[0]
        self.assertEqual(audit["version"], 2)
        self.assertEqual(audit["operator"], "Cara Wong")
        self.assertTrue(audit["source"]["edited_by_human"])

        # A second approval of the same task is refused.
        with self.assertRaises(commerce_demo.DemoError):
            commerce_demo.decide(task_id, "approved", "Ben Lee", 2, channel="marketplace_listing")

    def test_every_module_kind_lands_pending(self):
        forms = [
            {"kind": "sales_next_step", "lead_id": "LEAD-S-001"},
            {"kind": "lead_reply", "inquiry_id": "INQ-S-002", "language": "auto"},
            {"kind": "campaign", "sku": "SAMPLE-013", "audience_id": "AUD-S-02", "objective": "sales"},
            {"kind": "market_insight"},
            {"kind": "kb_update", "kb_id": "KB-RET-01", "proposal": "Extend to 30 days"},
            {"kind": "content", "sku": "SAMPLE-005", "content_type": "faq", "language": "zh-Hant"},
        ]
        for form in forms:
            self.assertEqual(self.draft(**form).status_code, 302, form)
        statuses = self.statuses()
        ids = self.demo_task_ids()
        self.assertEqual(len(ids), len(forms))
        for task_id in ids:
            self.assertEqual(statuses[task_id]["approval_status"], "waiting_approval")

    def test_draft_requires_operator(self):
        self.draft(operator="")
        self.assertEqual(self.demo_task_ids(), [])

    def test_draft_rate_limit(self):
        with patch.object(commerce_ui, "DRAFT_MIN_INTERVAL_SECONDS", 60):
            self.draft()
            self.draft()
        self.assertEqual(len(self.demo_task_ids()), 1)

    def test_worker_confirms_only_approved_audit(self):
        import worker_ecom_publish_record as worker

        task_id = self.create_one()
        with patch.object(sys, "argv", ["w", task_id]):
            self.assertEqual(worker.main(), 1)
        commerce_demo.decide(task_id, "approved", "Ben Lee", 1, channel="brand_site_faq")
        with patch.object(sys, "argv", ["w", task_id]):
            self.assertEqual(worker.main(), 0)

    def test_reset_removes_only_demo_tasks(self):
        self.create_one()
        removed = commerce_demo.reset_demo_tasks()
        self.assertEqual(removed, 1)
        self.assertEqual([t["id"] for t in self.queue()], self.original_task_ids)


class DemoProviderTests(DemoSandbox):
    def _result(self, answer="Drafted by model."):
        return {
            "provider": "openrouter",
            "requested_model": "m",
            "response_model": "m",
            "response_id": "gen-1",
            "usage": {},
            "chat": {"answer": answer, "referenced_task_ids": [],
                     "referenced_artifact_ids": [], "limitations": [],
                     "execution_authority": "none"},
        }

    def test_openrouter_path_one_call_per_action(self):
        with patch.dict(os.environ, {"ORCH_DEMO_FORCE_MOCK": "", "OPENROUTER_API_KEY": "sk-test-not-real"}), \
             patch.object(commerce_demo, "ask_orch", return_value=self._result()) as ask, \
             patch.object(commerce_demo, "record_chat_usage") as usage:
            task_id = self.create_one()
        ask.assert_called_once()
        self.assertEqual(ask.call_args.kwargs["mode"], "general")
        self.assertLessEqual(len(ask.call_args.kwargs["question"]), 800)
        usage.assert_called_once()
        state = self.statuses()[task_id]
        payload = commerce_demo.read_artifact(state["ecom"]["versions"][0]["artifact_id"])
        self.assertEqual(payload["body"], "Drafted by model.")
        self.assertEqual(payload["provenance"]["provider"], "openrouter")
        self.assertEqual(state["approval_status"], "waiting_approval")

    def test_provider_error_falls_back_to_mock_without_retry(self):
        from orch_chat import ChatProviderError

        with patch.dict(os.environ, {"ORCH_DEMO_FORCE_MOCK": "", "OPENROUTER_API_KEY": "sk-test-not-real"}), \
             patch.object(commerce_demo, "ask_orch", side_effect=ChatProviderError("down")) as ask:
            task_id = self.create_one()
        ask.assert_called_once()
        state = self.statuses()[task_id]
        payload = commerce_demo.read_artifact(state["ecom"]["versions"][0]["artifact_id"])
        self.assertEqual(payload["provenance"]["provider"], "mock")
        self.assertIn("down", payload["provenance"]["fallback_reason"])

    def test_mock_is_deterministic(self):
        source = commerce_demo.build_source("content", {"sku": "SAMPLE-002", "content_type": "faq"})
        self.assertEqual(
            commerce_demo.mock_draft("content", source, "en"),
            commerce_demo.mock_draft("content", source, "en"),
        )


class DemoI18nTests(unittest.TestCase):
    def test_locale_key_parity(self):
        keys = {code: set(ui_strings(code)) for code in SUPPORTED_LOCALES}
        self.assertEqual(keys["en"], keys["zh-Hant"])
        self.assertEqual(keys["en"], keys["zh-Hans"])

    def test_all_template_keys_exist(self):
        source = (PROJECT_ROOT / "commerce_ui.py").read_text(encoding="utf-8")
        source += (PROJECT_ROOT / "orch_ui.py").read_text(encoding="utf-8")
        used = set(re.findall(r"\bt\.((?:demo|nav|mod|chat|err)_[a-z0-9_]+)", source))
        for module in ("sales_hub", "content_studio", "knowledge_base", "lead_desk",
                       "campaign_engine", "market_dashboard", "approval_inbox", "audit_log"):
            for suffix in ("title", "role", "ai"):
                used.add(f"mod_{module}_{suffix}")
        for kind, spec in commerce_demo.DRAFT_KINDS.items():
            used.add(f"kind_{kind}")
            used.update(f"ch_{channel}" for channel in spec["channels"])
        for code in SUPPORTED_LOCALES:
            strings = ui_strings(code)
            missing = sorted(key for key in used if key not in strings)
            self.assertEqual(missing, [], code)

    def test_zh_hant_stays_default_and_uses_client_names(self):
        strings = ui_strings(None)
        self.assertEqual(strings["nav_sales"], "Sales Hub")
        self.assertEqual(strings["mod_approval_inbox_title"], "Approval Inbox")
        self.assertIn("示範數據", strings["demo_sample_badge"])


if __name__ == "__main__":
    unittest.main()
