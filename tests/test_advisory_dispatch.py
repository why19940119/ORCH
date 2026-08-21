import unittest
from unittest.mock import patch

from advisory_dispatch import run_advisory_preflight


TASK = {
    "id": "task_real_advisory_dispatch_test",
    "title": "Test approval-gated advisory dispatch",
    "command": [
        "python3",
        "worker_example.py",
    ],
    "priority": 999,
    "depends_on": [],
    "max_retries": 0,
    "requires_approval": True,
    "requires_policies": [],
    "advisory_preflight": {
        "artifact_logical_names": [],
        "policy_ids": [],
    },
}

SNAPSHOT = {
    "snapshot_version": "1.0",
    "snapshot_id": "snapshot_test_001",
    "scope": {
        "requested_task_ids": [
            "task_real_advisory_dispatch_test"
        ],
        "artifact_logical_names": [],
        "policy_ids": [],
    },
    "task_fingerprints": {},
    "artifact_fingerprints": {},
    "policy_contract_fingerprint": "sha256:test",
    "snapshot_fingerprint": "sha256:test",
}

ADVISORY_RESULT = {
    "provider": "openrouter",
    "requested_model": "mistralai/mistral-medium-3.1",
    "response_model": "mistralai/mistral-medium-3.1",
    "response_id": "test-response-001",
    "usage": {
        "total_tokens": 0,
        "cost": 0,
    },
    "advisory": {
        "summary": "Safe advisory-only result.",
        "risks": [],
        "recommended_action": "advisory_only",
        "confidence": 1.0,
    },
}

ARTIFACT = {
    "artifact_id": "artifact_test",
    "manifest_path": "artifacts/manifests/test.json",
    "latest_path": "artifacts/latest/test.json",
    "object_path": "artifacts/objects/sha256/test",
    "content_sha256": "sha256:test",
    "byte_size": 0,
}


class AdvisoryDispatchTests(unittest.TestCase):
    def test_task_without_config_does_not_call_ai(self):
        task = {
            key: value
            for key, value in TASK.items()
            if key != "advisory_preflight"
        }

        with patch(
            "advisory_dispatch.request_advisory"
        ) as request_advisory:
            result = run_advisory_preflight(task)

        self.assertEqual(
            result["status"],
            "not_required",
        )
        request_advisory.assert_not_called()

    def test_preflight_requires_human_approval(self):
        task = {
            **TASK,
            "requires_approval": False,
        }

        with patch(
            "advisory_dispatch.request_advisory"
        ) as request_advisory:
            result = run_advisory_preflight(task)

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "must require human approval",
            result["reason"],
        )
        request_advisory.assert_not_called()

    @patch("advisory_dispatch.publish_staged_artifact")
    @patch("advisory_dispatch.stage_json")
    @patch("advisory_dispatch.validate_scoped_snapshot")
    @patch("advisory_dispatch.request_advisory")
    @patch("advisory_dispatch.build_scoped_snapshot")
    def test_valid_preflight_is_allowed(
        self,
        build_snapshot,
        request_advisory,
        validate_snapshot,
        stage_json,
        publish_artifact,
    ):
        build_snapshot.return_value = SNAPSHOT
        request_advisory.return_value = ADVISORY_RESULT
        validate_snapshot.return_value = {
            "status": "valid",
        }
        stage_json.return_value = "staging/test.json"
        publish_artifact.return_value = ARTIFACT

        result = run_advisory_preflight(TASK)

        self.assertEqual(result["status"], "allowed")
        self.assertEqual(
            result["execution_authority"],
            "none",
        )
        request_advisory.assert_called_once_with(TASK)
        publish_artifact.assert_called_once()

    @patch("advisory_dispatch.publish_staged_artifact")
    @patch("advisory_dispatch.stage_json")
    @patch("advisory_dispatch.validate_scoped_snapshot")
    @patch("advisory_dispatch.request_advisory")
    @patch("advisory_dispatch.build_scoped_snapshot")
    def test_stale_snapshot_blocks_dispatch(
        self,
        build_snapshot,
        request_advisory,
        validate_snapshot,
        stage_json,
        publish_artifact,
    ):
        build_snapshot.return_value = SNAPSHOT
        request_advisory.return_value = ADVISORY_RESULT
        validate_snapshot.return_value = {
            "status": "stale",
            "reason": "task_scope_changed",
        }
        stage_json.return_value = "staging/test.json"
        publish_artifact.return_value = ARTIFACT

        result = run_advisory_preflight(TASK)

        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "snapshot became stale",
            result["reason"],
        )
        request_advisory.assert_called_once_with(TASK)
        publish_artifact.assert_called_once()

    def test_existing_human_block_does_not_call_ai(self):
        existing = {
            "status": "human_review_required",
            "snapshot": SNAPSHOT,
            "preflight": {
                "status": "human_review_required",
            },
        }

        with patch(
            "advisory_dispatch.request_advisory"
        ) as request_advisory:
            result = run_advisory_preflight(
                TASK,
                existing=existing,
            )

        self.assertNotEqual(result["status"], "allowed")
        request_advisory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
