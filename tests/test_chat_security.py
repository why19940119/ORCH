from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import json
import unittest

import chat_security


class ChatRedactionTests(unittest.TestCase):
    def test_openrouter_key_is_redacted(self):
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        text = f"Use this key: {secret}"

        result = chat_security.redact_sensitive_text(text)

        self.assertNotIn(secret, result["redacted_text"])
        self.assertIn(
            "[REDACTED_OPENROUTER_KEY]",
            result["redacted_text"],
        )
        self.assertIn(
            "openrouter_key",
            result["detection_categories"],
        )

    def test_bearer_token_is_redacted(self):
        secret = "Bearer abcdefghijklmnopqrstuvwxyz123456"
        text = f"Authorization: {secret}"

        result = chat_security.redact_sensitive_text(text)

        self.assertNotIn(secret, result["redacted_text"])
        self.assertIn(
            "Bearer [REDACTED_BEARER_TOKEN]",
            result["redacted_text"],
        )
        self.assertIn(
            "bearer_token",
            result["detection_categories"],
        )

    def test_jwt_is_redacted(self):
        secret = (
            "eyJhbGciOiJIUzI1NiJ9."
            "eyJzdWIiOiJvcmlnIn0."
            "abcdefghijklmnopqrstuvwxyz123456"
        )

        result = chat_security.redact_sensitive_text(
            f"JWT={secret}"
        )

        self.assertNotIn(secret, result["redacted_text"])
        self.assertIn(
            "[REDACTED_JWT]",
            result["redacted_text"],
        )
        self.assertIn(
            "jwt",
            result["detection_categories"],
        )

    def test_clean_text_is_unchanged(self):
        text = "Why is task_alpha blocked?"

        result = chat_security.redact_sensitive_text(text)

        self.assertEqual(
            result["redacted_text"],
            text,
        )
        self.assertEqual(
            result["detection_categories"],
            [],
        )

    def test_original_hash_uses_unredacted_text(self):
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        text = f"Key: {secret}"

        result = chat_security.redact_sensitive_text(text)

        expected_hash = (
            "sha256:"
            + hashlib.sha256(
                text.encode("utf-8")
            ).hexdigest()
        )

        self.assertEqual(
            result["original_sha256"],
            expected_hash,
        )
        self.assertNotIn(
            secret,
            result["redacted_text"],
        )


class ChatBudgetPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        self.original_state_dir = chat_security.STATE_DIR
        self.original_budget_file = chat_security.BUDGET_FILE
        self.original_usage_file = chat_security.USAGE_FILE
        self.original_lock_file = chat_security.LOCK_FILE

        chat_security.STATE_DIR = self.root / "state"
        chat_security.BUDGET_FILE = (
            chat_security.STATE_DIR / "chat_budget.json"
        )
        chat_security.USAGE_FILE = (
            chat_security.STATE_DIR / "chat_usage.jsonl"
        )
        chat_security.LOCK_FILE = (
            chat_security.STATE_DIR / ".chat_budget.lock"
        )

    def tearDown(self):
        chat_security.STATE_DIR = self.original_state_dir
        chat_security.BUDGET_FILE = self.original_budget_file
        chat_security.USAGE_FILE = self.original_usage_file
        chat_security.LOCK_FILE = self.original_lock_file
        self.temp_dir.cleanup()

    def test_budget_survives_reload(self):
        session_hash = "sha256:test-session"

        reservation_id = (
            chat_security.reserve_chat_budget(session_hash)
        )

        provider_result = {
            "provider": "openrouter",
            "response_model": (
                "mistralai/mistral-medium-3.1"
            ),
            "response_id": "test-response-001",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "cost": 0.00025,
            },
        }

        chat_security.settle_chat_budget(
            reservation_id,
            provider_result,
            session_hash,
        )

        reloaded_state = chat_security.load_budget_state()

        self.assertEqual(
            reloaded_state["daily_totals"]["request_count"],
            1,
        )

        self.assertEqual(
            reloaded_state["sessions"][session_hash][
                "total_tokens"
            ],
            150,
        )

        self.assertEqual(
            reloaded_state["sessions"][session_hash][
                "total_cost_usd"
            ],
            0.00025,
        )

        usage_records = [
            json.loads(line)
            for line in chat_security.USAGE_FILE.read_text(
                encoding="utf-8"
            ).splitlines()
        ]

        self.assertEqual(len(usage_records), 1)
        self.assertEqual(
            usage_records[0]["execution_authority"],
            "none",
        )


if __name__ == "__main__":
    unittest.main()


class ChatDynamicReservationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        self.original_state_dir = chat_security.STATE_DIR
        self.original_budget_file = chat_security.BUDGET_FILE
        self.original_usage_file = chat_security.USAGE_FILE
        self.original_lock_file = chat_security.LOCK_FILE

        chat_security.STATE_DIR = self.root / "state"
        chat_security.BUDGET_FILE = (
            chat_security.STATE_DIR / "chat_budget.json"
        )
        chat_security.USAGE_FILE = (
            chat_security.STATE_DIR / "chat_usage.jsonl"
        )
        chat_security.LOCK_FILE = (
            chat_security.STATE_DIR / ".chat_budget.lock"
        )

    def tearDown(self):
        chat_security.STATE_DIR = self.original_state_dir
        chat_security.BUDGET_FILE = self.original_budget_file
        chat_security.USAGE_FILE = self.original_usage_file
        chat_security.LOCK_FILE = self.original_lock_file
        self.temp_dir.cleanup()

    def test_dynamic_reservation_blocks_token_overrun(self):
        session_hash = "sha256:dynamic-session"

        reservation_id = (
            chat_security.reserve_chat_budget(
                session_hash,
                reserved_tokens=3500,
            )
        )

        provider_result = {
            "provider": "openrouter",
            "response_model": (
                "mistralai/mistral-medium-3.1"
            ),
            "response_id": "dynamic-test-response",
            "usage": {
                "prompt_tokens": 3000,
                "completion_tokens": 200,
                "total_tokens": 3200,
                "cost": 0.0004,
            },
        }

        chat_security.settle_chat_budget(
            reservation_id,
            provider_result,
            session_hash,
        )

        with self.assertRaises(
            chat_security.ChatBudgetError
        ):
            chat_security.reserve_chat_budget(
                session_hash,
                reserved_tokens=1200,
            )


class ChatTokenEstimatorTests(unittest.TestCase):
    def test_estimate_reserves_message_bytes_and_completion(self):
        from orch_chat import estimate_chat_request_tokens

        question = "Explain ORCH safety boundaries."
        context = {
            "tasks": [
                {
                    "id": "task_example",
                    "status": "done",
                }
            ]
        }

        estimated_tokens = estimate_chat_request_tokens(
            question=question,
            mode="orch_context",
            context=context,
            history=[],
        )

        self.assertGreaterEqual(
            estimated_tokens,
            528,
        )

        self.assertIsInstance(estimated_tokens, int)
