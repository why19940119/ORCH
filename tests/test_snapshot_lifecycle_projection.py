import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import snapshot_store


ROOT_TASK_ID = "task_advisory_root"
DEPENDENCY_TASK_ID = "task_dependency"


class SnapshotLifecycleProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        self.original_queue_file = snapshot_store.QUEUE_FILE
        self.original_status_file = snapshot_store.STATUS_FILE
        self.original_policy_file = (
            snapshot_store.POLICY_CONTRACTS_FILE
        )

        snapshot_store.QUEUE_FILE = (
            self.root / "task_queue.json"
        )
        snapshot_store.STATUS_FILE = (
            self.root / "task_status.json"
        )
        snapshot_store.POLICY_CONTRACTS_FILE = (
            self.root / "policy_contracts.json"
        )

        self.tasks = [
            {
                "id": DEPENDENCY_TASK_ID,
                "title": "Dependency",
                "command": ["python3", "dependency.py"],
                "priority": 1,
                "depends_on": [],
                "requires_approval": False,
                "requires_policies": [],
            },
            {
                "id": ROOT_TASK_ID,
                "title": "Advisory root",
                "command": ["python3", "root.py"],
                "priority": 2,
                "depends_on": [DEPENDENCY_TASK_ID],
                "requires_approval": True,
                "requires_policies": [],
            },
        ]

        self.statuses = {
            DEPENDENCY_TASK_ID: {
                "status": "done",
                "attempt": 1,
            },
            ROOT_TASK_ID: {
                "status": "todo",
                "attempt": 0,
            },
        }

        self.write_fixture()

    def tearDown(self):
        snapshot_store.QUEUE_FILE = self.original_queue_file
        snapshot_store.STATUS_FILE = self.original_status_file
        snapshot_store.POLICY_CONTRACTS_FILE = (
            self.original_policy_file
        )
        self.temp_dir.cleanup()

    def write_fixture(self):
        snapshot_store.QUEUE_FILE.write_text(
            json.dumps(self.tasks),
            encoding="utf-8",
        )

        snapshot_store.STATUS_FILE.write_text(
            json.dumps(self.statuses),
            encoding="utf-8",
        )

        snapshot_store.POLICY_CONTRACTS_FILE.write_text(
            json.dumps(
                {
                    "registry_version": "test",
                    "policies": {},
                }
            ),
            encoding="utf-8",
        )

    def build_advisory_snapshot(self):
        return snapshot_store.build_scoped_snapshot(
            requested_task_ids=[ROOT_TASK_ID],
            artifact_logical_names=[],
            policy_ids=[],
            ignore_runtime_state_for_task_ids=[
                ROOT_TASK_ID
            ],
        )

    def test_root_lifecycle_changes_do_not_stale_snapshot(self):
        snapshot = self.build_advisory_snapshot()

        self.statuses[ROOT_TASK_ID].update(
            {
                "status": "waiting_approval",
                "approval_status": "waiting_approval",
                "updated_at": "2026-08-22T00:00:00",
            }
        )
        self.write_fixture()

        waiting_validation = (
            snapshot_store.validate_scoped_snapshot(snapshot)
        )

        self.assertEqual(
            waiting_validation["status"],
            "valid",
        )

        self.statuses[ROOT_TASK_ID].update(
            {
                "status": "approved",
                "approval_status": "approved",
                "approved_at": "2026-08-22T00:01:00",
                "approved_by": "local_terminal_user",
            }
        )
        self.write_fixture()

        approved_validation = (
            snapshot_store.validate_scoped_snapshot(snapshot)
        )

        self.assertEqual(
            approved_validation["status"],
            "valid",
        )

    def test_root_semantic_state_change_stales_snapshot(self):
        snapshot = self.build_advisory_snapshot()

        self.statuses[ROOT_TASK_ID][
            "review_outcome"
        ] = "needs_attention"
        self.write_fixture()

        validation = snapshot_store.validate_scoped_snapshot(
            snapshot
        )

        self.assertEqual(validation["status"], "stale")
        self.assertIn(
            "task_scope_changed",
            validation["differences"],
        )

    def test_dependency_state_change_stales_snapshot(self):
        snapshot = self.build_advisory_snapshot()

        self.statuses[DEPENDENCY_TASK_ID][
            "status"
        ] = "failed"
        self.write_fixture()

        validation = snapshot_store.validate_scoped_snapshot(
            snapshot
        )

        self.assertEqual(validation["status"], "stale")
        self.assertIn(
            "task_scope_changed",
            validation["differences"],
        )


if __name__ == "__main__":
    unittest.main()
