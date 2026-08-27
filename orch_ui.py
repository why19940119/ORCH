from collections import Counter, deque
from pathlib import Path
import json
import os
import re
import secrets
import time

from flask import (
    Flask,
    abort,
    render_template_string,
    request,
    session,
)

from orch_chat import (
    ChatProviderError,
    ask_orch,
)
from chat_security import (
    publish_chat_audit_artifact,
    record_chat_usage,
    sha256_value,
)


PROJECT_ROOT = Path(__file__).resolve().parent

QUEUE_FILE = PROJECT_ROOT / "task_queue.json"
STATUS_FILE = PROJECT_ROOT / "state" / "task_status.json"
EVENTS_FILE = PROJECT_ROOT / "state" / "events.jsonl"

ARTIFACTS_ROOT = PROJECT_ROOT / "artifacts"
LATEST_DIR = ARTIFACTS_ROOT / "latest"
MANIFESTS_DIR = ARTIFACTS_ROOT / "manifests"

LOGICAL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

app = Flask(__name__)

app.secret_key = os.getenv(
    "ORCH_UI_SECRET_KEY",
    secrets.token_urlsafe(32),
)

app.config["TRUSTED_HOSTS"] = [
    "127.0.0.1",
    "localhost",
]

CHAT_MAX_HISTORY = 8
CHAT_MIN_INTERVAL_SECONDS = 3
CHAT_SESSIONS = {}


BASE_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta
    name="viewport"
    content="width=device-width, initial-scale=1"
  >
  <title>{{ title }} · ORCH Operator Console</title>
  <style>
    :root {
      --bg: #0b1220;
      --panel: #121d2e;
      --panel-2: #18263b;
      --text: #e7edf5;
      --muted: #9eb0c5;
      --line: #2c3d55;
      --blue: #4ca8ff;
      --green: #47d18c;
      --yellow: #ffcd57;
      --red: #ff7777;
      --purple: #bf8cff;
    }

    * { box-sizing: border-box; }

    body {
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont,
        "Segoe UI", sans-serif;
      margin: 0;
    }

    header {
      background: #0e1929;
      border-bottom: 1px solid var(--line);
      padding: 18px 28px;
    }

    header h1 {
      font-size: 19px;
      letter-spacing: .2px;
      margin: 0 0 10px;
    }

    nav a {
      color: var(--muted);
      font-size: 14px;
      margin-right: 18px;
      text-decoration: none;
    }

    nav a:hover,
    nav a.active {
      color: var(--blue);
    }

    main {
      margin: 0 auto;
      max-width: 1280px;
      padding: 26px 28px 44px;
    }

    h2 {
      font-size: 22px;
      margin: 0 0 8px;
    }

    h3 {
      color: #cbd9e9;
      font-size: 15px;
      margin: 0 0 12px;
    }

    .subtitle {
      color: var(--muted);
      font-size: 14px;
      margin: 0 0 24px;
    }

    .grid {
      display: grid;
      gap: 14px;
      grid-template-columns: repeat(
        auto-fit,
        minmax(150px, 1fr)
      );
      margin-bottom: 24px;
    }

    .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 16px;
    }

    .metric-label {
      color: var(--muted);
      display: block;
      font-size: 12px;
      margin-bottom: 8px;
      text-transform: uppercase;
    }

    .metric-value {
      font-size: 28px;
      font-weight: 700;
    }

    .section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 10px;
      margin-bottom: 18px;
      padding: 18px;
    }

    table {
      border-collapse: collapse;
      font-size: 13px;
      width: 100%;
    }

    th {
      color: var(--muted);
      font-size: 11px;
      font-weight: 600;
      letter-spacing: .4px;
      text-align: left;
      text-transform: uppercase;
    }

    th, td {
      border-bottom: 1px solid var(--line);
      padding: 11px 9px;
      vertical-align: top;
    }

    tr:last-child td {
      border-bottom: 0;
    }

    a {
      color: var(--blue);
      text-decoration: none;
    }

    a:hover {
      text-decoration: underline;
    }

    .badge {
      border-radius: 999px;
      display: inline-block;
      font-size: 11px;
      font-weight: 700;
      padding: 4px 8px;
      text-transform: uppercase;
    }

    .done { background: #153d2d; color: var(--green); }
    .todo { background: #263b55; color: #a9d2ff; }
    .running { background: #25395a; color: var(--blue); }
    .retrying { background: #4a3d16; color: var(--yellow); }
    .waiting_approval {
      background: #4a3d16;
      color: var(--yellow);
    }
    .approved { background: #263b55; color: #a9d2ff; }
    .blocked { background: #4a2323; color: var(--red); }
    .failed { background: #4a2323; color: var(--red); }
    .unknown { background: #303948; color: var(--muted); }

    .kv {
      display: grid;
      gap: 9px 18px;
      grid-template-columns: 210px 1fr;
      margin: 0;
    }

    .kv dt {
      color: var(--muted);
      font-size: 12px;
    }

    .kv dd {
      margin: 0;
      overflow-wrap: anywhere;
    }

    .empty {
      color: var(--muted);
      font-size: 14px;
      padding: 12px 0;
    }

    .event {
      border-bottom: 1px solid var(--line);
      padding: 11px 0;
    }

    .event:last-child {
      border-bottom: 0;
    }

    .event-time {
      color: var(--muted);
      font-size: 12px;
    }

    .event-name {
      color: var(--purple);
      font-size: 12px;
      font-weight: 700;
      margin: 0 8px;
      text-transform: uppercase;
    }

    .warning {
      border-left: 3px solid var(--yellow);
      color: #f6d78d;
      font-size: 13px;
      padding: 9px 12px;
    }

    .chat-history {
      display: flex;
      flex-direction: column;
      gap: 12px;
      margin-bottom: 18px;
    }

    .chat-message {
      border: 1px solid var(--line);
      border-radius: 9px;
      max-width: 88%;
      padding: 12px;
      white-space: pre-wrap;
    }

    .chat-user {
      align-self: flex-end;
      background: #17365d;
    }

    .chat-assistant {
      align-self: flex-start;
      background: var(--panel-2);
    }

    .chat-meta {
      color: var(--muted);
      font-size: 11px;
      margin-bottom: 7px;
      text-transform: uppercase;
    }

    textarea,
    select {
      background: #0b1422;
      border: 1px solid var(--line);
      border-radius: 7px;
      color: var(--text);
      font: inherit;
      padding: 10px;
      width: 100%;
    }

    textarea {
      min-height: 105px;
      resize: vertical;
    }

    button {
      background: var(--blue);
      border: 0;
      border-radius: 7px;
      color: #071321;
      cursor: pointer;
      font-weight: 700;
      margin-top: 10px;
      padding: 10px 15px;
    }

    button:hover {
      background: #82c4ff;
    }

    .chat-error {
      background: #4a2323;
      border-left: 3px solid var(--red);
      color: #ffc1c1;
      margin-bottom: 14px;
      padding: 10px 12px;
    }

    code {
      background: #0b1422;
      border: 1px solid #23344a;
      border-radius: 4px;
      color: #c7e0ff;
      font-size: 12px;
      padding: 2px 5px;
    }

    @media (max-width: 720px) {
      main { padding: 20px 14px; }
      .kv { grid-template-columns: 1fr; }
      table { min-width: 720px; }
      .table-wrap { overflow-x: auto; }
    }
  </style>
</head>
<body>
  <header>
    <h1>ORCH · Local Operator Console</h1>
    <nav>
      <a href="/" class="{{ 'active' if active == 'dashboard' }}">
        Dashboard
      </a>
      <a href="/tasks" class="{{ 'active' if active == 'tasks' }}">
        Tasks
      </a>
      <a href="/events" class="{{ 'active' if active == 'events' }}">
        Events
      </a>
      <a href="/artifacts" class="{{ 'active' if active == 'artifacts' }}">
        Artifacts
      </a>
      <a href="/chat" class="{{ 'active' if active == 'chat' }}">
        Chat
      </a>
    </nav>
  </header>
  <main>
    {{ body|safe }}
  </main>
</body>
</html>
"""


def load_json(path, default_value):
    if not path.exists():
        return default_value

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default_value


def load_tasks():
    tasks = load_json(QUEUE_FILE, [])
    return sorted(
        tasks,
        key=lambda task: task.get("priority", 999),
    )


def load_statuses():
    return load_json(STATUS_FILE, {})


def load_events(limit=100):
    if not EVENTS_FILE.exists():
        return []

    events = deque(maxlen=limit)

    for line in EVENTS_FILE.read_text(
        encoding="utf-8"
    ).splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    return list(reversed(events))


def task_view(task, statuses):
    task_id = task["id"]
    state = statuses.get(task_id, {})
    status = state.get("status", "todo")

    return {
        "id": task_id,
        "title": task.get("title", ""),
        "priority": task.get("priority", 999),
        "command": task.get("command", []),
        "depends_on": task.get("depends_on", []),
        "requires_approval": task.get(
            "requires_approval",
            False,
        ),
        "requires_policies": task.get(
            "requires_policies",
            [],
        ),
        "advisory_enabled": bool(
            task.get("advisory_preflight")
        ),
        "state": state,
        "status": status,
        "attempt": state.get("attempt", 0),
    }


def advisory_view(state):
    stored = state.get("advisory_preflight", {})

    if not isinstance(stored, dict):
        return None

    preflight = stored.get("preflight", {})
    advisory = preflight.get("advisory", {})

    if not isinstance(preflight, dict):
        preflight = {}

    if not isinstance(advisory, dict):
        advisory = {}

    if not stored:
        return None

    return {
        "status": stored.get("status"),
        "reason": stored.get("reason"),
        "provider": preflight.get("provider"),
        "model": preflight.get("response_model"),
        "response_id": preflight.get("response_id"),
        "recommended_action": advisory.get(
            "recommended_action"
        ),
        "confidence": advisory.get("confidence"),
        "summary": advisory.get("summary"),
        "risks": advisory.get("risks", []),
        "artifact_id": stored.get(
            "artifact",
            {},
        ).get("artifact_id"),
        "snapshot_fingerprint": stored.get(
            "snapshot",
            {},
        ).get("snapshot_fingerprint"),
        "execution_authority": stored.get(
            "execution_authority"
        ),
    }


TASK_ID_REFERENCE_PATTERN = re.compile(
    r"\btask_[A-Za-z0-9_-]+\b"
)


def extract_task_id_references(question):
    if not isinstance(question, str):
        return []

    seen = set()
    task_ids = []

    for task_id in TASK_ID_REFERENCE_PATTERN.findall(
        question
    ):
        if task_id not in seen:
            seen.add(task_id)
            task_ids.append(task_id)

    return task_ids


def compact_text(value, maximum_length):
    if not isinstance(value, str):
        return None

    value = value.strip()

    if len(value) <= maximum_length:
        return value

    return value[:maximum_length - 1] + "…"


def chat_task_summary(task, statuses):
    view = task_view(task, statuses)
    state = view["state"]
    advisory = advisory_view(state)

    return {
        "id": view["id"],
        "title": compact_text(view["title"], 120),
        "priority": view["priority"],
        "status": view["status"],
        "attempt": view["attempt"],
        "requires_approval": view["requires_approval"],
        "approval_status": state.get("approval_status"),
        "block_reason": compact_text(
            state.get("block_reason"),
            240,
        ),
        "policy_results": [
            {
                "policy_id": result.get("policy_id"),
                "status": result.get("status"),
                "reason": compact_text(
                    result.get("reason"),
                    240,
                ),
            }
            for result in state.get(
                "policy_results",
                [],
            )
        ],
        "advisory": (
            {
                "status": advisory["status"],
                "recommended_action": advisory[
                    "recommended_action"
                ],
                "confidence": advisory["confidence"],
                "artifact_id": advisory["artifact_id"],
                "snapshot_fingerprint": advisory[
                    "snapshot_fingerprint"
                ],
                "execution_authority": advisory[
                    "execution_authority"
                ],
            }
            if advisory
            else None
        ),
    }


def build_chat_context(question=""):
    statuses = load_statuses()
    source_tasks = load_tasks()
    requested_task_ids = extract_task_id_references(question)
    task_by_id = {
        task.get("id"): task
        for task in source_tasks
        if isinstance(task.get("id"), str)
    }

    matching_tasks = [
        chat_task_summary(task_by_id[task_id], statuses)
        for task_id in requested_task_ids
        if task_id in task_by_id
    ]

    resolved_task_ids = [
        task["id"]
        for task in matching_tasks
    ]

    unresolved_task_ids = [
        task_id
        for task_id in requested_task_ids
        if task_id not in task_by_id
    ]

    tasks = [
        chat_task_summary(task, statuses)
        for task in source_tasks
        if task.get("id") not in resolved_task_ids
    ][:12]

    source_events = load_events(100)

    matching_events = [
        {
            "timestamp": event.get("timestamp"),
            "event": event.get("event"),
            "task_id": event.get("task_id"),
            "message": compact_text(
                event.get("message"),
                240,
            ),
        }
        for event in source_events
        if event.get("task_id") in resolved_task_ids
    ][:10]

    latest_events = [
        {
            "timestamp": event.get("timestamp"),
            "event": event.get("event"),
            "task_id": event.get("task_id"),
            "message": compact_text(
                event.get("message"),
                160,
            ),
        }
        for event in source_events[:10]
    ]

    return {
        "context_version": "1.1",
        "scope": "read_only_operator_summary",
        "task_lookup": {
            "lookup_type": (
                "exact_task_id"
                if requested_task_ids
                else "summary_only"
            ),
            "requested_task_ids": requested_task_ids,
            "resolved_task_ids": resolved_task_ids,
            "unresolved_task_ids": unresolved_task_ids,
            "matching_tasks": matching_tasks,
            "matching_events": matching_events,
        },
        "tasks": tasks,
        "latest_events": latest_events,
        "limitations": [
            "Task lookup uses exact task_id matches only.",
            "No raw artifact payloads are included.",
            "No environment variables are included.",
            "No API keys are included.",
            "No command execution authority exists.",
        ],
    }


def get_chat_session():
    chat_id = session.get("chat_id")

    if not chat_id:
        chat_id = secrets.token_urlsafe(16)
        session["chat_id"] = chat_id

    return CHAT_SESSIONS.setdefault(chat_id, [])


def get_csrf_token():
    token = session.get("csrf_token")

    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token

    return token


def artifact_views():
    if not LATEST_DIR.exists():
        return []

    artifacts = []

    for pointer_path in sorted(LATEST_DIR.glob("*.json")):
        pointer = load_json(pointer_path, {})

        artifacts.append(
            {
                "logical_name": pointer.get(
                    "logical_name",
                    pointer_path.stem,
                ),
                "artifact_id": pointer.get("artifact_id"),
                "content_sha256": pointer.get(
                    "content_sha256"
                ),
                "updated_at_utc": pointer.get(
                    "updated_at_utc"
                ),
            }
        )

    return artifacts


def render_page(title, active, body_template, **context):
    body = render_template_string(
        body_template,
        **context,
    )

    return render_template_string(
        BASE_TEMPLATE,
        title=title,
        active=active,
        body=body,
    )


@app.get("/")
def dashboard():
    tasks = load_tasks()
    statuses = load_statuses()
    views = [
        task_view(task, statuses)
        for task in tasks
    ]

    counts = Counter(view["status"] for view in views)

    dashboard_template = """
      <h2>Dashboard</h2>
      <p class="subtitle">
        Read-only local view of ORCH task state and audit evidence.
      </p>

      <div class="grid">
        <div class="card">
          <span class="metric-label">Total Tasks</span>
          <span class="metric-value">{{ views|length }}</span>
        </div>
        <div class="card">
          <span class="metric-label">Done</span>
          <span class="metric-value">{{ counts.get('done', 0) }}</span>
        </div>
        <div class="card">
          <span class="metric-label">Waiting Approval</span>
          <span class="metric-value">
            {{ counts.get('waiting_approval', 0) }}
          </span>
        </div>
        <div class="card">
          <span class="metric-label">Blocked</span>
          <span class="metric-value">
            {{ counts.get('blocked', 0) }}
          </span>
        </div>
        <div class="card">
          <span class="metric-label">Failed</span>
          <span class="metric-value">
            {{ counts.get('failed', 0) }}
          </span>
        </div>
      </div>

      <div class="section">
        <h3>Operator Boundary</h3>
        <div class="warning">
          This UI is read-only. It has no approve, run, retry,
          delete, command-input, connector-write, or AI-call action.
        </div>
      </div>

      <div class="section">
        <h3>Latest Events</h3>
        {% if events %}
          {% for event in events[:10] %}
            <div class="event">
              <span class="event-time">
                {{ event.get('timestamp', '') }}
              </span>
              <span class="event-name">
                {{ event.get('event', '') }}
              </span>
              <a href="/tasks/{{ event.get('task_id', '') }}">
                {{ event.get('task_id', '') }}
              </a>
              — {{ event.get('message', '') }}
            </div>
          {% endfor %}
        {% else %}
          <div class="empty">No events recorded.</div>
        {% endif %}
      </div>
    """

    return render_page(
        "Dashboard",
        "dashboard",
        dashboard_template,
        views=views,
        counts=counts,
        events=load_events(20),
    )


@app.get("/tasks")
def tasks_page():
    statuses = load_statuses()

    task_template = """
      <h2>Tasks</h2>
      <p class="subtitle">
        Task definitions joined with current runtime state.
      </p>

      <div class="section table-wrap">
        <table>
          <thead>
            <tr>
              <th>Priority</th>
              <th>Task</th>
              <th>Status</th>
              <th>Attempt</th>
              <th>Approval</th>
              <th>Advisory</th>
            </tr>
          </thead>
          <tbody>
            {% for task in tasks %}
              <tr>
                <td>{{ task.priority }}</td>
                <td>
                  <a href="/tasks/{{ task.id }}">{{ task.id }}</a><br>
                  {{ task.title }}
                </td>
                <td>
                  <span class="badge {{ task.status }}">
                    {{ task.status }}
                  </span>
                </td>
                <td>{{ task.attempt }}</td>
                <td>
                  {% if task.requires_approval %}
                    {{ task.state.get(
                      'approval_status',
                      'waiting_approval'
                    ) }}
                  {% else %}
                    not required
                  {% endif %}
                </td>
                <td>
                  {% if task.advisory_enabled %}
                    enabled
                  {% else %}
                    not enabled
                  {% endif %}
                </td>
              </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    """

    return render_page(
        "Tasks",
        "tasks",
        task_template,
        tasks=[
            task_view(task, statuses)
            for task in load_tasks()
        ],
    )


@app.get("/tasks/<task_id>")
def task_detail(task_id):
    statuses = load_statuses()

    task = next(
        (
            item
            for item in load_tasks()
            if item.get("id") == task_id
        ),
        None,
    )

    if task is None:
        abort(404)

    view = task_view(task, statuses)
    advisory = advisory_view(view["state"])

    detail_template = """
      <h2>{{ task.id }}</h2>
      <p class="subtitle">{{ task.title }}</p>

      <div class="section">
        <h3>Task State</h3>
        <dl class="kv">
          <dt>Status</dt>
          <dd>
            <span class="badge {{ task.status }}">
              {{ task.status }}
            </span>
          </dd>

          <dt>Priority</dt>
          <dd>{{ task.priority }}</dd>

          <dt>Attempt</dt>
          <dd>{{ task.attempt }}</dd>

          <dt>Command</dt>
          <dd>
            {% for item in task.command %}
              <code>{{ item }}</code>
            {% endfor %}
          </dd>

          <dt>Dependencies</dt>
          <dd>
            {{ task.depends_on|join(', ') or 'none' }}
          </dd>

          <dt>Approval</dt>
          <dd>
            {% if task.requires_approval %}
              {{ task.state.get(
                'approval_status',
                'waiting_approval'
              ) }}
            {% else %}
              not required
            {% endif %}
          </dd>

          <dt>Updated At</dt>
          <dd>{{ task.state.get('updated_at', 'not recorded') }}</dd>

          <dt>Block Reason</dt>
          <dd>{{ task.state.get('block_reason', 'none') }}</dd>
        </dl>
      </div>

      <div class="section">
        <h3>Policies</h3>
        {% if task.state.get('policy_results') %}
          <dl class="kv">
            {% for result in task.state.get('policy_results', []) %}
              <dt>{{ result.get('policy_id', 'unknown') }}</dt>
              <dd>
                {{ result.get('status', 'unknown') }}
                — {{ result.get('reason', '') }}
              </dd>
            {% endfor %}
          </dl>
        {% else %}
          <div class="empty">No policy evaluation recorded.</div>
        {% endif %}
      </div>

      <div class="section">
        <h3>Advisory Preflight</h3>
        {% if advisory %}
          <dl class="kv">
            <dt>Preflight Status</dt>
            <dd>{{ advisory.status }}</dd>

            <dt>Recommended Action</dt>
            <dd>{{ advisory.recommended_action or 'not available' }}</dd>

            <dt>Confidence</dt>
            <dd>{{ advisory.confidence or 'not available' }}</dd>

            <dt>Summary</dt>
            <dd>{{ advisory.summary or advisory.reason }}</dd>

            <dt>Risks</dt>
            <dd>{{ advisory.risks|join(', ') or 'none recorded' }}</dd>

            <dt>Provider / Model</dt>
            <dd>
              {{ advisory.provider or 'not available' }}
              /
              {{ advisory.model or 'not available' }}
            </dd>

            <dt>Response ID</dt>
            <dd>{{ advisory.response_id or 'not available' }}</dd>

            <dt>Artifact ID</dt>
            <dd>{{ advisory.artifact_id or 'not available' }}</dd>

            <dt>Snapshot Fingerprint</dt>
            <dd>
              {{ advisory.snapshot_fingerprint or 'not available' }}
            </dd>

            <dt>Execution Authority</dt>
            <dd>
              {{ advisory.execution_authority or 'not available' }}
            </dd>
          </dl>
        {% else %}
          <div class="empty">
            No advisory preflight is stored for this task.
          </div>
        {% endif %}
      </div>
    """

    return render_page(
        f"Task {task_id}",
        "tasks",
        detail_template,
        task=view,
        advisory=advisory,
    )


@app.get("/events")
def events_page():
    events_template = """
      <h2>Events</h2>
      <p class="subtitle">
        Latest records from state/events.jsonl.
      </p>

      <div class="section">
        {% if events %}
          {% for event in events %}
            <div class="event">
              <span class="event-time">
                {{ event.get('timestamp', '') }}
              </span>
              <span class="event-name">
                {{ event.get('event', '') }}
              </span>
              <a href="/tasks/{{ event.get('task_id', '') }}">
                {{ event.get('task_id', '') }}
              </a>
              — {{ event.get('message', '') }}
            </div>
          {% endfor %}
        {% else %}
          <div class="empty">No events recorded.</div>
        {% endif %}
      </div>
    """

    return render_page(
        "Events",
        "events",
        events_template,
        events=load_events(100),
    )


@app.get("/artifacts")
def artifacts_page():
    artifact_template = """
      <h2>Artifacts</h2>
      <p class="subtitle">
        Read-only latest pointers under artifacts/latest/.
      </p>

      <div class="section table-wrap">
        <table>
          <thead>
            <tr>
              <th>Logical Name</th>
              <th>Artifact ID</th>
              <th>Content SHA-256</th>
              <th>Updated At</th>
            </tr>
          </thead>
          <tbody>
            {% for artifact in artifacts %}
              <tr>
                <td>
                  <a href="/artifacts/{{ artifact.logical_name }}">
                    {{ artifact.logical_name }}
                  </a>
                </td>
                <td>{{ artifact.artifact_id or 'not available' }}</td>
                <td>{{ artifact.content_sha256 or 'not available' }}</td>
                <td>{{ artifact.updated_at_utc or 'not available' }}</td>
              </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    """

    return render_page(
        "Artifacts",
        "artifacts",
        artifact_template,
        artifacts=artifact_views(),
    )


@app.get("/artifacts/<logical_name>")
def artifact_detail(logical_name):
    if not LOGICAL_NAME_PATTERN.fullmatch(logical_name):
        abort(404)

    pointer_path = LATEST_DIR / f"{logical_name}.json"

    if not pointer_path.exists():
        abort(404)

    pointer = load_json(pointer_path, {})
    manifest_path = PROJECT_ROOT / pointer.get(
        "manifest_path",
        "",
    )
    manifest = load_json(manifest_path, {})

    detail_template = """
      <h2>{{ logical_name }}</h2>
      <p class="subtitle">
        Immutable artifact metadata. Raw payload is intentionally
        not rendered in this read-only UI.
      </p>

      <div class="section">
        <dl class="kv">
          <dt>Artifact ID</dt>
          <dd>{{ manifest.get('artifact_id', 'not available') }}</dd>

          <dt>Logical Name</dt>
          <dd>{{ manifest.get('logical_name', logical_name) }}</dd>

          <dt>Content SHA-256</dt>
          <dd>{{ manifest.get('content_sha256', 'not available') }}</dd>

          <dt>Byte Size</dt>
          <dd>{{ manifest.get('byte_size', 'not available') }}</dd>

          <dt>Schema Version</dt>
          <dd>{{ manifest.get('schema_version', 'not available') }}</dd>

          <dt>Producer Task</dt>
          <dd>{{ manifest.get('producer_task_id', 'not available') }}</dd>

          <dt>Created At</dt>
          <dd>{{ manifest.get('created_at_utc', 'not available') }}</dd>

          <dt>Immutable</dt>
          <dd>{{ manifest.get('immutable', 'not available') }}</dd>
        </dl>
      </div>
    """

    return render_page(
        f"Artifact {logical_name}",
        "artifacts",
        detail_template,
        logical_name=logical_name,
        pointer=pointer,
        manifest=manifest,
    )


@app.route("/chat", methods=["GET", "POST"])
def chat_page():
    history = get_chat_session()
    csrf_token = get_csrf_token()
    error = None

    if request.method == "POST":
        submitted_token = request.form.get(
            "csrf_token",
            "",
        )

        if not secrets.compare_digest(
            csrf_token,
            submitted_token,
        ):
            abort(400)

        question = request.form.get("question", "").strip()
        mode = request.form.get("mode", "general")

        if mode not in {"general", "orch_context"}:
            abort(400)

        last_chat_at = session.get("last_chat_at", 0)

        if (
            time.time() - last_chat_at
            < CHAT_MIN_INTERVAL_SECONDS
        ):
            error = (
                "Please wait a few seconds before sending "
                "another chat request."
            )
        else:
            try:
                context = (
                    build_chat_context(question)
                    if mode == "orch_context"
                    else {}
                )

                session_id_sha256 = sha256_value(
                    session["chat_id"]
                )

                result = ask_orch(
                    question=question,
                    mode=mode,
                    context=context,
                    history=history,
                )

            except ChatProviderError as error_value:
                error = str(error_value)

            except Exception:
                error = (
                    "Chat request failed before an answer "
                    "could be safely recorded."
                )

            else:
                try:
                    chat = result["chat"]

                    audit_artifact = (
                        publish_chat_audit_artifact(
                            session_id_sha256=session_id_sha256,
                            turn_number=(
                                len(history) // 2
                            ) + 1,
                            mode=mode,
                            question=question,
                            answer=chat["answer"],
                            provider_result=result,
                            context=context,
                        )
                    )

                    record_chat_usage(
                        provider_result=result,
                        session_id_sha256=session_id_sha256,
                    )

                except Exception:
                    error = (
                        "Chat answer was not displayed because "
                        "its audit record could not be completed."
                    )

                else:
                    history.append(
                        {
                            "role": "user",
                            "content": question,
                            "mode": mode,
                        }
                    )

                    history.append(
                        {
                            "role": "assistant",
                            "content": chat["answer"],
                            "mode": mode,
                            "metadata": {
                                "provider": result["provider"],
                                "model": result[
                                    "response_model"
                                ],
                                "response_id": result[
                                    "response_id"
                                ],
                                "referenced_task_ids": chat[
                                    "referenced_task_ids"
                                ],
                                "referenced_artifact_ids": chat[
                                    "referenced_artifact_ids"
                                ],
                                "audit_artifact_id": (
                                    audit_artifact[
                                        "artifact_id"
                                    ]
                                ),
                                "execution_authority": chat[
                                    "execution_authority"
                                ],
                            },
                        }
                    )

                    del history[:-CHAT_MAX_HISTORY]
                    session["last_chat_at"] = time.time()

    chat_template = """
      <h2>ORCH Chat</h2>
      <p class="subtitle">
        Advisory-only chat. It cannot approve, run, retry, create,
        modify, delete, or dispatch any ORCH task.
      </p>

      <div class="section">
        <div class="warning">
          Chat execution authority is permanently <code>none</code>.
          Each submitted question creates at most one OpenRouter
          request. Chat history stays only in this running UI process.
        </div>
      </div>

      {% if error %}
        <div class="chat-error">{{ error }}</div>
      {% endif %}

      <div class="section">
        <h3>Conversation</h3>

        {% if history %}
          <div class="chat-history">
            {% for message in history %}
              <div class="chat-message chat-{{ message.role }}">
                <div class="chat-meta">
                  {{ message.role }}
                  {% if message.mode %}
                    · {{ message.mode }}
                  {% endif %}
                </div>

                {{ message.content }}

                {% if message.role == 'assistant'
                      and message.metadata %}
                  <div class="chat-meta" style="margin-top: 10px;">
                    {{ message.metadata.provider }}
                    · {{ message.metadata.model }}
                    · authority:
                    {{ message.metadata.execution_authority }}
                    · audit:
                    {{ message.metadata.audit_artifact_id }}
                    · daily cost:
                    {{ message.metadata.daily_cost_usd }}
                  </div>
                {% endif %}
              </div>
            {% endfor %}
          </div>
        {% else %}
          <div class="empty">
            Start a read-only conversation with ORCH Chat.
          </div>
        {% endif %}
      </div>

      <div class="section">
        <h3>Ask a Question</h3>

        <form method="post" action="/chat">
          <input
            type="hidden"
            name="csrf_token"
            value="{{ csrf_token }}"
          >

          <label for="mode">Mode</label>
          <select id="mode" name="mode">
            <option value="general">
              General Chat
            </option>
            <option value="orch_context" selected>
              ORCH Context Chat
            </option>
          </select>

          <p class="subtitle" style="margin-top: 10px;">
            ORCH Context Chat receives only an allowlisted summary of
            task state, policy results, advisory metadata, and recent
            events. It does not receive API keys, environment
            variables, raw artifact payloads, or write authority.
          </p>

          <label for="question">Question</label>
          <textarea
            id="question"
            name="question"
            maxlength="800"
            required
            placeholder="Example: Why are there currently blocked tasks?"
          ></textarea>

          <button type="submit">Ask ORCH Chat</button>
        </form>
      </div>
    """

    return render_page(
        "Chat",
        "chat",
        chat_template,
        history=history,
        csrf_token=csrf_token,
        error=error,
    )


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5050,
        debug=False,
    )
