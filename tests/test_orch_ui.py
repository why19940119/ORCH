import unittest
from unittest.mock import patch

from orch_ui import CHAT_SESSIONS, app


class OrchUiTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_read_only_routes_return_success(self):
        for path in [
            "/",
            "/tasks",
            "/events",
            "/artifacts",
        ]:
            response = self.client.get(path)
            self.assertEqual(
                response.status_code,
                200,
                path,
            )

    def test_unknown_task_returns_not_found(self):
        response = self.client.get(
            "/tasks/task_does_not_exist"
        )

        self.assertEqual(response.status_code, 404)

    def test_ui_post_routes_are_chat_only(self):
        post_routes = sorted(
            rule.rule
            for rule in app.url_map.iter_rules()
            if "POST" in rule.methods
        )

        self.assertEqual(post_routes, ["/chat"])


if __name__ == "__main__":
    unittest.main()


class OrchChatUiTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_chat_page_loads(self):
        response = self.client.get("/chat")

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"ORCH Chat",
            response.data,
        )

    def test_chat_route_rejects_invalid_csrf(self):
        response = self.client.post(
            "/chat",
            data={
                "csrf_token": "invalid",
                "mode": "general",
                "question": "Hello",
            },
        )

        self.assertEqual(response.status_code, 400)


class OrchChatProviderTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        CHAT_SESSIONS.clear()
        self.client = app.test_client()

    @patch("orch_ui.ask_orch")
    def test_valid_chat_request_uses_provider_once(
        self,
        mock_ask_orch,
    ):
        mock_ask_orch.return_value = {
            "provider": "openrouter",
            "requested_model": (
                "mistralai/mistral-medium-3.1"
            ),
            "response_model": (
                "mistralai/mistral-medium-3.1"
            ),
            "response_id": "chat-test-response-001",
            "usage": {
                "total_tokens": 0,
                "cost": 0,
            },
            "chat": {
                "answer": (
                    "ORCH is a human-gated task orchestrator."
                ),
                "referenced_task_ids": [],
                "referenced_artifact_ids": [],
                "limitations": [
                    "No execution authority."
                ],
                "execution_authority": "none",
            },
        }

        self.client.get("/chat")

        with self.client.session_transaction() as session:
            csrf_token = session["csrf_token"]

        response = self.client.post(
            "/chat",
            data={
                "csrf_token": csrf_token,
                "mode": "general",
                "question": "What is ORCH?",
            },
        )

        self.assertEqual(response.status_code, 200)

        self.assertIn(
            b"human-gated task orchestrator",
            response.data,
        )

        mock_ask_orch.assert_called_once()

        call_kwargs = mock_ask_orch.call_args.kwargs

        self.assertEqual(
            call_kwargs["question"],
            "What is ORCH?",
        )

        self.assertEqual(
            call_kwargs["mode"],
            "general",
        )
