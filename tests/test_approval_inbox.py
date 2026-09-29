import unittest
from unittest.mock import patch

from approval_inbox import classify_risk, inbox_item, is_waiting_approval
from orch_ui import app
from auth_testing import signed_in


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
        # v0.20.0: the approver is the logged-in account.
        signed_in(self, self.client, "Amy Chan", "approver")

    def test_approve_without_csrf_is_rejected(self):
        response = self.client.post("/inbox/task_demo/approve")
        self.assertEqual(response.status_code, 400)

    def test_approve_routes_through_gate_with_named_operator(self):
        self.client.get("/inbox")
        with self.client.session_transaction() as stored:
            token = stored["csrf_token"]

        task_id = "task_ecom_content_0123456789"
        with patch(
            "commerce_ui.commerce_demo.decide",
            return_value={"task_id": task_id, "decision": "approved"},
        ) as mocked:
            response = self.client.post(
                f"/inbox/{task_id}/approve",
                data={
                    "csrf_token": token,
                    "operator": "Mallory Typed",  # ignored since v0.20.0
                    "version": "1",
                    "channel": "email",
                    "note": "ok",
                },
            )

        self.assertEqual(response.status_code, 302)
        mocked.assert_called_once()
        args = mocked.call_args.args
        kwargs = mocked.call_args.kwargs
        self.assertEqual(args[0], task_id)
        self.assertEqual(args[1], "approved")
        self.assertEqual(args[2], "Amy Chan")
        self.assertEqual(kwargs.get("note"), "ok")
        self.assertEqual(kwargs.get("channel"), "email")

    def test_non_demo_task_cannot_be_approved_from_ui(self):
        self.client.get("/inbox")
        with self.client.session_transaction() as stored:
            token = stored["csrf_token"]
        response = self.client.post(
            "/inbox/task_approval_demo_006/approve",
            data={"csrf_token": token, "operator": "Amy Chan"},
        )
        self.assertEqual(response.status_code, 404)
