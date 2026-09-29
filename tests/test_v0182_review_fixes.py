"""v0.18.2 review fixes (items 1-9 + zh-Hant).

Every test uses temp dirs / mocks: no network, no real state writes.
"""

import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import artifact_store
import chat_attachments
import commerce_demo
import commerce_import
import commerce_ui
import mini_orch
import orch_db
import orch_chat
import orch_ui
from auth_testing import demo_signed_in, sign_in, signed_in
from chat_attachments import AttachmentError, process_uploaded_files
from orch_chat import ChatProviderError
from orch_ui import CHAT_SESSIONS, app
from ui_i18n import SUPPORTED_LOCALES, ui_strings


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32
BAD_PDF = b"%PDF-1.7\n1 0 obj <<>> endobj\ntrailer <</Root 1 0 R>>\n%%EOF"


def make_pdf(pages):
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(100, 100)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class FakeStorage:
    def __init__(self, filename, data, mimetype):
        self.filename = filename
        self.mimetype = mimetype
        self._data = data

    def read(self, size=-1):
        return self._data if size is None or size < 0 else self._data[:size]


class UploadsSandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_v0182_up_"))
        self.chat_root = (self.tmp / "chat").resolve()
        self.patches = [
            patch.object(chat_attachments, "UPLOADS_ROOT", self.tmp.resolve()),
            patch.object(chat_attachments, "CHAT_UPLOADS_ROOT", self.chat_root),
        ]
        for item in self.patches:
            item.start()
        app.config["TESTING"] = True
        CHAT_SESSIONS.clear()
        self.client = app.test_client()
        signed_in(self, self.client)  # v0.20.0: login required
        self.client.get("/chat")
        with self.client.session_transaction() as stored:
            self.token = stored["csrf_token"]

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stored_files(self):
        if not self.chat_root.exists():
            return []
        return [p for p in self.chat_root.rglob("*") if p.is_file()]

    def post(self, files, question="Summarise", locale=None):
        if locale:
            with self.client.session_transaction() as stored:
                stored["locale"] = locale
        return self.client.post(
            "/chat",
            data={
                "csrf_token": self.token,
                "mode": "general",
                "question": question,
                "format": "json",
                "attachments": files,
            },
            content_type="multipart/form-data",
            headers={"Accept": "application/json",
                     "X-Requested-With": "XMLHttpRequest"},
        )


# 1. malformed PDF -------------------------------------------------------
class MalformedPdfTests(UploadsSandbox):
    def test_bad_pdf_raises_attachment_error(self):
        with self.assertRaises(AttachmentError) as ctx:
            chat_attachments.extract_text_from_bytes(BAD_PDF, "application/pdf", "x.pdf")
        self.assertEqual(ctx.exception.code, "pdf_unreadable")

    @patch("orch_ui.ask_orch")
    def test_bad_pdf_upload_is_friendly_400_and_leaves_no_file(self, ask):
        response = self.post([(io.BytesIO(BAD_PDF), "broken.pdf", "application/pdf")])
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        t = ui_strings("zh-Hant")
        self.assertIn(t["err_att_pdf_unreadable"], payload["error"])
        self.assertTrue(payload["error"].startswith(t["err_chat_attachment"]))
        self.assertEqual(self.stored_files(), [])
        ask.assert_not_called()

    def test_page_count_is_capped(self):
        with patch.object(chat_attachments, "MAX_PDF_PAGES", 3):
            text = chat_attachments.extract_text_from_bytes(
                make_pdf(5), "application/pdf", "big.pdf"
            )
        self.assertIn("first 3 of 5 pages", text)

    def test_valid_pdf_still_works(self):
        records = process_uploaded_files(
            [FakeStorage("ok.pdf", make_pdf(1), "application/pdf")], "s1"
        )
        self.assertEqual(records[0]["kind"], "document")


# 2. MAX_CONTENT_LENGTH + 413 --------------------------------------------
class RequestSizeTests(UploadsSandbox):
    def test_limit_is_configured(self):
        self.assertEqual(app.config["MAX_CONTENT_LENGTH"], 16 * 1024 * 1024)

    @patch("orch_ui.ask_orch")
    def test_oversize_json_request_gets_localized_413(self, ask):
        with patch.dict(app.config, {"MAX_CONTENT_LENGTH": 1024}):
            response = self.post([(io.BytesIO(b"a" * 4096), "big.txt", "text/plain")])
        self.assertEqual(response.status_code, 413)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        self.assertIn("16MB", payload["error"])
        self.assertIn("上載內容太大", payload["error"])
        ask.assert_not_called()
        self.assertEqual(self.stored_files(), [])

    def test_oversize_form_post_gets_html_413_in_english(self):
        with self.client.session_transaction() as stored:
            stored["locale"] = "en"
        with patch.dict(app.config, {"MAX_CONTENT_LENGTH": 1024}):
            response = self.client.post(
                "/chat",
                data={"csrf_token": self.token, "question": "x" * 4096, "mode": "general"},
            )
        self.assertEqual(response.status_code, 413)
        self.assertIn("The upload is too large", response.get_data(as_text=True))


# 3. batch validation + cleanup + retention ------------------------------
class BatchAndRetentionTests(UploadsSandbox):
    def test_invalid_second_file_writes_nothing(self):
        with self.assertRaises(AttachmentError):
            process_uploaded_files(
                [
                    FakeStorage("good.txt", b"hello", "text/plain"),
                    FakeStorage("fake.png", b"not a png", "image/png"),
                ],
                "s1",
            )
        self.assertEqual(self.stored_files(), [])
        self.assertEqual(list(self.chat_root.glob("*")) if self.chat_root.exists() else [], [])

    def test_write_failure_removes_batch_dir(self):
        real_write = Path.write_bytes
        calls = {"n": 0}

        def flaky(path, data):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
            return real_write(path, data)

        with patch.object(Path, "write_bytes", flaky):
            with self.assertRaises(AttachmentError) as ctx:
                process_uploaded_files(
                    [FakeStorage("a.txt", b"a", "text/plain"),
                     FakeStorage("b.txt", b"b", "text/plain")],
                    "s1",
                )
        self.assertEqual(ctx.exception.code, "storage_failed")
        self.assertEqual(self.stored_files(), [])
        self.assertEqual([p for p in self.chat_root.iterdir()], [])

    def test_retention_sweep_removes_only_old_batches(self):
        self.chat_root.mkdir(parents=True)
        old = self.chat_root / "old_batch"
        new = self.chat_root / "new_batch"
        for folder in (old, new):
            folder.mkdir()
            (folder / "f.txt").write_text("x")
        past = time.time() - chat_attachments.UPLOAD_RETENTION_SECONDS - 60
        os.utime(old, (past, past))
        removed = chat_attachments.sweep_old_uploads()
        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_upload_triggers_sweep(self):
        with patch.object(chat_attachments, "sweep_old_uploads") as sweep:
            process_uploaded_files([FakeStorage("a.txt", b"a", "text/plain")], "s1")
        sweep.assert_called_once()


# 8. magic bytes ---------------------------------------------------------
class MagicBytesTests(UploadsSandbox):
    def test_valid_signatures_pass(self):
        for name, data, mime in (
            ("a.png", PNG, "image/png"),
            ("a.jpg", JPG, "image/jpeg"),
            ("a.jpeg", JPG, "image/jpeg"),
            ("a.gif", GIF, "image/gif"),
            ("a.webp", WEBP, "image/webp"),
        ):
            records = process_uploaded_files([FakeStorage(name, data, mime)], "s1")
            self.assertEqual(records[0]["kind"], "image", name)

    def test_mismatched_content_is_rejected(self):
        for name, data, mime in (
            ("a.png", JPG, "image/png"),
            ("a.jpg", PNG, "image/jpeg"),
            ("a.gif", b"GIF00a....", "image/gif"),
            ("a.webp", b"RIFF1234WAVE....", "image/webp"),
            ("a.pdf", b"hello, not a pdf", "application/pdf"),
        ):
            with self.assertRaises(AttachmentError) as ctx:
                process_uploaded_files([FakeStorage(name, data, mime)], "s1")
            self.assertEqual(ctx.exception.code, "magic_mismatch", name)
        self.assertEqual(self.stored_files(), [])

    def test_text_must_be_utf8_without_nul(self):
        for data in (b"\xff\xfe\xfa bad", b"abc\x00def"):
            with self.assertRaises(AttachmentError) as ctx:
                process_uploaded_files([FakeStorage("n.txt", data, "text/plain")], "s1")
            self.assertEqual(ctx.exception.code, "text_not_utf8")
        records = process_uploaded_files(
            [FakeStorage("n.md", "\ufeff# 標題".encode("utf-8"), "text/markdown")], "s1"
        )
        self.assertIn("標題", records[0]["extracted_text"])


# zh: localized upload / chat errors -------------------------------------
class LocalizedErrorTests(UploadsSandbox):
    def test_every_attachment_code_has_strings(self):
        source = Path(chat_attachments.__file__).read_text(encoding="utf-8")
        codes = set(re.findall(r'code="(\w+)"', source))
        self.assertIn("pdf_unreadable", codes)
        for code in SUPPORTED_LOCALES:
            strings = ui_strings(code)
            for item in codes:
                self.assertIn(f"err_att_{item}", strings, (code, item))

    def test_every_chat_code_has_strings(self):
        source = Path(orch_chat.__file__).read_text(encoding="utf-8")
        codes = set(re.findall(r'code="(\w+)"', source))
        self.assertIn("no_api_key", codes)
        for code in SUPPORTED_LOCALES:
            strings = ui_strings(code)
            for item in codes:
                self.assertIn(f"err_chatcode_{item}", strings, (code, item))

    def test_missing_api_key_is_localized_in_zh_hant(self):
        env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            response = self.post([], question="你好")
        self.assertEqual(response.status_code, 400)
        error = response.get_json()["error"]
        self.assertEqual(error, ui_strings("zh-Hant")["err_chatcode_no_api_key"])
        self.assertNotIn("OpenRouter API key", error)

    def test_attachment_errors_in_each_locale(self):
        for locale in SUPPORTED_LOCALES:
            response = self.post(
                [(io.BytesIO(b"MZ"), "tool.exe", "application/octet-stream")],
                locale=locale,
            )
            error = response.get_json()["error"]
            t = ui_strings(locale)
            self.assertTrue(error.startswith(t["err_chat_attachment"]), locale)
            self.assertIn(".exe", error)
            if locale != "en":
                self.assertNotIn("File type not allowed", error)

    def test_http_error_code_formats_status(self):
        t = ui_strings("zh-Hans")
        message = orch_ui.localized_chat_error(
            ChatProviderError("x", code="http", status=429), t
        )
        self.assertIn("429", message)
        self.assertEqual(
            orch_ui.localized_chat_error(RuntimeError("raw"), t),
            t["err_chatcode_failed"],
        )


# 4-7 demo queue, lock, CLI refuse, audit ordering ------------------------
class DemoStateSandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_v0182_demo_"))
        (self.tmp / "state").mkdir()
        self.main_queue = self.tmp / "task_queue.json"
        self.main_queue.write_text(json.dumps([
            {"id": "task_main_001", "title": "Main task", "command": ["true"],
             "priority": 1, "depends_on": [], "max_retries": 1,
             "requires_approval": True, "requires_policies": []},
        ]), encoding="utf-8")
        self.main_bytes = self.main_queue.read_bytes()
        self.demo_queue = self.tmp / "state" / "ecom_demo_queue.json"
        self.status = self.tmp / "state" / "task_status.json"
        self.events = self.tmp / "state" / "events.jsonl"
        self.lock = self.tmp / "state" / ".ecom_demo.lock"
        art = self.tmp / "artifacts"
        self.patches = [
            patch.object(commerce_demo, "QUEUE_FILE", self.demo_queue),
            patch.object(commerce_demo, "MAIN_QUEUE_FILE", self.main_queue),
            patch.object(commerce_demo, "STATUS_FILE", self.status),
            patch.object(commerce_demo, "EVENTS_FILE", self.events),
            patch.object(commerce_demo, "LOCK_FILE", self.lock),
            patch.object(commerce_import, "IMPORT_STATE_FILE", self.tmp / "state" / "ecom_import.json"),
            patch.object(commerce_import, "LOCK_FILE", self.lock),
            patch.object(mini_orch, "QUEUE_FILE", self.main_queue),
            patch.object(mini_orch, "DEMO_QUEUE_FILE", self.demo_queue),
            patch.object(mini_orch, "STATUS_FILE", self.status),
            patch.object(mini_orch, "EVENTS_FILE", self.events),
            patch.object(mini_orch, "LOCK_FILE", self.lock),
            patch.object(mini_orch, "STATE_DIR", self.tmp / "state"),
            patch.object(orch_ui, "QUEUE_FILE", self.main_queue),
            patch.object(orch_ui, "STATUS_FILE", self.status),
            patch.object(orch_ui, "EVENTS_FILE", self.events),
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
        demo_signed_in(self, self.client)  # v0.20.0: login required

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def new_draft(self, operator="Amy Chan"):
        with redirect_stdout(io.StringIO()):
            return commerce_demo.create_draft(
                "content", {"sku": "SAMPLE-001", "content_type": "product_page"},
                operator, language="zh-Hant",
            )["task_id"]

    def statuses(self):
        return orch_db.load(self.status, {})


class DemoQueueTests(DemoStateSandbox):
    def test_draft_goes_to_demo_queue_not_tracked_queue(self):
        task_id = self.new_draft()
        self.assertEqual(self.main_queue.read_bytes(), self.main_bytes)
        demo = orch_db.load(self.demo_queue, [])
        self.assertEqual([t["id"] for t in demo], [task_id])

    def test_demo_queue_is_gitignored(self):
        gitignore = (Path(orch_ui.PROJECT_ROOT) / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("state/ecom_demo_queue.json", gitignore)

    def test_demo_tasks_show_on_tasks_page_and_dashboard(self):
        task_id = self.new_draft()
        tasks_html = self.client.get("/tasks").get_data(as_text=True)
        self.assertIn(task_id, tasks_html)
        self.assertIn("task_main_001", tasks_html)
        self.assertIn("[示範] 內容工作室：", tasks_html)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get(f"/tasks/{task_id}").status_code, 200)
        sign_in(self.client, "Ben Lee")  # v0.20.0: approvers see decide forms
        inbox = self.client.get("/inbox").get_data(as_text=True)
        self.assertIn(f"/inbox/{task_id}/approve", inbox)

    def test_mini_orch_merges_queues_and_dedupes(self):
        task_id = self.new_draft()
        legacy = orch_db.load(self.demo_queue, [])[0]
        main = json.loads(self.main_queue.read_text(encoding="utf-8")) + [legacy]
        self.main_queue.write_text(json.dumps(main), encoding="utf-8")
        ids = [t["id"] for t in mini_orch.load_all_tasks()]
        self.assertEqual(sorted(ids), sorted(["task_main_001", task_id]))
        self.assertEqual(
            sorted(t["id"] for t in orch_ui.load_tasks()),
            sorted(["task_main_001", task_id]),
        )

    def test_approval_flow_and_run_queue_reach_demo_task(self):
        task_id = self.new_draft()
        with redirect_stdout(io.StringIO()):
            commerce_demo.decide(task_id, "approved", "Ben Lee", 1, channel="brand_site_faq")
        executed = []

        def fake_run(command, **kwargs):
            executed.append(command)

            class Done:
                returncode = 0
                stdout = "{}"
                stderr = ""
            return Done()

        with patch.object(mini_orch.subprocess, "run", fake_run), \
                patch.object(mini_orch, "evaluate_required_policies",
                             return_value=(True, [])), \
                redirect_stdout(io.StringIO()):
            mini_orch.run_queue()
        self.assertTrue(any(task_id in " ".join(c) for c in executed))

    def test_import_legacy_copies_without_touching_main_queue(self):
        task_id = self.new_draft()
        task = orch_db.load(self.demo_queue, [])[0]
        orch_db.delete(self.demo_queue)
        main = json.loads(self.main_queue.read_text(encoding="utf-8")) + [task]
        self.main_queue.write_text(json.dumps(main), encoding="utf-8")
        before = self.main_queue.read_bytes()
        self.assertEqual(commerce_demo.import_legacy_demo_tasks(), [task_id])
        self.assertEqual(commerce_demo.import_legacy_demo_tasks(), [])
        self.assertEqual(self.main_queue.read_bytes(), before)
        demo = orch_db.load(self.demo_queue, [])
        self.assertEqual([t["id"] for t in demo], [task_id])


class RunQueueLockTests(DemoStateSandbox):
    def test_run_queue_uses_the_shared_lock(self):
        used = []
        real_lock = mini_orch.state_lock

        @contextmanager
        def spy(lock_file=None):
            used.append(Path(lock_file) if lock_file else mini_orch.LOCK_FILE)
            with real_lock(lock_file):
                yield

        with patch.object(mini_orch, "state_lock", spy), \
                patch.object(mini_orch, "run_task"), \
                redirect_stdout(io.StringIO()):
            mini_orch.run_queue()
        self.assertTrue(used)
        self.assertTrue(all(path == self.lock for path in used))
        self.assertEqual(Path(commerce_demo.LOCK_FILE), self.lock)

    def test_status_save_does_not_clobber_concurrent_ui_writes(self):
        statuses = {"task_main_001": {"id": "task_main_001", "status": "todo"}}
        # Simulate the UI creating a draft after run_queue loaded statuses.
        orch_db.save(self.status, {"task_ecom_content_abcdef0123": {
            "id": "task_ecom_content_abcdef0123", "status": "waiting_approval"}})
        statuses["task_main_001"]["status"] = "blocked"
        mini_orch.save_task_status(statuses, "task_main_001")
        disk = self.statuses()
        self.assertEqual(disk["task_main_001"]["status"], "blocked")
        self.assertEqual(disk["task_ecom_content_abcdef0123"]["status"], "waiting_approval")

    def test_save_blocks_while_ui_holds_lock(self):
        import threading

        statuses = {"task_main_001": {"id": "task_main_001", "status": "todo"}}
        done = threading.Event()

        def writer():
            mini_orch.save_task_status(statuses, "task_main_001")
            done.set()

        with commerce_demo.demo_lock():
            thread = threading.Thread(target=writer)
            thread.start()
            self.assertFalse(done.wait(0.3))
        thread.join(5)
        self.assertTrue(done.is_set())


class CliApproveTests(DemoStateSandbox):
    def test_cli_refuses_demo_tasks(self):
        task_id = self.new_draft()
        out = io.StringIO()
        with redirect_stdout(out):
            result = mini_orch.approve_task(task_id)
        self.assertFalse(result)
        self.assertIn("/inbox", out.getvalue())
        self.assertEqual(self.statuses()[task_id]["approval_status"], "waiting_approval")

    def test_cli_still_approves_normal_tasks(self):
        with redirect_stdout(io.StringIO()):
            mini_orch.approve_task("task_main_001")
        self.assertEqual(self.statuses()["task_main_001"]["approval_status"], "approved")


class AuditOrderingTests(DemoStateSandbox):
    def test_failed_gate_publishes_no_audit(self):
        task_id = self.new_draft()
        with patch.object(mini_orch, "decide_approval",
                          return_value={"ok": False, "reason": "already_decided"}):
            with self.assertRaises(commerce_demo.DemoError):
                commerce_demo.decide(task_id, "approved", "Ben Lee", 1, channel="brand_site_faq")
        self.assertEqual(commerce_demo.audit_records(), [])

    def test_audit_published_after_gate_and_linked(self):
        task_id = self.new_draft()
        order = []
        real_decide = mini_orch.decide_approval
        real_publish = commerce_demo._publish_json

        def decide_spy(*args, **kwargs):
            order.append("gate")
            return real_decide(*args, **kwargs)

        def publish_spy(logical_name, *args, **kwargs):
            order.append(logical_name)
            return real_publish(logical_name, *args, **kwargs)

        with patch.object(mini_orch, "decide_approval", decide_spy), \
                patch.object(commerce_demo, "_publish_json", publish_spy), \
                redirect_stdout(io.StringIO()):
            result = commerce_demo.decide(task_id, "approved", "Ben Lee", 1,
                                          channel="brand_site_faq")
        self.assertEqual(order, ["gate", commerce_demo.AUDIT_LOGICAL_NAME])
        decision = self.statuses()[task_id]["ecom"]["decision"]
        self.assertEqual(decision["audit_artifact_id"], result["audit_artifact_id"])


# zh: renames + draft titles ----------------------------------------------
class ZhHantNamingTests(DemoStateSandbox):
    def test_renamed_modules(self):
        hant = ui_strings("zh-Hant")
        hans = ui_strings("zh-Hans")
        en = ui_strings("en")
        self.assertEqual(hant["nav_leads"], "查詢／線索台")
        self.assertEqual(hant["nav_campaigns"], "推廣活動引擎")
        self.assertEqual(hant["mod_lead_desk_title"], "ORCH 查詢／線索台")
        self.assertEqual(hant["mod_campaign_engine_title"], "ORCH 推廣活動引擎")
        self.assertEqual(hans["nav_leads"], "查询／线索台")
        self.assertEqual(hans["nav_campaigns"], "推广活动引擎")
        self.assertEqual(en["nav_leads"], "Lead Desk")
        self.assertEqual(en["nav_campaigns"], "Campaign Engine")
        for key, value in (("nav_sales", "銷售中心"), ("nav_content", "內容工作室"),
                           ("nav_knowledge", "知識庫"), ("nav_market", "市場儀表板"),
                           ("nav_inbox", "審批收件箱"), ("nav_audit", "審計紀錄")):
            self.assertEqual(hant[key], value)

    def test_draft_title_is_zh_hant_by_default_and_per_locale(self):
        task_id = self.new_draft()
        task = orch_db.load(self.demo_queue, [])[0]
        self.assertTrue(task["title"].startswith("[示範] 內容工作室："), task["title"])
        self.assertIn("商品頁", task["title"])
        en_views = commerce_demo.draft_views(locale="en")
        self.assertTrue(en_views[0]["title"].startswith("[Demo] Content Studio: Product page"))
        hans_views = commerce_demo.draft_views(locale="zh-Hans")
        self.assertTrue(hans_views[0]["title"].startswith("[示范] 内容工作室："))
        self.assertEqual(en_views[0]["id"], task_id)

    def test_version_bumped(self):
        # v0.20.0 (WP-ORCH-11) supersedes v0.19.1 / v0.18.2.
        self.assertEqual(commerce_demo.DEMO_VERSION, "v0.21.0")


# 9. fresh-clone test config ------------------------------------------------
class TestConfigTests(unittest.TestCase):
    def test_pytest_config_present(self):
        root = Path(orch_ui.PROJECT_ROOT)
        self.assertTrue((root / "conftest.py").is_file())
        self.assertIn("pythonpath = .", (root / "pytest.ini").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
