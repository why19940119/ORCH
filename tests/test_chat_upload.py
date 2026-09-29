"""v0.18.0 chat upload wiring: multipart -> chat_attachments -> ask_orch."""

import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import chat_attachments
from orch_ui import CHAT_SESSIONS, app
from auth_testing import signed_in


def mock_result(answer="Read your file."):
    return {
        "provider": "openrouter",
        "requested_model": "m",
        "response_model": "m",
        "response_id": "chat-upload-1",
        "usage": {"total_tokens": 0, "cost": 0},
        "chat": {
            "answer": answer,
            "referenced_task_ids": [],
            "referenced_artifact_ids": [],
            "limitations": [],
            "execution_authority": "none",
        },
    }


class ChatUploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_chat_upload_"))
        self.patches = [
            patch.object(chat_attachments, "UPLOADS_ROOT", self.tmp.resolve()),
            patch.object(chat_attachments, "CHAT_UPLOADS_ROOT", (self.tmp / "chat").resolve()),
        ]
        for item in self.patches:
            item.start()
        app.config["TESTING"] = True
        CHAT_SESSIONS.clear()
        self.client = app.test_client()
        signed_in(self, self.client)  # v0.20.0: login required
        self.client.get("/chat")
        with self.client.session_transaction() as sess:
            self.token = sess["csrf_token"]

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, files, question="Summarise this", **extra):
        data = {
            "csrf_token": self.token,
            "mode": "general",
            "question": question,
            "format": "json",
            "attachments": files,
        }
        data.update(extra)
        return self.client.post(
            "/chat",
            data=data,
            content_type="multipart/form-data",
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
        )

    @patch("orch_ui.publish_chat_audit_artifact")
    @patch("orch_ui.record_chat_usage")
    @patch("orch_ui.ask_orch")
    def test_txt_attachment_reaches_ask_orch(self, ask, usage, audit):
        audit.return_value = {"artifact_id": "artifact_chat_audit_upload"}
        ask.return_value = mock_result()
        response = self.post([(io.BytesIO(b"hello sample notes"), "notes.txt", "text/plain")])
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertTrue(payload["ok"])

        ask.assert_called_once()
        attachments = ask.call_args.kwargs["attachments"]
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["name"], "notes.txt")
        self.assertEqual(attachments[0]["kind"], "document")
        self.assertIn("hello sample notes", attachments[0]["extracted_text"])

        meta = payload["user"]["attachments"]
        self.assertEqual(meta[0]["name"], "notes.txt")
        for forbidden in ("path", "stored_name", "extracted_text", "data_base64"):
            self.assertNotIn(forbidden, meta[0])

        history = next(iter(CHAT_SESSIONS.values()))
        self.assertEqual(history[0]["attachments"][0]["sha256"], attachments[0]["sha256"])
        self.assertNotIn("path", history[0]["attachments"][0])
        self.assertEqual(audit.call_args.kwargs["attachments"][0]["name"], "notes.txt")

    @patch("orch_ui.publish_chat_audit_artifact")
    @patch("orch_ui.record_chat_usage")
    @patch("orch_ui.ask_orch")
    def test_file_without_question_is_enough(self, ask, usage, audit):
        audit.return_value = {"artifact_id": "a"}
        ask.return_value = mock_result()
        response = self.post([(io.BytesIO(b"x"), "a.md", "text/markdown")], question="")
        self.assertEqual(response.status_code, 200)
        ask.assert_called_once()

    @patch("orch_ui.ask_orch")
    def test_bad_extension_is_rejected_as_user_error(self, ask):
        response = self.post([(io.BytesIO(b"MZ"), "tool.exe", "application/octet-stream")])
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["error"])
        ask.assert_not_called()

    @patch("orch_ui.ask_orch")
    def test_more_than_three_files_rejected(self, ask):
        files = [(io.BytesIO(b"x"), f"f{i}.txt", "text/plain") for i in range(4)]
        response = self.post(files)
        self.assertEqual(response.status_code, 400)
        ask.assert_not_called()

    @patch("orch_ui.ask_orch")
    def test_empty_question_and_no_file_rejected(self, ask):
        response = self.post([], question="")
        self.assertEqual(response.status_code, 400)
        ask.assert_not_called()

    def test_multipart_requires_csrf(self):
        response = self.client.post(
            "/chat",
            data={"mode": "general", "question": "x",
                  "attachments": [(io.BytesIO(b"x"), "a.txt", "text/plain")]},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)

    def test_composer_markup_and_js_contract(self):
        html = self.client.get("/chat").get_data(as_text=True)
        self.assertIn('enctype="multipart/form-data"', html)
        self.assertIn('name="attachments"', html)
        self.assertIn("multiple", html)
        self.assertIn(chat_attachments.accept_attribute(), html)
        self.assertIn('id="chat-attach-button"', html)
        self.assertIn('id="chat-attach-chips"', html)
        self.assertNotRegex(html, r'<textarea[^>]*id="question"[^>]*\brequired\b')
        self.assertRegex(html, r'<textarea\s+id="question"(?![^>]*required)')
        self.assertIn('formData.append("attachments"', html)
        self.assertIn("data-attach-chip", html)
        self.assertIn('data-max-files="3"', html)


if __name__ == "__main__":
    unittest.main()
