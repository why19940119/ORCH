"""v0.20.1 chat accuracy follow-up.

A real-API run fabricated a 30-day revenue figure. The chat context now
carries precomputed 7 / 30 day totals with the previous period, best and
worst days, top pages and a complete weekly series with partial weeks
flagged. The prompt forbids estimates, uses HK$ and 匯入數據, and replies
are cleaned of internal labels server-side. Also: sample won-leads carry no
rank, and account-change requests that no longer apply are auto-closed.

The OpenRouter HTTP call is mocked; nothing leaves the machine.
"""

import json
import os
import re
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import commerce_demo
import commerce_import
import orch_auth
import orch_chat
import orch_ui
from auth_testing import use_temp_auth
from orch_chat import ORCH_CONTEXT_SYSTEM_PROMPT, build_system_prompt, sanitize_reply
from orch_ui import PROJECT_ROOT
from test_commerce_import import ImportSandbox
from ui_i18n import SUPPORTED_LOCALES, ui_strings

START = date(2026, 7, 1)
DAYS = 90   # 2026-07-01 .. 2026-09-28


def fixture_csvs():
    """90 days of one order a day (SKU A: qty 1 + i%3, HK$10*(i+1)) plus one
    big SKU B order on 2026-08-15 (qty 2, HK$5,000)."""
    orders = ["order_id,date,sku,quantity,amount_hkd"]
    for i in range(DAYS):
        day = START + timedelta(days=i)
        orders.append(f"O{i},{day.isoformat()},A,{1 + i % 3},{10 * (i + 1)}")
    orders.append("OB,2026-08-15,B,2,5000")
    products = ("sku,name,price_hkd,stock,category\n"
                "A,Alpha Tea,10,500,tea\nB,Beta Pot,2500,3,ware\n")
    traffic = ("date,page,pageviews,source\n"
               "2026-09-01,/,3000,google\n"
               "2026-09-02,/sale,1200,instagram\n"
               "2026-09-03,/tea,800,direct\n")
    return {"products": products, "orders": "\n".join(orders) + "\n", "traffic": traffic}


def chat_response(answer="數據截至 2026-09-28。", limitations=()):
    body = json.dumps({
        "id": "r-chat", "model": "test/model",
        "choices": [{"message": {"content": json.dumps({
            "answer": answer, "referenced_task_ids": [], "referenced_artifact_ids": [],
            "limitations": list(limitations), "execution_authority": "none",
        })}}],
    }).encode("utf-8")
    response = MagicMock()
    response.read.return_value = body
    response.__enter__.return_value = response
    return response


class ChatHarness(ImportSandbox):
    def ask(self, question="過去30日營業額幾多？", response=None):
        self.client.get("/chat")
        with self.client.session_transaction() as stored:
            token = stored["csrf_token"]
            stored["last_chat_at"] = 0   # skip the 3-second chat rate limit
        self.audit_calls = []
        env = {"OPENROUTER_API_KEY": "test-not-a-real-key", "ORCH_DEMO_FORCE_MOCK": ""}
        with patch.dict(os.environ, env), \
                patch.object(orch_chat.urllib.request, "urlopen",
                             return_value=response or chat_response()) as urlopen, \
                patch.object(orch_ui, "record_chat_usage", lambda **_: None), \
                patch.object(orch_ui, "publish_chat_audit_artifact",
                             lambda **kw: self.audit_calls.append(kw) or {"artifact_id": "artifact_chat_test"}):
            result = self.client.post("/chat", data={
                "csrf_token": token, "mode": "orch_context", "question": question})
        return result, urlopen

    def prompt(self, question="過去30日營業額幾多？"):
        result, urlopen = self.ask(question)
        self.assertEqual(result.status_code, 200)
        payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        return payload["messages"][0]["content"], payload["messages"][-1]["content"]

    @staticmethod
    def context_of(user):
        return json.loads(user.split("REFERENCE_DATA:\n", 1)[1].split("\n\nUSER_QUESTION:", 1)[0])

    def summary(self):
        self.assertEqual(self.upload(**fixture_csvs()).status_code, 302)
        system, user = self.prompt()
        store = self.context_of(user)["store_data"]
        return system, store, "\n".join(store["summary"])


class PeriodTotalsPromptTests(ChatHarness):
    """Item 1: prompt capture over a >60 day fixture with exact values."""

    def test_metrics_period_totals(self):
        self.upload(**fixture_csvs())
        metrics = commerce_import.compute_metrics()
        p7, p30 = metrics["period_totals"]["7"], metrics["period_totals"]["30"]
        self.assertEqual(p7["current"], {"start": "2026-09-22", "end": "2026-09-28",
                                         "revenue": 6090.0, "orders": 7, "units": 15,
                                         "covered_days": 7, "fully_covered": True,
                                         "covered": True})
        self.assertEqual((p7["previous"]["revenue"], p7["previous"]["orders"], p7["previous"]["units"]),
                         (5600.0, 7, 14))
        self.assertEqual(p7["change"]["revenue"], {"abs": 490.0, "pct": 8.8})
        self.assertEqual(p30["current"]["revenue"], 22650.0)
        self.assertEqual((p30["previous"]["start"], p30["previous"]["end"]), ("2026-07-31", "2026-08-29"))
        self.assertEqual((p30["previous"]["revenue"], p30["previous"]["orders"], p30["previous"]["units"]),
                         (18650.0, 31, 62))
        self.assertEqual(p30["change"]["orders"], {"abs": -1, "pct": -3.2})
        self.assertTrue(p30["previous"]["fully_covered"])
        self.assertEqual(metrics["best_days"][0]["date"], "2026-08-15")
        self.assertEqual(metrics["worst_days"][0], {"date": "2026-07-01", "revenue": 10.0,
                                                    "orders": 1, "units": 1})
        self.assertEqual(len(metrics["weekly_series"]), 14)

    def test_prompt_contains_exact_period_totals(self):
        _, store, summary = self.summary()
        self.assertIn("last_7_days (2026-09-22 to 2026-09-28): revenue HK$6,090.00, orders 7, units 15", summary)
        self.assertIn("previous_7_days (2026-09-15 to 2026-09-21): revenue HK$5,600.00, orders 7, units 14", summary)
        self.assertIn("change_7_days vs previous: revenue +HK$490.00 (+8.8%), orders +0 (+0.0%), units +1 (+7.1%)",
                      summary)
        self.assertIn("last_30_days (2026-08-30 to 2026-09-28): revenue HK$22,650.00, orders 30, units 60", summary)
        self.assertIn("previous_30_days (2026-07-31 to 2026-08-29): revenue HK$18,650.00, orders 31, units 62",
                      summary)
        self.assertIn("change_30_days vs previous: revenue +HK$4,000.00 (+21.4%), orders -1 (-3.2%), units -2 (-3.2%)",
                      summary)
        # The fabricated-figure failure mode: every 30-day number is given.
        self.assertEqual(store["as_of_date"], "2026-09-28")

    def test_best_worst_days_and_top_pages(self):
        _, _, summary = self.summary()
        best = summary.split("best_days_by_revenue (all time): ")[1].split("\n")[0]
        self.assertTrue(best.startswith("2026-08-15 HK$5,460.00 (2 orders, 3 units)"), best)
        worst = summary.split("worst_days_by_revenue (all time, days with orders): ")[1].split("\n")[0]
        self.assertTrue(worst.startswith("2026-07-01 HK$10.00 (1 orders, 1 units)"), worst)
        self.assertIn("days_with_orders: 90 of 90 days", summary)
        pages = summary.split("top_pages_by_pageviews")[1].split("\n")[1:4]
        self.assertEqual(pages, ['- "/"|3000|60.0%', '- "/sale"|1200|24.0%', '- "/tea"|800|16.0%'])

    def test_weekly_series_flags_partial_weeks(self):
        _, _, summary = self.summary()
        self.assertIn("- 2026-W40 (2026-09-28 to 2026-09-28, PARTIAL: 1 of 7 days): HK$900.00, 1 orders, 3 units",
                      summary)
        self.assertIn("- 2026-W27 (2026-07-01 to 2026-07-05, PARTIAL: 5 of 7 days)", summary)
        full = [line for line in summary.split("\n") if line.startswith("- 2026-W39")][0]
        self.assertIn("(2026-09-21 to 2026-09-27)", full)
        self.assertNotIn("PARTIAL", full)
        self.assertIn("14 of 14", summary)   # complete series

    def test_weekly_series_includes_weeks_without_orders(self):
        self.upload(orders="order_id,date,sku,quantity,amount_hkd\n"
                           "O1,2026-09-01,TEA-01,1,88\nO2,2026-09-20,TEA-01,1,88\n")
        weeks = commerce_import.compute_metrics()["weekly_series"]
        self.assertEqual([w["week"] for w in weeks], ["2026-W36", "2026-W37", "2026-W38"])
        self.assertEqual((weeks[1]["revenue"], weeks[1]["orders"], weeks[1]["partial"]), (0.0, 0, False))

    def test_labels_currency_and_chinese_data_label(self):
        _, store, summary = self.summary()
        self.assertTrue(summary.startswith("數據來源 / data source: 匯入數據"), summary[:80])
        self.assertEqual(store["data_label_by_language"]["zh-Hant"], "匯入數據")
        self.assertEqual(store["data_label_by_language"]["zh-Hans"], "导入数据")
        self.assertIn("匯入數據", store["data_label"])
        self.assertEqual(store["currency"], "HK$")
        self.assertNotIn("進口", json.dumps(store, ensure_ascii=False))
        # Every money figure in the summary carries HK$.
        self.assertFalse(re.search(r"revenue \d", summary), "bare revenue figure without HK$")


class BudgetTests(unittest.TestCase):
    def big_metrics(self):
        rows = [{"sku": f"SKU-{i:03d}", "name": "Very long product name " * 3, "units": 500 - i,
                 "orders": 10, "revenue": 10000.0 - i, "share_pct": 1.0} for i in range(200)]
        weeks = [{"week": f"2025-W{i:02d}", "week_start": "2025-01-06", "covered_start": "2025-01-06",
                  "covered_end": "2025-01-12", "covered_days": 7, "partial": False,
                  "revenue": 1000.0, "orders": 5, "units": 9} for i in range(1, 53)]
        period = {"days": 30, "current": {"start": "a", "end": "b", "revenue": 1.0, "orders": 1, "units": 1},
                  "previous": {"start": "c", "end": "d", "revenue": 2.0, "orders": 2, "units": 2,
                               "fully_covered": True},
                  "change": {"revenue": {"abs": -1.0, "pct": -50.0}, "orders": {"abs": -1, "pct": -50.0},
                             "units": {"abs": -1, "pct": -50.0}}}
        return {
            "totals": {"revenue": 1.0, "orders": 1, "order_lines": 1, "units": 1, "aov": 1.0,
                       "pageviews": 1, "products": 200},
            "order_period": ["2025-01-01", "2025-12-31"],
            "period_totals": {"7": dict(period, days=7), "30": period},
            "sales_by_sku": rows, "latest_day_skus": rows,
            "stock_cover": [{"sku": r["sku"], "name": r["name"], "stock": 1, "velocity_per_day": 1.0,
                             "days_of_cover": 1.0, "status": "low"} for r in rows],
            "traffic_by_source": [{"source": f"s{i}", "pageviews": 1, "share_pct": 1.0} for i in range(50)],
            "traffic_by_page": [{"page": f"/p/{i}" * 5, "pageviews": 1, "share_pct": 1.0} for i in range(50)],
            "weekly_series": weeks, "revenue_by_day": [{"date": "2025-12-31", "revenue": 1.0,
                                                       "orders": 1, "units": 1}] * 30,
            "best_days": [], "worst_days": [],
        }

    def test_large_dataset_shrinks_but_keeps_period_totals(self):
        lines = commerce_demo.chat_summary(self.big_metrics(), "2025-12-31")
        text = "\n".join(lines)
        self.assertLessEqual(len(text) + 1, commerce_demo.CHAT_SUMMARY_BUDGET_CHARS)
        for key in ("last_7_days", "previous_7_days", "change_7_days", "last_30_days",
                    "previous_30_days", "change_30_days", "all_time_totals"):
            self.assertIn(key, text)
        self.assertIn("of 200", text)       # shrunk lists say how many exist
        self.assertIn("omitted", text)      # weekly series says it is shortened

    def test_period_totals_kept_even_with_tiny_budget(self):
        lines = commerce_demo.chat_summary(self.big_metrics(), "2025-12-31", budget=100)
        self.assertIn("change_30_days", "\n".join(lines))

    def test_small_dataset_uses_full_level(self):
        metrics = self.big_metrics()
        metrics["sales_by_sku"] = metrics["sales_by_sku"][:4]
        text = "\n".join(commerce_demo.chat_summary(metrics, "2025-12-31", budget=10 ** 6))
        self.assertIn("4 of 4", text)


class PromptRuleTests(unittest.TestCase):
    """Items 2 and 3: no estimation, HK$, 匯入數據, register."""

    def test_no_estimation_rule(self):
        system = ORCH_CONTEXT_SYSTEM_PROMPT
        self.assertIn("Never estimate, extrapolate", system)
        self.assertIn("do not do your own arithmetic", system)
        self.assertIn("say plainly that the\ndata does not include it", system)
        self.assertIn("PARTIAL", system)

    def test_wording_rules(self):
        system = ORCH_CONTEXT_SYSTEM_PROMPT
        self.assertIn("「匯入數據」", system)
        self.assertIn("「导入数据」", system)
        self.assertIn("Never call it 進口 or 进口", system)
        self.assertIn("HK$", system)
        self.assertNotIn("is read-only", system)

    def test_register_rule_in_locale_instruction(self):
        for locale in SUPPORTED_LOCALES:
            system = build_system_prompt("orch_context", locale)
            self.assertIn("reply in Cantonese written in Traditional Chinese", system)
            self.assertIn("Currency is always HK$", system)

    def test_imported_is_huiru_in_zh_hant_strings(self):
        zh_hant = ui_strings("zh-Hant")
        offenders = [k for k, v in zh_hant.items() if "進口" in v]
        self.assertEqual(offenders, [])
        self.assertIn("匯入", zh_hant["chat_data_imported"])
        self.assertNotIn("进口", json.dumps(ui_strings("zh-Hans"), ensure_ascii=False))

    def test_context_limitation_is_neutral(self):
        source = (PROJECT_ROOT / "orch_ui.py").read_text(encoding="utf-8")
        self.assertNotIn("store_data is read-only", source)


class SanitizerTests(ChatHarness):
    """Item 4a: internal labels never reach the displayed / stored reply."""

    def test_sanitize_reply_unit(self):
        text = sanitize_reply("根據 store_data 同 `ecom_import.json`，REFERENCE_DATA 顯示", "zh-Hant")
        self.assertEqual(text, "根據 數據 同 匯入數據，數據 顯示")
        text = sanitize_reply("From imported_store_data (data_source) in demo/sample_data.json.", "en")
        self.assertEqual(text, "From the imported data (the data) in the sample data.")
        self.assertEqual(sanitize_reply("据 data_source 显示", "zh-Hans"), "据 数据 显示")
        # Ordinary words that only contain the labels are left alone.
        self.assertEqual(sanitize_reply("restore_database storedata", "en"), "restore_database storedata")

    def test_reply_is_sanitized_before_display_and_storage(self):
        self.upload(**fixture_csvs())
        leaky = chat_response(
            answer="根據 store_data（data_source: imported_store_data，檔案 ecom_import.json），"
                   "REFERENCE_DATA 顯示 HK$6,090.00。",
            limitations=["sample_data.json is fictional", "ORCH_CONTEXT only"])
        result, _ = self.ask(response=leaky)
        html = result.get_data(as_text=True)
        self.assertIn("根據 數據（數據: 匯入數據，檔案 匯入數據），數據 顯示 HK$6,090.00。", html)
        self.assertEqual(len(self.audit_calls), 1)
        audit = self.audit_calls[0]
        chat = audit["provider_result"]["chat"]
        self.assertEqual(audit["answer"], chat["answer"])
        stored = json.dumps({"answer": chat["answer"], "limitations": chat["limitations"]},
                            ensure_ascii=False)
        with self.client.session_transaction() as session:
            history = json.dumps(orch_ui.CHAT_SESSIONS.get(session["chat_id"], []),
                                 ensure_ascii=False, default=str)
        self.assertIn("HK$6,090.00", history)
        for label in ("store_data", "data_source", "imported_store_data", "ecom_import.json",
                      "REFERENCE_DATA", "sample_data.json", "ORCH_CONTEXT"):
            self.assertNotIn(label, html, label)
            self.assertNotIn(label, stored, label)
            self.assertNotIn(label, history, label)

    def test_general_mode_is_untouched(self):
        chat = {"answer": "Edit package.json", "limitations": []}
        self.assertEqual(orch_chat.sanitize_chat_answer(chat, "en")["answer"], "Edit package.json")
        # ask_orch only applies the sanitizer in ORCH Context mode.
        source = (PROJECT_ROOT / "orch_chat.py").read_text(encoding="utf-8")
        self.assertIn('if mode == "orch_context" else validate_chat_answer(parsed_answer)', source)


class SampleLeadsTests(ChatHarness):
    """Item 4b: won leads are not presented as a ranking."""

    def test_won_leads_have_no_rank(self):
        ranking = commerce_demo.sample_sales_ranking()
        self.assertNotIn("won_order_leads_by_sku", ranking)
        leads = ranking["won_order_leads_not_sales_ranking"]
        self.assertTrue(leads)
        self.assertTrue(all("rank" not in row for row in leads))
        _, user = self.prompt("最好賣係咩？")
        self.assertNotIn('"rank"', user)
        self.assertIn("示範數據", self.context_of(user)["store_data"]["data_label"])


class AccountChangeTests(unittest.TestCase):
    """Item 4c: requests that no longer apply are refused or auto-closed."""

    def setUp(self):
        use_temp_auth(self, users=(("Ann Admin", "admin"), ("Bob Admin", "admin"),
                                   ("Ed Editor", "editor")))
        store = orch_auth.load_store()
        store["bootstrap"] = {"complete": True}
        orch_auth._save_store(store)

    def events(self, name):
        return [r for r in orch_auth.read_audit() if r["event"] == name]

    def test_duplicate_pending_request_is_refused(self):
        orch_auth.request_change("Ann Admin", "disable", "Ed Editor")
        with self.assertRaises(orch_auth.AuthError) as caught:
            orch_auth.request_change("Bob Admin", "disable", "ed editor")
        self.assertEqual(caught.exception.code, "duplicate_request")
        orch_auth.request_change("Ann Admin", "change_role", "Ed Editor", role="approver")
        with self.assertRaises(orch_auth.AuthError) as caught:
            orch_auth.request_change("Ann Admin", "change_role", "Ed Editor", role="approver")
        self.assertEqual(caught.exception.code, "duplicate_request")
        # A different target role is a different request.
        orch_auth.request_change("Ann Admin", "change_role", "Ed Editor", role="admin")

    def test_second_disable_is_auto_closed_after_first_applies(self):
        first = orch_auth.request_change("Ann Admin", "disable", "Ed Editor")
        store = orch_auth.load_store()   # simulate an older duplicate from v0.20.0
        dup = dict(store["pending_changes"][-1], id="chg_dup0000001", requested_by="Bob Admin")
        store["pending_changes"].append(dup)
        orch_auth._save_store(store)
        orch_auth.decide_change("Bob Admin", first["id"], "approved")
        changes = {c["id"]: c for c in orch_auth.pending_changes()}
        self.assertEqual(changes["chg_dup0000001"]["status"], "auto_closed")
        self.assertEqual(changes["chg_dup0000001"]["auto_close_reason"], "target_disabled")
        rows = self.events("account_change_auto_closed")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["change_id"], rows[0]["reason"], rows[0]["actor"]),
                         ("chg_dup0000001", "target_disabled", "system"))

    def test_revalidated_on_approve(self):
        role = orch_auth.request_change("Ann Admin", "change_role", "Ed Editor", role="approver")
        # The target's role changes by another route before approval.
        store = orch_auth.load_store()
        store["users"][orch_auth._key("Ed Editor")]["role"] = "approver"
        orch_auth._save_store(store)
        result = orch_auth.decide_change("Bob Admin", role["id"], "approved")
        self.assertEqual((result["status"], result["auto_close_reason"]), ("auto_closed", "invalid_role"))
        self.assertEqual(self.events("account_change_auto_closed")[0]["actor"], "Bob Admin")
        self.assertEqual(self.events("account_change_applied"), [])

    def test_deleted_target_is_auto_closed_on_approve(self):
        change = orch_auth.request_change("Ann Admin", "reset_password", "Ed Editor",
                                          password="Brand-New-Pass-1")
        store = orch_auth.load_store()
        del store["users"][orch_auth._key("Ed Editor")]
        orch_auth._save_store(store)
        result = orch_auth.decide_change("Bob Admin", change["id"], "approved")
        self.assertEqual(result["auto_close_reason"], "unknown_user")
        self.assertNotIn("password_hash", json.dumps(orch_auth.load_store()["pending_changes"]))

    def test_changes_for_disabled_target_are_refused(self):
        store = orch_auth.load_store()
        store["users"][orch_auth._key("Ed Editor")]["disabled"] = True
        orch_auth._save_store(store)
        for kind, extra in (("disable", {}), ("change_role", {"role": "approver"})):
            with self.assertRaises(orch_auth.AuthError) as caught:
                orch_auth.request_change("Ann Admin", kind, "Ed Editor", **extra)
            self.assertEqual(caught.exception.code, "target_disabled")
        orch_auth.request_change("Ann Admin", "enable", "Ed Editor")   # still allowed

    def test_same_role_change_is_refused(self):
        with self.assertRaises(orch_auth.AuthError) as caught:
            orch_auth.request_change("Ann Admin", "change_role", "Ed Editor", role="editor")
        self.assertEqual(caught.exception.code, "invalid_role")

    def test_requester_no_longer_admin_is_auto_closed_on_approve(self):
        change = orch_auth.request_change("Ann Admin", "disable", "Bob Admin")
        store = orch_auth.load_store()
        store["users"][orch_auth._key("Ann Admin")]["disabled"] = True
        store["users"][orch_auth._key("Cy Admin")] = orch_auth._new_user(
            "Cy Admin", "admin", "x", "test")
        orch_auth._save_store(store)
        # Review fix: it can never be approved, so it is closed (audited).
        result = orch_auth.decide_change("Cy Admin", change["id"], "approved")
        self.assertEqual((result["status"], result["auto_close_reason"]),
                         ("auto_closed", "requester_not_admin"))
        self.assertFalse(orch_auth.get_user("Bob Admin").get("disabled"))

    def test_i18n_keys(self):
        for code in SUPPORTED_LOCALES:
            strings = ui_strings(code)
            for key in ("auth_err_duplicate_request", "auth_err_target_disabled",
                        "gov_ev_account_change_auto_closed", "gov_msg_change_auto_closed",
                        "adm_status_auto_closed"):
                self.assertIn(key, strings, (code, key))


class ReviewFixSanitizerTests(unittest.TestCase):
    """PR #6 review items 1 and 4: only known internal names are replaced,
    in the UI locale; legitimate text is never garbled."""

    def test_legitimate_text_is_untouched(self):
        for text in ("Required artifact does not exist: output/policy_prerequisite.json",
                     "https://example.com/feed.json",
                     "Edit package.json",
                     "Download https://example.com/demo/sample_data.json",
                     "In summary: sales rose. The summary shows growth; the scope is small.",
                     "restore_database storedata"):
            for locale in ("en", "zh-Hant", "zh-Hans"):
                self.assertEqual(sanitize_reply(text, locale), text, (text, locale))

    def test_known_file_names_as_whole_tokens(self):
        self.assertEqual(sanitize_reply("see state/ecom_import.json", "en"), "see the imported data")
        self.assertEqual(sanitize_reply("see demo/sample_data.json", "en"), "see the sample data")
        self.assertEqual(sanitize_reply("`ecom_import.json` and sample_data.json", "en"),
                         "the imported data and the sample data")
        self.assertEqual(sanitize_reply("my_ecom_import.json", "en"), "my_ecom_import.json")

    def test_replacement_words_follow_ui_locale(self):
        text = "According to store_data and ecom_import.json"
        self.assertEqual(sanitize_reply(text, "en"), "According to the data and the imported data")
        self.assertEqual(sanitize_reply(text, "zh-Hant"), "According to 數據 and 匯入數據")
        self.assertEqual(sanitize_reply(text, "zh-Hans"), "According to 数据 and 导入数据")
        # Chinese text in an English UI still gets English words (UI locale).
        self.assertEqual(sanitize_reply("根據 store_data", "en"), "根據 the data")
        self.assertEqual(sanitize_reply("store_data", None), "the data")

    def test_internal_keys_dotted_backticked_and_snake_case(self):
        self.assertEqual(sanitize_reply("From store_data.summary.", "en"), "From the data.")
        self.assertEqual(sanitize_reply("REFERENCE_DATA.store_data.as_of_date is set", "en"),
                         "the data is set")
        self.assertEqual(sanitize_reply("see `summary` and `sales_ranking`", "en"),
                         "see the data and the data")
        self.assertEqual(sanitize_reply('"summary": [1]', "en"), "the data: [1]")
        self.assertEqual(sanitize_reply("as_of_date: 2026-09-28", "en"), "the data: 2026-09-28")
        for key in ("sales_ranking", "as_of_date", "matching_products",
                    "won_order_leads_not_sales_ranking"):
            self.assertEqual(sanitize_reply(f"{key} shows", "zh-Hant"), "數據 shows", key)
        self.assertEqual(sanitize_reply("imported_store_data.summary", "en"), "the imported data")
        # Plain prose words stay.
        self.assertEqual(sanitize_reply("Summary: a good week", "en"), "Summary: a good week")


class ReviewFixCoverageTests(ChatHarness):
    """PR #6 review item 2: windows not fully covered by the data."""

    def test_partial_current_and_uncovered_previous(self):
        # 20 days of data: 30-day window only partly covered, previous 30
        # days not covered at all -> no change figure (no +HK$x vs HK$0).
        self.upload(orders="order_id,date,sku,quantity,amount_hkd\n"
                           "O1,2026-09-01,TEA-01,1,88\nO2,2026-09-20,TEA-01,1,1986\n")
        metrics = commerce_import.compute_metrics()
        p30 = metrics["period_totals"]["30"]
        self.assertEqual((p30["current"]["covered_days"], p30["current"]["fully_covered"]), (20, False))
        self.assertEqual((p30["previous"]["covered"], p30["previous"]["covered_days"]), (False, 0))
        self.assertIsNone(p30["change"])
        p7 = metrics["period_totals"]["7"]
        self.assertTrue(p7["current"]["fully_covered"])
        self.assertTrue(p7["previous"]["fully_covered"])
        self.assertIsNotNone(p7["change"])
        summary = "\n".join(commerce_demo.chat_summary(metrics, "2026-09-20"))
        self.assertIn("last_30_days (2026-08-22 to 2026-09-20, PARTIAL: data covers only 20 of 30 days "
                      "(data starts 2026-09-01)): revenue HK$2,074.00", summary)
        self.assertIn("previous_30_days (2026-07-23 to 2026-08-21): NOT COVERED by the data", summary)
        self.assertIn("change_30_days vs previous: NOT AVAILABLE", summary)
        self.assertNotIn("+HK$2,074.00", summary)
        self.assertIn("change_7_days vs previous: revenue +HK$1,986.00", summary)

    def test_partially_covered_previous_has_no_change(self):
        self.upload(orders="order_id,date,sku,quantity,amount_hkd\n"
                           "O1,2026-09-10,TEA-01,1,88\nO2,2026-09-20,TEA-01,1,100\n")
        p7 = commerce_import.compute_metrics()["period_totals"]["7"]
        self.assertEqual((p7["previous"]["covered_days"], p7["previous"]["fully_covered"]), (4, False))
        self.assertIsNone(p7["change"])
        summary = "\n".join(commerce_demo.chat_summary(commerce_import.compute_metrics(), "2026-09-20"))
        self.assertIn("previous_7_days (2026-09-07 to 2026-09-13, PARTIAL: data covers only 4 of 7 days", summary)

    def test_fully_covered_fixture_keeps_change(self):
        self.upload(**fixture_csvs())
        p30 = commerce_import.compute_metrics()["period_totals"]["30"]
        self.assertTrue(p30["comparable"])
        self.assertEqual(p30["change"]["revenue"], {"abs": 4000.0, "pct": 21.4})


class ReviewFixAccountTests(unittest.TestCase):
    """PR #6 review item 3: requests from an admin later disabled/demoted."""

    def setUp(self):
        use_temp_auth(self, users=(("Ann Admin", "admin"), ("Bob Admin", "admin"),
                                   ("Cy Admin", "admin"), ("Ed Editor", "editor")))
        store = orch_auth.load_store()
        store["bootstrap"] = {"complete": True}
        orch_auth._save_store(store)

    def test_sweep_closes_requests_of_demoted_admin(self):
        stuck = orch_auth.request_change("Ann Admin", "change_role", "Ed Editor", role="approver")
        demote = orch_auth.request_change("Bob Admin", "change_role", "Ann Admin", role="editor")
        orch_auth.decide_change("Cy Admin", demote["id"], "approved")
        changes = {c["id"]: c for c in orch_auth.pending_changes()}
        self.assertEqual((changes[stuck["id"]]["status"], changes[stuck["id"]]["auto_close_reason"]),
                         ("auto_closed", "requester_not_admin"))
        rows = [r for r in orch_auth.read_audit() if r["event"] == "account_change_auto_closed"]
        self.assertEqual([(r["change_id"], r["reason"]) for r in rows],
                         [(stuck["id"], "requester_not_admin")])
        # And a fresh identical request is no longer blocked.
        again = orch_auth.request_change("Bob Admin", "change_role", "Ed Editor", role="approver")
        self.assertEqual(again["status"], "pending")

    def test_duplicate_check_skips_requests_of_disabled_admin(self):
        stuck = orch_auth.request_change("Ann Admin", "disable", "Ed Editor")
        store = orch_auth.load_store()   # disabled by another route, no sweep
        store["users"][orch_auth._key("Ann Admin")]["disabled"] = True
        orch_auth._save_store(store)
        fresh = orch_auth.request_change("Bob Admin", "disable", "Ed Editor")
        self.assertEqual(fresh["status"], "pending")
        orch_auth.decide_change("Cy Admin", fresh["id"], "approved")
        changes = {c["id"]: c for c in orch_auth.pending_changes()}
        self.assertEqual(changes[stuck["id"]]["status"], "auto_closed")


class ReviewFixPromptAndUiTests(unittest.TestCase):
    """PR #6 review items 5, 6 and 7."""

    def test_cantonese_register_and_revenue_term(self):
        system = build_system_prompt("orch_context", "zh-Hant")
        self.assertIn("consistently colloquial Cantonese from start to finish", system)
        self.assertIn("do not mix in written-Chinese", system)
        for text in (system, ORCH_CONTEXT_SYSTEM_PROMPT):
            self.assertIn("營業額", text)
            self.assertIn("营业额", text)
        self.assertIn("never\nwrite 營收 or 营收", ORCH_CONTEXT_SYSTEM_PROMPT)
        # 營收/营收 appears nowhere else: labels, context builder, UI strings.
        for name in ("ui_i18n.py", "commerce_demo.py", "commerce_import.py",
                     "commerce_ui.py", "orch_ui.py"):
            source = (PROJECT_ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("營收", source, name)
            self.assertNotIn("营收", source, name)
        for locale in SUPPORTED_LOCALES:
            blob = json.dumps(ui_strings(locale), ensure_ascii=False)
            self.assertNotIn("營收", blob)
            self.assertNotIn("营收", blob)

    def test_context_header_names_revenue(self):
        metrics = BudgetTests().big_metrics()
        header = commerce_demo.chat_summary(metrics, "2025-12-31")[0]
        self.assertIn("revenue = 營業額", header)

    def test_narrow_composer_stacks(self):
        source = (PROJECT_ROOT / "orch_ui.py").read_text(encoding="utf-8")
        desktop = source.index(".chat-page .composer-grid {\n      grid-template-columns: auto minmax(0, 1fr) auto;")
        block = source.split("v0.20.1 review fix: a later desktop rule", 1)[1].split("\n    }\n", 1)[0]
        late = source.index("v0.20.1 review fix: a later desktop rule")
        self.assertGreater(late, desktop)       # comes after, so it wins
        self.assertIn("@media (max-width: 720px)", block)
        self.assertIn("grid-template-columns: 1fr;", block)
        # It is the LAST composer-grid column rule in the stylesheet.
        self.assertGreater(late, source.rfind("grid-template-columns: auto minmax(0, 1fr) auto;"))
        self.assertIn("min-height: 120px;", block)

    def test_versions(self):
        # v0.21.0 merge: the app version wins (was v0.20.1 in PR #6).
        self.assertEqual(orch_auth.AUTH_VERSION, "v0.21.0")
        self.assertEqual(commerce_import.IMPORT_VERSION, "v0.21.0")
        use_temp_auth(self, users=())
        record = orch_auth.audit("test_event", "cli")
        self.assertEqual(record["audit_version"], "v0.21.0")


class ChatDataSourceUiTests(ImportSandbox):
    """Item 5: the data-source line is spaced help text that wraps."""

    def test_class_and_css(self):
        html = self.client.get("/chat").get_data(as_text=True)
        self.assertIn('class="composer-help chat-data-source" role="note"', html)
        css = html.split(".chat-page p.chat-data-source {", 1)[1].split("}", 1)[0]
        for rule in ("font-size: 12px", "line-height: 1.5", "margin: 2px 4px 12px",
                     "overflow-wrap: anywhere", "white-space: normal"):
            self.assertIn(rule, css)
        # Full-width line above the controls, not an item of the 3-column grid.
        form = html.split('id="chat-form"', 1)[1]
        self.assertLess(form.index("chat-data-source"), form.index('class="composer-grid"'))
        grid = form.split('class="composer-grid"', 1)[1].split("</form>", 1)[0]
        self.assertNotIn("chat-data-source", grid)

    def test_i18n_parity_and_version(self):
        keys = {code: set(ui_strings(code)) for code in SUPPORTED_LOCALES}
        self.assertEqual(keys["en"], keys["zh-Hant"])
        self.assertEqual(keys["en"], keys["zh-Hans"])
        self.assertEqual(commerce_demo.DEMO_VERSION, "v0.21.0")
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("v0.20.1", readme)      # changelog line kept
        self.assertIn("v0.21.0", readme)


if __name__ == "__main__":
    unittest.main()
