"""v0.20.0: ORCH Context chat answers store questions from the imported
store data when the import is active (sample data otherwise), states the
as-of date for time-relative questions and never names internal labels.

The OpenRouter HTTP call is mocked; nothing leaves the machine.
"""

import json
import os
from unittest.mock import MagicMock, patch

import commerce_demo
import commerce_import
import orch_chat
import orch_ui
from orch_chat import ORCH_CONTEXT_SYSTEM_PROMPT, build_system_prompt
from test_commerce_import import ImportSandbox
from ui_i18n import SUPPORTED_LOCALES, ui_strings

QUESTION = "今日最好賣係咩？"
INTERNAL_NAMES = ("ORCH_CONTEXT", "ecom_import", "ecom_demo_queue", "sample_data.json",
                  "state/", "demo/")


def chat_response():
    body = json.dumps({
        "id": "r-chat", "model": "test/model",
        "choices": [{"message": {"content": json.dumps({
            "answer": "數據截至 2026-09-25：LOW-01。", "referenced_task_ids": [],
            "referenced_artifact_ids": [], "limitations": [], "execution_authority": "none",
        })}}],
    }).encode("utf-8")
    response = MagicMock()
    response.read.return_value = body
    response.__enter__.return_value = response
    return response


class ChatStoreDataTests(ImportSandbox):
    def ask(self, question=QUESTION):
        self.client.get("/chat")
        with self.client.session_transaction() as stored:
            token = stored["csrf_token"]
            stored["last_chat_at"] = 0   # skip the 3-second chat rate limit
        env = {"OPENROUTER_API_KEY": "test-not-a-real-key", "ORCH_DEMO_FORCE_MOCK": ""}
        with patch.dict(os.environ, env), \
                patch.object(orch_chat.urllib.request, "urlopen", return_value=chat_response()) as urlopen, \
                patch.object(orch_ui, "record_chat_usage", lambda **_: None), \
                patch.object(orch_ui, "publish_chat_audit_artifact",
                             lambda **_: {"artifact_id": "artifact_chat_test"}):
            response = self.client.post("/chat", data={
                "csrf_token": token, "mode": "orch_context", "question": question})
        return response, urlopen

    def prompt(self, question=QUESTION):
        response, urlopen = self.ask(question)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(urlopen.call_count, 1)
        payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        return payload["messages"][0]["content"], payload["messages"][-1]["content"]

    def context_of(self, user):
        return json.loads(user.split("REFERENCE_DATA:\n", 1)[1].split("\n\nUSER_QUESTION:", 1)[0])

    def test_imported_data_ranking_and_as_of_date(self):
        self.upload()
        metrics = commerce_import.compute_metrics()
        system, user = self.prompt()
        store = self.context_of(user)["store_data"]
        self.assertEqual(store["data_source"], "imported_store_data")
        self.assertEqual(store["as_of_date"], "2026-09-25")
        self.assertIn("2026-09-25", store["as_of_note"])
        summary = "\n".join(store["summary"])
        # Top SKUs by revenue (TEA-01 616.00) and by units, latest-day ranking.
        by_revenue = summary.split("top_products_by_revenue")[1]
        self.assertTrue(by_revenue.split("\n")[1].startswith('- "TEA-01"|'), by_revenue[:200])
        self.assertIn("616.00", by_revenue)
        self.assertTrue(summary.split("top_products_by_units")[1].split("\n")[1].startswith('- "TEA-01"|'))
        latest = summary.split("latest_day 2026-09-25")[1]
        self.assertTrue(latest.split("\n")[1].startswith('- "LOW-01"|'), latest[:200])
        self.assertIn("as of 2026-09-25 (latest order date; not real-time)", summary)
        self.assertIn("recent_order_days", summary)
        self.assertIn("low_stock", summary)
        self.assertIn("traffic_by_source", summary)
        self.assertIn("conversion:", summary)
        self.assertEqual(metrics["sales_by_sku"][0]["sku"], "TEA-01")
        # No sample catalogue when the import is active.
        self.assertNotIn("SAMPLE-001", user)
        self.assertNotIn("竹纖維毛巾", user)
        # System prompt: latest-date instruction, no internal names.
        self.assertIn("latest available date", system)
        self.assertIn("數據截至", system)
        self.assertIn("Never\nclaim the figures are live", system)
        for name in INTERNAL_NAMES:
            self.assertNotIn(name, system, name)
            self.assertNotIn(name, user, name)

    def test_unmatched_orders_count_in_summary(self):
        self.upload()
        self.upload(products="sku,name,price_hkd,stock,category\nTEA-02,Jasmine Tea 50g,58.5,300,tea\n",
                    orders=None, traffic=None)
        _, user = self.prompt()
        store = self.context_of(user)["store_data"]
        self.assertEqual(store["unmatched_order_lines"], 3)
        self.assertIn("excluded_order_lines (SKU not in products, left out of every figure): 3", "\n".join(store["summary"]))

    def test_keyword_lookup_on_top_of_summary(self):
        self.upload()
        _, user = self.prompt("TEA-01 最近賣成點？")
        products = self.context_of(user)["store_data"]["matching_products"]
        self.assertEqual([p["sku"] for p in products], ['"TEA-01"'])
        self.assertEqual((products[0]["units_sold"], products[0]["stock"]), (7, 12))
        _, user = self.prompt("How long does shipping take?")
        self.assertEqual(self.context_of(user)["store_data"]["matching_products"], [])

    def test_sample_mode_labels_sample_and_has_no_sales_note(self):
        system, user = self.prompt()
        store = self.context_of(user)["store_data"]
        self.assertEqual(store["data_source"], "sample_data")
        self.assertIn("SAMPLE", store["data_label"])
        self.assertIsNone(store["as_of_date"])
        ranking = store["sales_ranking"]
        self.assertFalse(ranking["per_product_sales_available"])
        self.assertIn("no per-product sales", ranking["note"])
        # Ranking only uses values present in the sample (won leads).
        data = commerce_demo.load_sample_data()
        won = {lead["sku"]: lead for lead in data["order_leads"] if lead["stage"] == "won"}
        leads = ranking["won_order_leads_not_sales_ranking"]
        self.assertEqual({row["sku"] for row in leads}, set(won))
        for row in leads:
            self.assertNotIn("rank", row)   # v0.20.1: not read as a best-seller list
            self.assertEqual(row["est_value_hkd"], won[row["sku"]]["est_value_hkd"])
        self.assertEqual([w["orders"] for w in ranking["weekly_order_counts_all_products"]],
                         data["kpi"]["orders"])
        self.assertIn("sample has\n  no per-product sales data", system)
        for name in INTERNAL_NAMES:
            self.assertNotIn(name, user, name)

    def test_import_switched_off_uses_sample(self):
        self.upload()
        self.client.post("/import/toggle", data={"csrf_token": self.token, "active": "0"})
        _, user = self.prompt()
        self.assertEqual(self.context_of(user)["store_data"]["data_source"], "sample_data")

    def test_question_length_limit_kept(self):
        self.upload()
        response, urlopen = self.ask("好" * (orch_chat.MAX_QUESTION_CHARS + 1))
        self.assertEqual(urlopen.call_count, 0)
        self.assertIn('maxlength="800"', self.client.get("/chat").get_data(as_text=True))

    def test_chat_page_says_which_data(self):
        html = self.client.get("/chat").get_data(as_text=True)
        self.assertIn('data-chat-data-source="sample"', html)
        self.assertIn(ui_strings("zh-Hant")["chat_data_sample"], html)
        self.upload()
        html = self.client.get("/chat").get_data(as_text=True)
        self.assertIn('data-chat-data-source="imported"', html)
        self.assertIn(ui_strings("zh-Hant")["chat_data_imported"].format(date="2026-09-25"), html)


class ChatPromptTextTests(ImportSandbox):
    def test_system_prompt_rules(self):
        for locale in (None, "zh-Hant", "en"):
            system = build_system_prompt("orch_context", locale)
            self.assertNotIn("ORCH_CONTEXT", system)
            self.assertIn("Never mention internal labels", system)
            self.assertIn("as_of_date", system)
            self.assertIn("本月", system)
        self.assertNotIn("ecom_import", ORCH_CONTEXT_SYSTEM_PROMPT)

    def test_i18n_parity(self):
        for key in ("chat_data_imported", "chat_data_sample"):
            values = [ui_strings(code)[key] for code in SUPPORTED_LOCALES]
            self.assertEqual(len(set(values)), len(values), key)
        self.assertIn("{date}", ui_strings("zh-Hans")["chat_data_imported"])
