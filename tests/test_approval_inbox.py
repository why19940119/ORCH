import unittest
from unittest.mock import patch

from approval_inbox import classify_risk, inbox_item, is_waiting_approval
from orch_ui import app


class ApprovalInboxHelperTests(unittest.TestCase):
    def test_classify_price_and_refund(self):
        self.assertIn(
            "price",
            classify_risk({"title": "Update SKU price list"}),
        )
        self.assertIn(
            "refund",
            classify_risk({"title": "Handle \u9000\u6b3e request"}),
        )

    def test_waiting_requires_approval_flag(self):
        task = {"requires_approval": True}
        self.assertTrue(
            is_waiting_approval(task, {"status": "waiting_approval"})
        )
        self.assertFalse(
            is_waiting_approval(
                {"requires_approval": False},
                {"status": "waiting_approval"},
            )
        )

    def test_inbox_item_marks_high_risk(self):
        item = inbox_item(
            {
                "id": "task_promo_001",
                "title": "Publish discount campaign",
                "command": ["python3", "worker_local_note.py"],
                "state": {"status": "waiting_approval"},
                "requires_approval": True,
            }
        )
        self.assertTrue(item["high_risk"])
        self.assertTrue(item["can_approve"])


class ApprovalInboxRouteTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_approve_without_csrf_is_rejected(self):
        response = self.client.post("/inbox/task_demo/approve")
        self.assertEqual(response.status_code, 400)

    def test_approve_uses_mini_orch_and_does_not_run_queue(self):
        self.client.get("/inbox")
        with self.client.session_transaction() as stored:
            token = stored["csrf_token"]

        with patch(
            "orch_ui.approve_task",
            return_value={"ok": True, "task_id": "task_demo"},
        ) as mocked:
            response = self.client.post(
                "/inbox/task_demo/approve",
                data={"csrf_token": token, "note": "ok", "next": "/inbox"},
            )

        self.assertEqual(response.status_code, 302)
        mocked.assert_called_once()
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs.get("approved_by"), "orch_ui_operator")
        self.assertEqual(kwargs.get("note"), "ok")
