"""v0.19.0 (WP-ORCH-10) store-data CSV importer tests.

Every path is sandboxed (see DemoSandbox) and every model call is mocked:
drafts run with ORCH_DEMO_FORCE_MOCK=1 or with a patched ``ask_orch``.
"""

import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import commerce_demo
import commerce_import
from orch_ui import PROJECT_ROOT, app
from test_commerce_demo import DemoSandbox
from ui_i18n import SUPPORTED_LOCALES, ui_strings


BOM = "\ufeff"

PRODUCTS = (
    BOM + "sku,name,price_hkd,stock,category\n"
    "TEA-01,Oolong Tea 100g,88,12,tea\n"
    "TEA-02,Jasmine Tea 50g,58.5,300,tea\n"
    "CUP-01,Glass Cup,120,0,ware\n"
    "LOW-01,Last Few Mugs,99,2,ware\n"
    "TEA-01,Duplicate,1,1,tea\n"
    "BAD-01,Bad price,abc,5,x\n"
    "NEG-01,Negative stock,10,-3,x\n"
)
ORDERS = (
    "order_id,date,sku,quantity,amount_hkd\n"
    "O1,2026-09-01,TEA-01,2,176\n"
    "O1,2026-09-01,TEA-02,1,58.5\n"
    "O2,2026-09-10,TEA-01,5,440\n"
    "O3,2026-09-20,TEA-02,3,175.5\n"
    "O4,2026-09-25,LOW-01,6,594\n"
    "O3,2026-09-20,TEA-02,1,58.5\n"
    "O5,2026-13-01,TEA-01,1,88\n"
    "O6,2026-09-21,ZZZ-99,1,10\n"
    "O7,2026-09-22,TEA-01,0,0\n"
    "O8,2026-09-22,TEA-01,1,-5\n"
)
TRAFFIC = (
    "date,page,pageviews,source\n"
    "2026-09-01,/,500,google\n"
    "2026-09-10,/tea,300,instagram\n"
    "2026-09-20,/,200,direct\n"
    "2026-09-20,/,5,direct\n"
    "09/21/2026,/,5,email\n"
)


def csv_files(products=PRODUCTS, orders=ORDERS, traffic=TRAFFIC):
    files = {}
    for kind, text in (("products", products), ("orders", orders), ("traffic", traffic)):
        if text is not None:
            files[kind] = (f"{kind}.csv", text.encode("utf-8"))
    return files


class ImportSandbox(DemoSandbox):
    def upload(self, **overrides):
        data = {"csrf_token": self.token}
        for kind, (name, raw) in csv_files(**overrides).items():
            data[kind] = (io.BytesIO(raw), name)
        return self.client.post("/import", data=data, content_type="multipart/form-data")

    def state(self):
        return commerce_import.load_state()


class ValidationTests(unittest.TestCase):
    def run_import(self, **overrides):
        return commerce_import.run_import(csv_files(**overrides))

    def codes(self, report, file_kind):
        return [(e["row"], e["code"]) for e in report["errors"] if e["file"] == file_kind]

    def test_bom_header_is_accepted(self):
        dataset, report = self.run_import()
        self.assertIsNotNone(dataset)
        self.assertTrue(report["files"]["products"]["accepted"])
        self.assertEqual(dataset["products"][0]["sku"], "TEA-01")

    def test_per_row_errors(self):
        _, report = self.run_import()
        self.assertEqual(
            self.codes(report, "products"),
            [(6, "duplicate"), (7, "not_number"), (8, "negative")],
        )
        self.assertEqual(
            self.codes(report, "orders"),
            # v0.19.1: row 7 repeats O3 + TEA-02 and is merged, not rejected.
            [(8, "bad_date"), (9, "unknown_sku"),
             (10, "not_positive"), (11, "negative")],
        )
        self.assertEqual(self.codes(report, "traffic"), [(5, "duplicate"), (6, "bad_date")])

    def test_partial_import_keeps_valid_rows(self):
        dataset, report = self.run_import()
        self.assertEqual([p["sku"] for p in dataset["products"]], ["TEA-01", "TEA-02", "CUP-01", "LOW-01"])
        self.assertEqual(len(dataset["orders"]), 5)
        self.assertEqual(len(dataset["traffic"]), 3)
        files = report["files"]
        self.assertEqual((files["orders"]["rows_valid"], files["orders"]["rows_rejected"]), (6, 4))
        self.assertEqual(files["orders"]["rows_merged"], 1)
        self.assertEqual(report["error_count"], 9)

    def test_bad_header_skips_file(self):
        dataset, report = self.run_import(products="sku,title,price,stock,category\nA,B,1,1,c\n")
        self.assertEqual(report["errors"][0]["code"], "bad_header")
        self.assertFalse(report["files"]["products"]["accepted"])
        # Orders can't be validated against missing products: all unknown SKU.
        self.assertFalse(report["files"]["orders"]["accepted"])
        self.assertTrue(report["files"]["traffic"]["accepted"])
        self.assertEqual(dataset["products"], [])

    def test_header_order_and_case_are_tolerated(self):
        dataset, _ = self.run_import(products="Name, SKU ,price_hkd,stock,category\nTea,T1,5,1,x\n",
                                     orders=None, traffic=None)
        self.assertEqual(dataset["products"][0]["sku"], "T1")

    def test_invalid_date_values(self):
        for value in ("2026-02-30", "2026/09/01", "20260901", "2026-9-1"):
            self.assertIsNone(commerce_import._parse_date(value), value)
        self.assertEqual(commerce_import._parse_date("2024-02-29"), "2024-02-29")

    def test_numbers(self):
        self.assertEqual(commerce_import._parse_decimal("HK$1,234.50"), 1234.5)
        self.assertIsNone(commerce_import._parse_decimal("12abc"))
        self.assertEqual(commerce_import._parse_int("3.0"), 3)
        self.assertIsNone(commerce_import._parse_int("3.5"))

    def test_not_utf8_and_empty(self):
        dataset, report = commerce_import.run_import({
            "products": ("products.csv", "sku,name\n".encode("utf-16")),
            "traffic": ("traffic.csv", b""),
        })
        self.assertIsNone(dataset)
        codes = {e["file"]: e["code"] for e in report["errors"]}
        self.assertEqual(codes, {"products": "not_utf8", "traffic": "empty_file"})

    def test_wrong_column_count(self):
        _, report = self.run_import(traffic="date,page,pageviews,source\n2026-09-01,/,5\n",
                                    products=None, orders=None)
        self.assertEqual(report["errors"][0]["code"], "wrong_columns")

    def test_file_size_cap(self):
        big = ("sku,name,price_hkd,stock,category\n" + "A,B,1,1,c\n" * 10).encode()
        with patch.object(commerce_import, "MAX_FILE_BYTES", 64):
            dataset, report = commerce_import.run_import({"products": ("p.csv", big)})
        self.assertIsNone(dataset)
        self.assertEqual(report["errors"][0]["code"], "file_too_large")

    def test_unknown_sku_uses_previous_products(self):
        dataset, _ = self.run_import()
        again, report = commerce_import.run_import(
            {"orders": ("orders.csv", b"order_id,date,sku,quantity,amount_hkd\nO9,2026-09-26,TEA-02,1,58.5\n")},
            previous=dataset,
        )
        self.assertEqual(report["error_count"], 0)
        self.assertEqual(len(again["orders"]), 1)
        self.assertEqual(len(again["products"]), 4)
        self.assertEqual(len(again["traffic"]), 3)

    def test_error_messages_localized_without_mixed_language(self):
        _, report = self.run_import()
        for code in SUPPORTED_LOCALES:
            t = ui_strings(code)
            for error in report["errors"]:
                message = commerce_import.error_message(error, t)
                self.assertNotEqual(message, t["imp_err_generic"], error)
                if code != "en":
                    # Only data values and column names may be Latin text.
                    stripped = message
                    for token in (error["value"], error["field"], "YYYY-MM-DD", "SKU", "products.csv"):
                        stripped = stripped.replace(str(token), "")
                    self.assertNotRegex(stripped, r"[A-Za-z]{3,}", (code, message))


class MetricsTests(unittest.TestCase):
    def setUp(self):
        dataset, _ = commerce_import.run_import(csv_files())
        self.metrics = commerce_import.compute_metrics({"data": dataset, "imported_at_utc": "x"})

    def test_totals_and_sales_by_sku(self):
        m = self.metrics
        self.assertEqual(m["totals"]["revenue"], 1502.5)   # O3 TEA-02 rows merged
        self.assertEqual(m["totals"]["orders"], 4)
        self.assertEqual(m["totals"]["units"], 18)
        self.assertEqual(m["sales_by_sku"][0]["sku"], "TEA-01")
        self.assertEqual(m["sales_by_sku"][0]["revenue"], 616.0)
        self.assertEqual(m["order_period"], ["2026-09-01", "2026-09-25"])

    def test_revenue_by_week(self):
        weeks = {row["week"]: row["revenue"] for row in self.metrics["revenue_by_week"]}
        self.assertEqual(weeks, {"2026-W36": 234.5, "2026-W37": 440.0,
                                 "2026-W38": 234.0, "2026-W39": 594.0})

    def test_days_of_cover(self):
        cover = {row["sku"]: row for row in self.metrics["stock_cover"]}
        self.assertEqual(cover["CUP-01"]["status"], "out")
        # 6 units in the 30-day window -> 0.2/day; stock 2 -> 10 days (< 14).
        self.assertEqual(cover["LOW-01"]["status"], "low")
        self.assertEqual(cover["LOW-01"]["days_of_cover"], 10.0)
        self.assertEqual(cover["TEA-01"]["days_of_cover"], round(12 / (7 / 30), 1))
        self.assertEqual(self.metrics["stock_cover"][0]["sku"], "CUP-01")

    def test_traffic_and_conversion(self):
        m = self.metrics
        self.assertEqual(m["traffic_by_source"][0], {"source": "google", "pageviews": 500, "share_pct": 50.0})
        conv = m["conversion"]
        self.assertEqual((conv["start"], conv["end"]), ("2026-09-01", "2026-09-20"))
        self.assertEqual((conv["orders"], conv["pageviews"], conv["rate_pct"]), (3, 1000, 0.3))

    def test_no_overlap_means_no_conversion(self):
        dataset, _ = commerce_import.run_import(csv_files(
            traffic="date,page,pageviews,source\n2025-01-01,/,10,google\n"))
        m = commerce_import.compute_metrics({"data": dataset})
        self.assertIsNone(m["conversion"])


class ImportPageTests(ImportSandbox):
    def test_upload_requires_csrf(self):
        data = {"products": (io.BytesIO(PRODUCTS.encode()), "products.csv")}
        response = self.client.post("/import", data=data, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.state())
        for path in ("/import/folder", "/import/toggle", "/import/reset"):
            self.assertEqual(self.client.post(path, data={}).status_code, 400, path)

    def test_upload_report_and_banner_switch(self):
        page = self.client.get("/market").get_data(as_text=True)
        self.assertIn("data-sample-banner", page)

        response = self.upload()
        self.assertEqual(response.status_code, 302)
        state = self.state()
        self.assertTrue(state["active"])
        self.assertEqual(state["counts"], {"products": 4, "orders": 5, "traffic": 3})
        self.assertTrue(state["imported_at_utc"].endswith("+00:00"))

        report = self.client.get("/import").get_data(as_text=True)
        self.assertIn("data-import-errors", report)
        self.assertEqual(report.count('data-error-code="'), 9)
        self.assertIn('data-error-code="unknown_sku"', report)
        self.assertIn("ZZZ-99", report)
        self.assertIn(ui_strings("zh-Hant")["demo_msg_import_done"], report)

        for path in ("/sales", "/content", "/knowledge", "/leads", "/campaigns", "/market", "/inbox", "/audit", "/import"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn("data-imported-banner", html, path)
            self.assertIn("真實匯入數據", html, path)
            self.assertNotIn("data-sample-banner", html, path)

    def test_sample_sections_labelled(self):
        self.upload()
        for path in ("/sales", "/knowledge", "/leads", "/campaigns"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn("data-sample-note", html, path)
        sales = self.client.get("/sales").get_data(as_text=True)
        self.assertIn("TEA-01", sales)          # products table uses the import

    def test_market_dashboard_real_metrics(self):
        self.upload()
        html = self.client.get("/market").get_data(as_text=True)
        self.assertIn("data-imp-metrics", html)
        self.assertIn("HK$1,502", html)
        self.assertIn("0.3%", html)
        self.assertIn("data-conversion-assumption", html)
        self.assertIn("訂單數 ÷ 瀏覽量", html)
        self.assertIn("data-imp-cover", html)
        self.assertNotIn(ui_strings("zh-Hant")["demo_kpi_weekly"], html)

    def test_toggle_and_reset(self):
        self.upload()
        self.client.post("/import/toggle", data={"csrf_token": self.token, "active": "0"})
        self.assertFalse(self.state()["active"])
        self.assertIn("data-sample-banner", self.client.get("/market").get_data(as_text=True))
        self.client.post("/import/toggle", data={"csrf_token": self.token, "active": "1"})
        self.assertTrue(self.state()["active"])
        self.client.post("/import/reset", data={"csrf_token": self.token})
        self.assertIsNone(self.state())
        self.assertIn("data-sample-banner", self.client.get("/sales").get_data(as_text=True))

    def test_nothing_valid_keeps_previous_data(self):
        self.upload()
        before = self.state()["data"]
        self.upload(products="wrong,header\n1,2\n", orders=None, traffic=None)
        state = self.state()
        self.assertEqual(state["data"], before)
        self.assertFalse(state["last_report"]["accepted"])

    def test_non_csv_rejected(self):
        data = {"csrf_token": self.token, "products": (io.BytesIO(b"x"), "products.xlsx")}
        self.client.post("/import", data=data, content_type="multipart/form-data")
        self.assertIsNone(self.state())

    def test_request_size_cap(self):
        with patch.dict(app.config, {"MAX_CONTENT_LENGTH": 1024}):
            data = {"csrf_token": self.token,
                    "products": (io.BytesIO(b"a" * 4096), "products.csv")}
            response = self.client.post("/import", data=data, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 413)
        self.assertIn('href="/import"', response.get_data(as_text=True))

    def test_import_page_all_locales(self):
        self.upload()
        for code in SUPPORTED_LOCALES:
            self.client.post("/locale", data={"csrf_token": self.token, "locale": code, "next": "/import"})
            html = self.client.get("/import").get_data(as_text=True)
            t = ui_strings(code)
            self.assertIn(t["imp_report_title"], html, code)
            self.assertIn(t["imp_badge"], html, code)


class ImportDraftTests(ImportSandbox):
    def test_insight_draft_goes_to_inbox(self):
        self.upload()
        response = self.draft(kind="import_insight", language="zh-Hant")
        self.assertEqual(response.status_code, 302)
        task_id = self.demo_task_ids()[-1]
        self.assertIn("import_insight", task_id)
        self.assertEqual(self.statuses()[task_id]["approval_status"], "waiting_approval")
        view = commerce_demo.draft_views(locale="zh-Hant")[0]
        self.assertTrue(view["title"].startswith("[匯入數據]"))
        self.assertFalse(view["source"]["sample_data"])
        self.assertIn("TEA-01", view["body"])
        self.assertIn("HK$1,502.50", view["body"])
        self.assertIn("LOW-01", view["body"])            # restock suggestion
        inbox = self.client.get("/inbox").get_data(as_text=True)
        self.assertIn(task_id, inbox)

        # Approval goes through the existing gate + audit.
        commerce_demo.decide(task_id, "approved", "Ben Lee", 1, channel="internal_report")
        audit = commerce_demo.audit_records()[0]
        self.assertEqual(audit["kind"], "import_insight")
        self.assertFalse(audit["sample_data"])
        self.assertIn("data/import", audit["source"]["dataset"])
        self.assertFalse(audit["external_call"])

    def test_campaign_draft_uses_real_metrics(self):
        self.upload()
        self.draft(kind="import_campaign", sku="LOW-01", objective="sales", language="en")
        view = commerce_demo.draft_views(locale="en")[0]
        self.assertEqual(view["kind"], "import_campaign")
        self.assertIn("low stock", view["body"].lower())
        self.assertIn("google", view["body"])
        self.assertTrue(view["title"].startswith("[Imported] Campaign Engine"))

    def test_content_draft_uses_imported_product(self):
        self.upload()
        self.draft(kind="content", sku="TEA-02", content_type="product_page", language="zh-Hant")
        view = commerce_demo.draft_views(locale="zh-Hant")[0]
        self.assertIn("Jasmine Tea 50g", view["body"])
        self.assertIn("HK$58.5", view["body"])
        self.assertNotIn("Harbour Sample Co.", view["body"])
        self.assertEqual(view["source"]["refs"][0], "TEA-02")
        content_page = self.client.get("/content").get_data(as_text=True)
        self.assertIn('value="TEA-02"', content_page)
        self.assertNotIn('value="SAMPLE-001"', content_page)

    def test_sample_drafts_still_work_with_import(self):
        self.upload()
        self.assertEqual(self.draft(kind="lead_reply", inquiry_id="INQ-S-002").status_code, 302)
        view = commerce_demo.draft_views(locale="en")[0]
        self.assertTrue(view["source"]["sample_data"])
        self.assertTrue(view["title"].startswith("[Demo]"))

    def test_import_kinds_need_active_import(self):
        self.draft(kind="import_insight")
        self.assertEqual(self.demo_task_ids(), [])
        self.upload()
        commerce_import.set_active(False)
        self.draft(kind="import_campaign", sku="TEA-01", objective="sales")
        self.assertEqual(self.demo_task_ids(), [])

    def test_openrouter_path_is_mocked_and_prompt_grounded(self):
        self.upload()
        calls = []

        def fake_ask_orch(**kwargs):
            calls.append(kwargs)
            return {"chat": {"answer": "Restock LOW-01 first."}, "provider": "openrouter",
                    "response_model": "test/model", "response_id": "r1"}

        env = {"ORCH_DEMO_FORCE_MOCK": "", "OPENROUTER_API_KEY": "test-not-a-real-key"}
        with patch.dict(os.environ, env), \
                patch.object(commerce_demo, "ask_orch", fake_ask_orch), \
                patch.object(commerce_demo, "record_chat_usage", lambda **_: None):
            self.draft(kind="import_insight")
        self.assertEqual(len(calls), 1)
        prompt = calls[0]["question"]
        self.assertIn("IMPORTED", prompt)
        self.assertIn("orders / pageviews", prompt)
        self.assertLessEqual(len(prompt), commerce_demo.MAX_IMPORT_PROMPT_CHARS)
        self.assertEqual(calls[0]["max_question_chars"], commerce_demo.MAX_IMPORT_PROMPT_CHARS)
        view = commerce_demo.draft_views(locale="en")[0]
        self.assertEqual(view["body"], "Restock LOW-01 first.")
        self.assertEqual(view["approval_status"], "waiting_approval")

    def test_provider_error_falls_back_to_mock(self):
        self.upload()

        def failing(**_):
            raise commerce_demo.ChatProviderError("boom")

        with patch.dict(os.environ, {"ORCH_DEMO_FORCE_MOCK": "", "OPENROUTER_API_KEY": "x"}), \
                patch.object(commerce_demo, "ask_orch", failing):
            self.draft(kind="import_insight", language="en")
        view = commerce_demo.draft_views(locale="en")[0]
        self.assertEqual(view["provenance"]["provider"], "mock")
        self.assertIn("imported store data", view["body"])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_import_cli_"))
        self.patches = [
            patch.object(commerce_import, "IMPORT_DIR", self.tmp / "data" / "import"),
            patch.object(commerce_import, "IMPORT_STATE_FILE", self.tmp / "state" / "ecom_import.json"),
            patch.object(commerce_import, "LOCK_FILE", self.tmp / "state" / ".lock"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = commerce_import.main(list(args))
        return code, out.getvalue()

    def test_no_files(self):
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("找不到 CSV 檔案", out)
        self.assertTrue((self.tmp / "data" / "import").is_dir())

    def test_import_status_reset(self):
        folder = self.tmp / "data" / "import"
        folder.mkdir(parents=True)
        for kind, (name, raw) in csv_files().items():
            (folder / name).write_bytes(raw)
        code, out = self.run_cli("--lang", "en")
        self.assertEqual(code, 0)
        self.assertIn("orders.csv: 6 of 10 rows imported, 4 rejected", out)
        self.assertIn("orders.csv note: 1 rows repeated an order_id + sku", out)
        self.assertIn('SKU "ZZZ-99" is not in products.csv.', out)
        state = commerce_import.load_state()
        self.assertEqual(state["last_report"]["source"], "folder")
        code, out = self.run_cli("--status", "--lang", "en")
        self.assertIn("Imported data is in use", out)
        code, out = self.run_cli("--reset")
        self.assertIn("已刪除匯入數據", out)
        self.assertIsNone(commerce_import.load_state())
        code, out = self.run_cli("--reset", "--lang", "zh-Hans")
        self.assertIn("没有可删除的导入数据", out)

    def test_usage(self):
        code, out = self.run_cli("--bogus")
        self.assertEqual(code, 2)


class RepoHygieneTests(unittest.TestCase):
    def test_import_state_and_folder_gitignored(self):
        ignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("data/import/", ignore)
        self.assertIn("state/ecom_import.json", ignore)

    def test_imp_keys_parity_and_used_keys_exist(self):
        keys = {code: set(ui_strings(code)) for code in SUPPORTED_LOCALES}
        self.assertEqual(keys["en"], keys["zh-Hant"])
        self.assertEqual(keys["en"], keys["zh-Hans"])
        source = (PROJECT_ROOT / "commerce_ui.py").read_text(encoding="utf-8")
        source += (PROJECT_ROOT / "commerce_import.py").read_text(encoding="utf-8")
        used = set(re.findall(r"\bt\.(imp_[a-z0-9_]+)", source))
        used |= set(re.findall(r"\"(imp_[a-z0-9_]+)\"", source))
        used |= {f"imp_err_{c}" for c in ("bad_header", "missing_value", "not_number", "not_integer",
                                          "negative", "not_positive", "bad_date", "unknown_sku",
                                          "duplicate", "not_utf8", "empty_file", "too_many_rows",
                                          "too_long", "wrong_columns", "file_too_large",
                                          "order_date_conflict")}
        used |= {f"imp_notice_{c}" for c in ("encoding_fallback", "rows_skipped",
                                             "orders_merged", "orders_unmatched")}
        used |= {f"imp_enc_{e}" for e in commerce_import.FALLBACK_ENCODINGS}
        used |= {f"imp_field_{f}" for cols in commerce_import.SCHEMAS.values() for f in cols}
        used |= {f"imp_cover_{s}" for s in ("out", "low", "ok", "no_sales")}
        used |= {f"imp_file_{k}" for k in commerce_import.FILE_ORDER}
        used |= {f"imp_source_{k}" for k in ("cli", "folder", "upload")}
        for code in SUPPORTED_LOCALES:
            missing = sorted(k for k in used if k not in keys[code])
            self.assertEqual(missing, [], code)

    def test_version(self):
        # v0.20.0 (WP-ORCH-11) builds on the v0.19.1 import module.
        self.assertEqual(commerce_demo.DEMO_VERSION, "v0.21.1")
        self.assertEqual(commerce_import.IMPORT_VERSION, "v0.21.1")
        self.assertIn("v0.19.1", (PROJECT_ROOT / "README.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
