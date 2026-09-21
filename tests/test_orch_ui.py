import os
import subprocess
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orch_ui import (
    ARTIFACTS_ROOT,
    CHAT_SESSIONS,
    PROJECT_ROOT,
    app,
    build_chat_context,
    extract_task_id_references,
    load_local_dotenv,
    resolve_contained_artifact_path,
)


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


class TaskStatusAndComposerUiTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_tasks_render_all_distinct_status_badge_classes(self):
        statuses = [
            "todo",
            "running",
            "retrying",
            "waiting_approval",
            "approved",
            "done",
            "blocked",
            "failed",
        ]
        tasks = [
            {
                "id": f"task_status_{status}",
                "title": f"{status} task",
                "priority": index,
            }
            for index, status in enumerate(statuses, start=1)
        ]
        task_states = {
            task["id"]: {"status": status}
            for task, status in zip(tasks, statuses)
        }

        with patch("orch_ui.load_tasks", return_value=tasks), patch(
            "orch_ui.load_statuses",
            return_value=task_states,
        ):
            response = self.client.get("/tasks")

        self.assertEqual(response.status_code, 200)
        for status in statuses:
            self.assertIn(
                f'class="badge {status}"'.encode(),
                response.data,
            )

    def test_status_and_composer_css_contract(self):
        source = Path("orch_ui.py").read_text(encoding="utf-8")

        for status in [
            "todo",
            "retrying",
            "waiting_approval",
            "approved",
            "blocked",
            "failed",
        ]:
            self.assertIn(f"    .{status}", source)

        self.assertIn(
            ".chat-page .chat-composer {",
            source,
        )
        self.assertIn(
            "background: var(--panel);",
            source,
        )
        self.assertIn("z-index: 50;", source)
        self.assertIn(
            "box-shadow: 0 -8px 24px rgba(5, 2, 12, .45);",
            source,
        )
        self.assertIn(
            ".chat-page .composer-grid {",
            source,
        )
        self.assertIn(
            "grid-template-columns: minmax(150px, 190px) "
            "minmax(0, 1fr) auto;",
            source,
        )

        desktop_grid = (
            "grid-template-columns: minmax(150px, 190px) "
            "minmax(0, 1fr) auto;"
        )
        desktop_idx = source.index(desktop_grid)
        after_desktop = source[desktop_idx:]
        media_marker = "@media (max-width: 720px)"
        self.assertIn(media_marker, after_desktop)
        media_idx = after_desktop.index(media_marker)
        media_tail = after_desktop[media_idx:]
        # Last composer-grid column rule under a 720px media must be 1fr
        # so the narrow layout wins the cascade over the desktop grid.
        narrow_rule = (
            ".chat-page .composer-grid {"
            + chr(10)
            + "        grid-template-columns: 1fr;"
        )
        self.assertIn(narrow_rule, media_tail)
        last_grid_1fr = media_tail.rfind(
            "grid-template-columns: 1fr;"
        )
        last_grid_minmax = media_tail.rfind(
            "grid-template-columns: minmax("
        )
        self.assertGreater(last_grid_1fr, -1)
        self.assertTrue(
            last_grid_minmax == -1 or last_grid_1fr > last_grid_minmax
        )

    def test_chat_keyboard_contract_is_preserved(self):
        source = Path("orch_ui.py").read_text(encoding="utf-8")

        self.assertIn('event.key === "Enter"', source)
        self.assertIn("!event.shiftKey", source)
        self.assertIn("event.preventDefault();", source)


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

    def test_chat_route_rejects_missing_csrf(self):
        response = self.client.post(
            "/chat",
            data={
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

    @patch("orch_ui.publish_chat_audit_artifact")
    @patch("orch_ui.record_chat_usage")
    @patch("orch_ui.ask_orch")
    def test_valid_chat_request_uses_provider_once(
        self,
        mock_ask_orch,
        mock_record_usage,
        mock_publish_audit,
    ):
        mock_publish_audit.return_value = {
            "artifact_id": "artifact_chat_audit_test",
        }

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
        mock_record_usage.assert_called_once()
        mock_publish_audit.assert_called_once()

        self.assertIn(
            b"artifact_chat_audit_test",
            response.data,
        )

        call_kwargs = mock_ask_orch.call_args.kwargs

        self.assertEqual(
            call_kwargs["question"],
            "What is ORCH?",
        )

        self.assertEqual(
            call_kwargs["mode"],
            "general",
        )


class OrchUiHostValidationTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_untrusted_host_is_rejected(self):
        response = self.client.get(
            "/tasks",
            headers={
                "Host": "evil.example",
            },
        )

        self.assertEqual(response.status_code, 400)

    def test_loopback_host_is_allowed(self):
        response = self.client.get(
            "/tasks",
            headers={
                "Host": "127.0.0.1",
            },
        )

        self.assertEqual(response.status_code, 200)


class TaskAwareChatContextTests(unittest.TestCase):
    @patch("orch_ui.load_events")
    @patch("orch_ui.load_statuses")
    @patch("orch_ui.load_tasks")
    def test_exact_task_lookup_injects_task_and_events(
        self,
        mock_load_tasks,
        mock_load_statuses,
        mock_load_events,
    ):
        mock_load_tasks.return_value = [
            {
                "id": "task_other_001",
                "title": "Other task",
                "priority": 9,
            },
            {
                "id": "task_exact_013",
                "title": "Exact lookup task",
                "priority": 1,
                "requires_approval": True,
            },
        ]

        mock_load_statuses.return_value = {
            "task_exact_013": {
                "status": "done",
                "attempt": 1,
                "approval_status": "approved",
                "policy_results": [
                    {
                        "policy_id": "policy_demo",
                        "status": "pass",
                        "reason": "Policy passed.",
                    }
                ],
            }
        }

        mock_load_events.return_value = [
            {
                "timestamp": "2026-08-27T12:00:00Z",
                "event": "task_completed",
                "task_id": "task_exact_013",
                "message": "Exact task completed.",
            },
            {
                "timestamp": "2026-08-27T11:00:00Z",
                "event": "task_created",
                "task_id": "task_other_001",
                "message": "Other task created.",
            },
        ]

        context = build_chat_context(
            "What happened to task_exact_013?"
        )

        lookup = context["task_lookup"]

        self.assertEqual(
            lookup["lookup_type"],
            "exact_task_id",
        )
        self.assertEqual(
            lookup["resolved_task_ids"],
            ["task_exact_013"],
        )
        self.assertEqual(
            lookup["matching_tasks"][0]["status"],
            "done",
        )
        self.assertEqual(
            lookup["matching_events"][0]["event"],
            "task_completed",
        )
        self.assertEqual(
            context["tasks"][0]["id"],
            "task_other_001",
        )

    def test_unknown_task_id_is_reported_unresolved(self):
        context = build_chat_context(
            "Check task_missing_404 and task_missing_404."
        )

        self.assertEqual(
            extract_task_id_references(
                "task_missing_404 task_missing_404"
            ),
            ["task_missing_404"],
        )
        self.assertEqual(
            context["task_lookup"]["unresolved_task_ids"],
            ["task_missing_404"],
        )
        self.assertEqual(
            context["task_lookup"]["resolved_task_ids"],
            [],
        )


class OrchUiSecretAndCookieTests(unittest.TestCase):
    def test_session_cookie_flags(self):
        self.assertIs(app.config["SESSION_COOKIE_HTTPONLY"], True)
        self.assertEqual(
            app.config["SESSION_COOKIE_SAMESITE"],
            "Lax",
        )
        self.assertIs(app.config["SESSION_COOKIE_SECURE"], False)

    def test_secret_key_comes_from_env(self):
        expected = "unit-test-orch-ui-secret-key"
        env = os.environ.copy()
        env["ORCH_UI_SECRET_KEY"] = expected
        code = (
            "import orch_ui; "
            "assert orch_ui.app.secret_key == "
            + repr(expected)
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            result.stderr,
        )


class ArtifactRootContainmentTests(unittest.TestCase):
    def test_accepts_path_under_artifacts_root(self):
        relative = "artifacts/manifests/safe_manifest.json"
        resolved = resolve_contained_artifact_path(relative)
        self.assertEqual(
            resolved,
            (PROJECT_ROOT / relative).resolve(),
        )
        self.assertTrue(
            str(resolved).startswith(
                str(ARTIFACTS_ROOT.resolve())
            )
        )

    def test_rejects_parent_traversal(self):
        with self.assertRaises(ValueError):
            resolve_contained_artifact_path(
                "artifacts/../.env.example"
            )

    def test_rejects_absolute_path_outside_artifacts(self):
        outsider = (PROJECT_ROOT / ".env.example").resolve()
        with self.assertRaises(ValueError):
            resolve_contained_artifact_path(str(outsider))

    def test_rejects_sibling_outside_artifacts(self):
        with self.assertRaises(ValueError):
            resolve_contained_artifact_path("orch_ui.py")

    def test_artifact_detail_rejects_escaping_pointer(self):
        client = app.test_client()
        pointer = {
            "logical_name": "escape_probe",
            "artifact_id": "artifact_escape_probe",
            "manifest_path": "../.env.example",
            "content_sha256": "sha256:test",
            "updated_at_utc": "2026-09-21T00:00:00+00:00",
        }
        pointer_path = (
            ARTIFACTS_ROOT / "latest" / "escape_probe.json"
        )
        ARTIFACTS_ROOT.joinpath("latest").mkdir(
            parents=True,
            exist_ok=True,
        )
        pointer_path.write_text(
            json.dumps(pointer),
            encoding="utf-8",
        )
        try:
            response = client.get("/artifacts/escape_probe")
            self.assertEqual(response.status_code, 404)
        finally:
            if pointer_path.exists():
                pointer_path.unlink()


class LocalDotenvTests(unittest.TestCase):
    def test_loads_missing_keys_from_dotenv(self):
        key = "ORCH_DOTENV_PROBE_LOAD"
        os.environ.pop(key, None)

        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                f"{key}=from-dotenv-file\n",
                encoding="utf-8",
            )
            load_local_dotenv(env_file)

        self.assertEqual(os.environ.get(key), "from-dotenv-file")
        os.environ.pop(key, None)

    def test_does_not_override_existing_environ(self):
        key = "ORCH_DOTENV_PROBE_OVERRIDE"
        os.environ[key] = "from-process-env"

        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                f"{key}=from-dotenv-file\n",
                encoding="utf-8",
            )
            load_local_dotenv(env_file)

        self.assertEqual(os.environ.get(key), "from-process-env")
        os.environ.pop(key, None)

    def test_secret_key_can_come_from_dotenv_file(self):
        expected = "dotenv-ui-secret-for-unit-test"
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            env_file = project / ".env"
            env_file.write_text(
                f"ORCH_UI_SECRET_KEY={expected}\n",
                encoding="utf-8",
            )
            code = (
                "from pathlib import Path\n"
                "import orch_ui\n"
                "orch_ui.load_local_dotenv(Path(%r))\n"
                "import os\n"
                "assert os.environ.get('ORCH_UI_SECRET_KEY') == %r\n"
            ) % (str(env_file), expected)
            env = os.environ.copy()
            env.pop("ORCH_UI_SECRET_KEY", None)
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=str(Path(__file__).resolve().parents[1]),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
