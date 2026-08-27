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




class ChatUsageLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        self.original_state_dir = chat_security.STATE_DIR
        self.original_usage_file = chat_security.USAGE_FILE

        chat_security.STATE_DIR = self.root / "state"
        chat_security.USAGE_FILE = (
            chat_security.STATE_DIR / "chat_usage.jsonl"
        )

    def tearDown(self):
        chat_security.STATE_DIR = self.original_state_dir
        chat_security.USAGE_FILE = self.original_usage_file
        self.temp_dir.cleanup()

    def test_usage_ledger_records_provider_metadata_only(self):
        provider_result = {
            "provider": "openrouter",
            "response_model": (
                "mistralai/mistral-medium-3.1"
            ),
            "response_id": "usage-test-response-001",
            "usage": {
                "prompt_tokens": 123,
                "completion_tokens": 45,
                "total_tokens": 168,
                "cost": 0.001,
            },
        }

        chat_security.record_chat_usage(
            provider_result=provider_result,
            session_id_sha256="sha256:usage-test-session",
        )

        records = [
            json.loads(line)
            for line in chat_security.USAGE_FILE.read_text(
                encoding="utf-8"
            ).splitlines()
        ]

        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0]["record_type"],
            "orch_chat_usage",
        )
        self.assertEqual(
            records[0]["usage"]["total_tokens"],
            168,
        )
        self.assertEqual(
            records[0]["execution_authority"],
            "none",
        )
        self.assertNotIn("question", records[0])
        self.assertNotIn("answer", records[0])
