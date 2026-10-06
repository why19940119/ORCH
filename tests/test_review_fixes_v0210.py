"""v0.21.0 review fixes (PR #7): audit commit, setup token, proxy/host
check, Docker persistence and scripts. Migration / permission fixes are in
test_orch_db_v0210.py."""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

import artifact_store
import commerce_demo
import mini_orch
import orch_db
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


if __name__ == "__main__":
    unittest.main()
