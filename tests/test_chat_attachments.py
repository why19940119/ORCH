import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chat_attachments import (
    AttachmentError,
    CHAT_UPLOADS_ROOT,
    assert_within_chat_uploads,
    extract_text_from_bytes,
    process_uploaded_files,
    validate_upload_meta,
)
from orch_chat import (
    DEFAULT_VISION_MODEL,
    ChatProviderError,
    ask_orch,
    get_chat_config,
)


class FakeStorage:
    def __init__(self, filename, data, mimetype):
        self.filename = filename
        self.mimetype = mimetype
        self._data = data

    def read(self, size=-1):
        if size is None or size < 0:
            return self._data
        return self._data[:size]


class ChatAttachmentUnitTests(unittest.TestCase):
    def test_reject_exe_extension(self):
        with self.assertRaises(AttachmentError):
            validate_upload_meta("malware.exe", "application/octet-stream", 12)

    def test_reject_bad_mime(self):
        with self.assertRaises(AttachmentError):
            validate_upload_meta("notes.txt", "application/x-msdownload", 12)

    def test_path_escape_rejected(self):
        with self.assertRaises(AttachmentError):
            assert_within_chat_uploads(Path("/tmp/evil.txt"))

    def test_txt_extraction_smoke(self):
        text = extract_text_from_bytes(
            b"hello orch attachments",
            "text/plain",
            "note.txt",
        )
        self.assertIn("hello orch attachments", text)

    def test_process_txt_upload(self):
        storage = FakeStorage(
            "hello.txt",
            b"payload text",
            "text/plain",
        )
        records = process_uploaded_files([storage], session_key="testsess")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kind"], "document")
        self.assertIn("payload text", records[0]["extracted_text"])
        stored = Path(records[0]["path"])
        self.assertTrue(stored.is_file())
        assert_within_chat_uploads(stored)

    def test_vision_default_model_constant(self):
        self.assertEqual(
            DEFAULT_VISION_MODEL,
            "google/gemini-2.0-flash-001",
        )

    def test_image_path_clear_error_without_api_key(self):
        env = os.environ.copy()
        env.pop("OPENROUTER_API_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ChatProviderError) as ctx:
                ask_orch(
                    question="What is in this image?",
                    mode="general",
                    context={},
                    history=[],
                    attachments=[
                        {
                            "name": "shot.png",
                            "kind": "image",
                            "mime": "image/png",
                            "data_base64": "aaaa",
                        }
                    ],
                )
        self.assertIn("API key", str(ctx.exception))

    def test_get_chat_config_vision_default(self):
        with patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "sk-test-key-value",
            },
            clear=False,
        ):
            os.environ.pop("OPENROUTER_VISION_MODEL", None)
            cfg = get_chat_config(use_vision=True)
            self.assertEqual(cfg["model"], DEFAULT_VISION_MODEL)


if __name__ == "__main__":
    unittest.main()
