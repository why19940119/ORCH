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
