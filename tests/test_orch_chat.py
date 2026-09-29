import unittest

from orch_chat import (
    GENERAL_SYSTEM_PROMPT,
    ORCH_CONTEXT_SYSTEM_PROMPT,
    build_messages,
    validate_chat_answer,
)


class OrchChatModePromptTests(unittest.TestCase):
    def test_general_prompt_allows_normal_dialogue(self):
        messages = build_messages(
            question="Who won the World Cup in 2022?",
            mode="general",
            context={},
            history=[],
        )

        system = messages[0]["content"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(system, GENERAL_SYSTEM_PROMPT)
        self.assertIn("Answer normal questions freely", system)
        self.assertIn("NOT limited to ORCH topics", system)
        self.assertNotIn(
            "only discuss ORCH",
            system.lower(),
        )
        # Must not instruct the model that it can only discuss ORCH.
        self.assertNotIn(
            "can only discuss",
            system.lower(),
        )
        self.assertNotIn(
            "task and policy system",
            system.lower(),
        )
        self.assertIn('execution_authority": "none"', system)
        self.assertEqual(messages[-1]["content"], "Who won the World Cup in 2022?")

    def test_orch_context_prompt_stays_scoped(self):
        messages = build_messages(
            question="Why is task_demo blocked?",
            mode="orch_context",
            context={"tasks": []},
            history=[],
        )

        system = messages[0]["content"]
        self.assertEqual(system, ORCH_CONTEXT_SYSTEM_PROMPT)
        self.assertIn("ORCH_CONTEXT", messages[-1]["content"])
        self.assertIn("task_lookup", system)
        self.assertIn("no execution authority", system.lower())
        self.assertIn('execution_authority": "none"', system)
        self.assertIn("General\nConversation mode", system)

    def test_orch_context_prompt_covers_ecommerce_sample_data(self):
        self.assertIn("ecommerce_demo", ORCH_CONTEXT_SYSTEM_PROMPT)
        self.assertIn("ARE in scope", ORCH_CONTEXT_SYSTEM_PROMPT)
        self.assertIn("Approval Inbox", ORCH_CONTEXT_SYSTEM_PROMPT)

    def test_locale_instruction_is_appended(self):
        for mode in ("orch_context", "general"):
            messages = build_messages(
                question="SAMPLE-001係咩？",
                mode=mode,
                context={"ecommerce_demo": {}},
                history=[],
                locale="zh-Hant",
            )
            system = messages[0]["content"]
            self.assertIn("UI_LOCALE: zh-Hant", system)
            self.assertIn("Traditional Chinese", system)
            self.assertIn("refusals", system)
            self.assertIn('execution_authority": "none"', system)

    def test_unknown_locale_adds_nothing(self):
        messages = build_messages(
            question="hi", mode="orch_context", context={}, history=[],
            locale="xx",
        )
        self.assertEqual(messages[0]["content"], ORCH_CONTEXT_SYSTEM_PROMPT)

    def test_ask_orch_sends_locale_without_network(self):
        from unittest.mock import patch, MagicMock
        import json as _json
        import orch_chat

        body = _json.dumps({
            "id": "r1", "model": "m",
            "choices": [{"message": {"content": _json.dumps({
                "answer": "示範商品。", "referenced_task_ids": [],
                "referenced_artifact_ids": [], "limitations": [],
                "execution_authority": "none",
            })}}],
        }).encode("utf-8")
        response = MagicMock()
        response.read.return_value = body
        response.__enter__.return_value = response
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}), \
             patch.object(orch_chat.urllib.request, "urlopen", return_value=response) as urlopen:
            result = orch_chat.ask_orch(
                "SAMPLE-001係咩？", "orch_context", {"ecommerce_demo": {}}, [],
                locale="zh-Hant",
            )
        payload = _json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertIn("UI_LOCALE: zh-Hant", payload["messages"][0]["content"])
        self.assertEqual(result["chat"]["answer"], "示範商品。")

    def test_validate_rejects_non_none_authority(self):
        with self.assertRaisesRegex(
            Exception,
            "execution authority",
        ):
            validate_chat_answer(
                {
                    "answer": "ok",
                    "referenced_task_ids": [],
                    "referenced_artifact_ids": [],
                    "limitations": [],
                    "execution_authority": "execute",
                }
            )


if __name__ == "__main__":
    unittest.main()
