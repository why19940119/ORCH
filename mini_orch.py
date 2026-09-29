import sys
import shlex
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import json
import subprocess
import time

import orch_db
from advisory_dispatch import run_advisory_preflight

STATE_DIR = Path("state")
QUEUE_FILE = Path("task_queue.json")
STATUS_FILE = STATE_DIR / "task_status.json"
EVENTS_FILE = STATE_DIR / "events.jsonl"
# v0.18.2: e-commerce demo drafts live in a separate gitignored queue.
DEMO_QUEUE_FILE = STATE_DIR / "ecom_demo_queue.json"
# Shared with commerce_demo.demo_lock (UI) so status writes never race.
LOCK_FILE = STATE_DIR / ".ecom_demo.lock"
DEMO_TASK_PREFIX = "task_ecom_"


@contextmanager
def state_lock(lock_file=None):
    """v0.21.0: one SQLite write transaction (BEGIN IMMEDIATE) on the state
    DB next to ``lock_file`` (was an flock). Re-entrant per thread; every
    load/save/event inside commits or rolls back together."""
    # Default: the DB that holds STATUS_FILE (read at call time, so a
    # patched STATUS_FILE never locks the repository's own state/orch.db).
    lock_path = Path(lock_file) if lock_file is not None else STATUS_FILE
    with orch_db.transaction(lock_path):
        yield


def load_all_tasks(queue_file=None, demo_queue_file=None):
    """task_queue.json plus the demo queue, de-duplicated by task id
    (a demo task copied from a legacy task_queue.json appears once)."""
    main_tasks = load_json(Path(queue_file or QUEUE_FILE), [])
    demo_path = Path(demo_queue_file or DEMO_QUEUE_FILE)
    demo_tasks = load_json(demo_path, [])
    seen = set()
    merged = []
    for task in list(demo_tasks) + list(main_tasks):
        task_id = task.get("id")
        if task_id in seen:
            continue
        seen.add(task_id)
        merged.append(task)
    return merged


def save_task_status(statuses, task_id):
    """Locked read-merge-write of one task's state (v0.18.2).

    Only ``task_id`` is written, so concurrent UI changes to other tasks
    (new demo drafts, approvals) are never clobbered by a queue run.
    """
    with state_lock():
        disk = load_json(STATUS_FILE, {})
        if task_id in statuses:
            disk[task_id] = statuses[task_id]
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        save_json(STATUS_FILE, disk)
    for key, value in disk.items():
        if key != task_id:
            statuses[key] = value


def now():
    return datetime.now().isoformat(timespec="seconds")


def load_json(file_path, default_value):
    # v0.21.0: state names (task_status.json, ecom_demo_queue.json, ...)
    # live in state/orch.db; other paths (task_queue.json) stay files.
    if orch_db.is_managed(file_path):
        return orch_db.load(file_path, default_value)
    if not file_path.exists():
        return default_value

    return json.loads(file_path.read_text(encoding="utf-8"))


def save_json(file_path, data):
    if orch_db.is_managed(file_path):
        orch_db.save(file_path, data)
        return
    file_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_event(event, task, message, events_file=None, extra=None):
    record = {
        "timestamp": now(),
        "event": event,
        "task_id": task["id"],
        "task_title": task["title"],
        "message": message,
    }

    if extra:
        for key, value in extra.items():
            record.setdefault(key, value)

    target = Path(events_file) if events_file is not None else EVENTS_FILE
    if orch_db.is_managed(target):
        orch_db.append(target, record)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"[{record['timestamp']}] {event}: {task['id']} - {message}")


def get_task_state(task, statuses):
    return statuses.get(
        task["id"],
        {
            "id": task["id"],
            "title": task["title"],
            "status": "todo",
            "attempt": 0,
        },
    )


def dependencies_completed(task, statuses):
    for dependency_id in task.get("depends_on", []):
        dependency_status = statuses.get(dependency_id, {}).get("status")

        if dependency_status != "done":
            return False

    return True


def evaluate_artifact_exists(policy):
    artifact = policy.get("artifact")

    if not isinstance(artifact, str) or not artifact.strip():
        return {
            "status": "blocked",
            "reason": "artifact-exists policy requires a non-empty artifact path.",
        }

    artifact_path = Path(artifact)

    if artifact_path.is_absolute():
        return {
            "status": "blocked",
            "reason": "artifact-exists policy does not allow absolute paths.",
        }

    project_root = Path.cwd().resolve()
    resolved_path = (project_root / artifact_path).resolve()

    if not resolved_path.is_relative_to(project_root):
        return {
            "status": "blocked",
            "reason": "artifact-exists policy path escapes the project directory.",
        }

    if not resolved_path.exists():
        return {
            "status": "blocked",
            "artifact": str(artifact_path),
            "reason": f"Required artifact does not exist: {artifact_path}",
        }

    if not resolved_path.is_file():
        return {
            "status": "blocked",
            "artifact": str(artifact_path),
            "reason": f"Required artifact is not a file: {artifact_path}",
        }

    return {
        "status": "allowed",
        "artifact": str(artifact_path),
        "reason": "Required artifact exists.",
    }


def evaluate_json_field_equals(policy):
    artifact = policy.get("artifact")
    field = policy.get("field")

    if not isinstance(artifact, str) or not artifact.strip():
        return {
            "status": "blocked",
            "reason": (
                "json-field-equals policy requires a non-empty "
                "artifact path."
            ),
        }

    if not isinstance(field, str) or not field.strip():
        return {
            "status": "blocked",
            "artifact": artifact,
            "reason": (
                "json-field-equals policy requires a non-empty "
                "field path."
            ),
        }

    if "equals" not in policy:
        return {
            "status": "blocked",
            "artifact": artifact,
            "reason": (
                "json-field-equals policy requires an equals value."
            ),
        }

    artifact_path = Path(artifact)

    if artifact_path.is_absolute():
        return {
            "status": "blocked",
            "reason": (
                "json-field-equals policy does not allow absolute paths."
            ),
        }

    project_root = Path.cwd().resolve()
    resolved_path = (project_root / artifact_path).resolve()

    if not resolved_path.is_relative_to(project_root):
        return {
            "status": "blocked",
            "reason": (
                "json-field-equals policy path escapes the project "
                "directory."
            ),
        }

    if not resolved_path.exists():
        return {
            "status": "blocked",
            "artifact": str(artifact_path),
            "reason": f"Required JSON artifact does not exist: {artifact_path}",
        }

    if not resolved_path.is_file():
        return {
            "status": "blocked",
            "artifact": str(artifact_path),
            "reason": f"Required JSON artifact is not a file: {artifact_path}",
        }

    try:
        document = json.loads(
            resolved_path.read_text(encoding="utf-8")
        )
    except json.JSONDecodeError as error:
        return {
            "status": "blocked",
            "artifact": str(artifact_path),
            "reason": f"Required artifact is invalid JSON: {error}",
        }

    value = document

    for field_part in field.split("."):
        if not isinstance(value, dict) or field_part not in value:
            return {
                "status": "blocked",
                "artifact": str(artifact_path),
                "reason": (
                    "Required JSON field does not exist: "
                    f"{field}"
                ),
            }

        value = value[field_part]

    if value != policy["equals"]:
        return {
            "status": "blocked",
            "artifact": str(artifact_path),
            "reason": (
                f"Required JSON field does not match expected value: "
                f"{field}"
            ),
        }

    return {
        "status": "allowed",
        "artifact": str(artifact_path),
        "reason": (
            "Required JSON field matches expected value: "
            f"{field}"
        ),
    }


POLICY_EVALUATORS = {
    "artifact-exists": evaluate_artifact_exists,
    "json-field-equals": evaluate_json_field_equals,
}


def evaluate_required_policies(task):
    required_policies = task.get("requires_policies", [])

    if not isinstance(required_policies, list):
        return False, [
            {
                "status": "blocked",
                "reason": "requires_policies must be a list.",
            }
        ]

    results = []

    for policy in required_policies:
        if not isinstance(policy, dict):
            results.append(
                {
                    "status": "blocked",
                    "reason": "Each policy requirement must be an object.",
                }
            )
            continue

        policy_id = policy.get("id")
        evaluator = POLICY_EVALUATORS.get(policy_id)

        if evaluator is None:
            results.append(
                {
                    "policy_id": policy_id,
                    "status": "blocked",
                    "reason": f"Unknown policy ID: {policy_id}",
                }
            )
            continue

        result = evaluator(policy)
        result["policy_id"] = policy_id
        results.append(result)

    allowed = all(
        result.get("status") == "allowed"
        for result in results
    )

    return allowed, results


def evaluate_advisory_preflight(
    task,
    task_state,
    statuses,
):
    if not task.get("advisory_preflight"):
        return True, None

    existing_preflight = task_state.get(
        "advisory_preflight"
    )

    try:
        result = run_advisory_preflight(
            task,
            existing=existing_preflight,
        )
    except Exception as error:
        result = {
            "status": "blocked",
            "reason": (
                "Advisory preflight failed: "
                f"{error}"
            ),
            "execution_authority": "none",
        }

    task_state["advisory_preflight"] = result
    task_state["updated_at"] = now()
    statuses[task["id"]] = task_state
    save_task_status(statuses, task["id"])

    if result.get("status") == "allowed":
        return True, result

    return False, result


def run_task(task, statuses):
    task_id = task["id"]
    task_state = get_task_state(task, statuses)

    if task_state["status"] == "done":
        write_event("task_skipped", task, "Task was already completed.")
        return

    if not dependencies_completed(task, statuses):
        task_state["status"] = "blocked"
        task_state["updated_at"] = now()
        statuses[task_id] = task_state
        save_task_status(statuses, task["id"])

        write_event("task_blocked", task, "A dependency has not completed.")
        return

    policies_allowed, policy_results = evaluate_required_policies(task)

    if task.get("requires_policies"):
        task_state["policy_results"] = policy_results

        if policies_allowed:
            task_state.pop("block_reason", None)
            task_state.pop("blocked_at", None)

        task_state["updated_at"] = now()
        statuses[task_id] = task_state
        save_task_status(statuses, task["id"])

    if not policies_allowed:
        blocking_result = next(
            (
                result
                for result in policy_results
                if result.get("status") != "allowed"
            ),
            {"reason": "An unknown policy blocked dispatch."},
        )

        block_reason = (
            "Required policy blocked dispatch: "
            + blocking_result.get("reason", "Unknown policy failure.")
        )

        previous_status = task_state.get("status")
        previous_block_reason = task_state.get("block_reason")

        task_state["status"] = "blocked"
        task_state["block_reason"] = block_reason
        task_state["blocked_at"] = now()
        task_state["updated_at"] = now()
        statuses[task_id] = task_state
        save_task_status(statuses, task["id"])

        if (
            previous_status != "blocked"
            or previous_block_reason != block_reason
        ):
            write_event("task_blocked", task, block_reason)
        else:
            print(
                f"[{now()}] task_blocked: {task_id} - {block_reason}"
            )

        return

    preflight_allowed, preflight_result = (
        evaluate_advisory_preflight(
            task,
            task_state,
            statuses,
        )
    )

    if not preflight_allowed:
        preflight_status = preflight_result.get(
            "status",
            "blocked",
        )

        preflight_reason = preflight_result.get(
            "reason",
            "Advisory preflight blocked dispatch.",
        )

        task_state["status"] = "blocked"
        task_state["block_reason"] = (
            "Advisory preflight blocked dispatch: "
            f"{preflight_reason}"
        )
        task_state["blocked_at"] = now()
        task_state["updated_at"] = now()
        statuses[task_id] = task_state
        save_task_status(statuses, task["id"])

        write_event(
            "task_blocked",
            task,
            task_state["block_reason"],
        )

        return

    if task.get("requires_approval", False):
        approval_status = task_state.get("approval_status", "waiting_approval")

        if approval_status == "rejected":
            # v0.18.0: a human rejection is final for this task; never
            # re-queue it for approval and never dispatch it.
            if task_state.get("status") != "rejected":
                task_state["status"] = "rejected"
                task_state["updated_at"] = now()
                statuses[task_id] = task_state
                save_task_status(statuses, task["id"])
            print(f"Task {task_id} was rejected by a human; not dispatched.")
            return

        if approval_status != "approved":
            previous_status = task_state.get("status")

            task_state["status"] = "waiting_approval"
            task_state["approval_status"] = "waiting_approval"
            task_state["updated_at"] = now()
            statuses[task_id] = task_state
            save_task_status(statuses, task["id"])

            if previous_status != "waiting_approval":
                write_event(
                    "task_waiting_approval",
                    task,
                    "Human approval is required before dispatch.",
                )

            print(
                f"Task {task_id} is waiting for approval.\n"
                f"Run: python3 mini_orch.py approve {task_id}"
            )
            return

    max_attempts = task.get("max_retries", 0) + 1

    while task_state["attempt"] < max_attempts:
        task_state["attempt"] += 1
        task_state["status"] = "running"
        task_state["started_at"] = now()
        statuses[task_id] = task_state
        save_task_status(statuses, task["id"])

        write_event(
            "task_started",
            task,
            f"Attempt {task_state['attempt']} of {max_attempts}.",
        )

        try:
            result = subprocess.run(
                task["command"],
                text=True,
                capture_output=True,
                timeout=60,
            )
        except FileNotFoundError:
            result = None
            error_message = f"Command not found: {task['command'][0]}"
        except subprocess.TimeoutExpired:
            result = None
            error_message = "Task timed out after 60 seconds."
        else:
            error_message = result.stderr.strip() or result.stdout.strip()

        if result is not None and result.returncode == 0:
            task_state["status"] = "done"
            task_state.pop("error", None)
            task_state.pop("block_reason", None)
            task_state.pop("blocked_at", None)
            task_state["finished_at"] = now()
            task_state["output"] = result.stdout.strip()
            statuses[task_id] = task_state
            save_task_status(statuses, task["id"])

            write_event("task_completed", task, task_state["output"])
            return

        task_state["status"] = "retrying"
        task_state["error"] = error_message
        statuses[task_id] = task_state
        save_task_status(statuses, task["id"])

        write_event("task_failed", task, error_message)

        if task_state["attempt"] < max_attempts:
            write_event("task_retrying", task, "Retrying in 2 seconds.")
            time.sleep(2)

    task_state["status"] = "failed"
    task_state["finished_at"] = now()
    statuses[task_id] = task_state
    save_task_status(statuses, task["id"])

    write_event("task_abandoned", task, "No retries remain.")


def decide_approval(
    task_id,
    decision,
    decided_by,
    note=None,
    *,
    queue_file=None,
    status_file=None,
    events_file=None,
    extra_state=None,
    strict=True,
    requested_by=None,
    os_user=None,
):
    """Record a human approval decision through the standard gate.

    ``decision`` is ``"approved"`` or ``"rejected"``. Returns a result
    dict (``ok`` plus ``reason`` on failure). This never dispatches
    the task; an approved task runs on the next queue execution.
    """
    if decision not in {"approved", "rejected"}:
        return {"ok": False, "reason": "invalid_decision"}

    if not isinstance(decided_by, str) or not decided_by.strip():
        return {"ok": False, "reason": "operator_required"}

    decided_by = decided_by.strip()

    # v0.20.0: nobody approves their own work. ``requested_by`` is a name
    # or list of names (draft requester + every version author).
    if requested_by:
        authors = [requested_by] if isinstance(requested_by, str) else list(requested_by)
        if any(
            str(author or "").strip().casefold() == decided_by.casefold()
            for author in authors
        ):
            return {"ok": False, "reason": "self_approval" if decision == "approved"
                    else "self_rejection"}

    queue_path = Path(queue_file) if queue_file is not None else QUEUE_FILE
    status_path = (
        Path(status_file) if status_file is not None else STATUS_FILE
    )

    # v0.21.0: the gate check, the conditional status update and the audit
    # event are one SQLite transaction (BEGIN IMMEDIATE).
    if not orch_db.is_managed(status_path):     # plain JSON file (old callers)
        return _decide_in_transaction(
            task_id, decision, decided_by, note, queue_file, queue_path,
            status_path, events_file, extra_state, strict, os_user,
        )
    with orch_db.transaction(status_path):
        return _decide_in_transaction(
            task_id, decision, decided_by, note, queue_file, queue_path,
            status_path, events_file, extra_state, strict, os_user,
        )


def _decide_in_transaction(task_id, decision, decided_by, note, queue_file,
                           queue_path, status_path, events_file, extra_state,
                           strict, os_user):
    if queue_file is not None:
        tasks = load_json(queue_path, [])
    else:
        tasks = load_all_tasks()
    statuses = load_json(status_path, {})

    task = next((item for item in tasks if item["id"] == task_id), None)

    if task is None:
        return {"ok": False, "reason": "task_not_found"}

    if not task.get("requires_approval", False):
        return {"ok": False, "reason": "approval_not_required"}

    task_state = get_task_state(task, statuses)

    if task_state.get("status") == "done":
        return {"ok": False, "reason": "already_done"}

    prior = task_state.get("approval_status")

    if prior == "rejected" or (strict and prior == "approved"):
        return {"ok": False, "reason": "already_decided"}

    timestamp = now()

    if decision == "approved":
        task_state["status"] = "approved"
        task_state["approval_status"] = "approved"
        task_state["approved_at"] = timestamp
        task_state["approved_by"] = decided_by
        event_name = "task_approved"
        message = (
            f"Approved by {decided_by}. "
            "Task can run on next queue execution."
        )
    else:
        task_state["status"] = "rejected"
        task_state["approval_status"] = "rejected"
        task_state["rejected_at"] = timestamp
        task_state["rejected_by"] = decided_by
        event_name = "task_rejected"
        message = f"Rejected by {decided_by}. Task will not be dispatched."

    if note:
        task_state["approval_note"] = str(note)[:500]
        message += f" Note: {str(note)[:200]}"

    if extra_state:
        task_state.update(extra_state)
    if os_user:
        # v0.20.0: CLI decisions record the OS account (shell access is trusted).
        task_state["decided_os_user"] = os_user

    task_state["updated_at"] = timestamp
    statuses[task_id] = task_state
    # Conditional update: only succeeds while the stored decision is still
    # the one checked above (so only one approval can ever win).
    if not orch_db.is_managed(status_path):
        status_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(status_path, statuses)
    elif not orch_db.put_task_state_if(status_path, task_id, task_state, prior):
        return {"ok": False, "reason": "already_decided"}

    write_event(
        event_name,
        task,
        message,
        events_file=events_file,
        extra={"operator": decided_by, **({"os_user": os_user} if os_user else {})},
    )

    return {
        "ok": True,
        "task_id": task_id,
        "decision": decision,
        "decided_by": decided_by,
        "decided_at": timestamp,
        "state": task_state,
    }


def _os_user():
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return "unknown"


def approve_task(task_id):
    if str(task_id).startswith(DEMO_TASK_PREFIX):
        # v0.18.2: demo drafts need a named approver, a channel and an
        # ecom_audit record, which only the Approval Inbox writes.
        print(
            f"Refused: {task_id} is an e-commerce demo draft. Approve or "
            "reject it in the Approval Inbox (http://127.0.0.1:5050/inbox) "
            "so the audit record is written."
        )
        return False

    with state_lock():
        result = decide_approval(
            task_id,
            "approved",
            "local_terminal_user",
            strict=False,
            os_user=_os_user(),
        )

    if result["ok"]:
        print(f"Approved: {task_id}")
        print("Next step: python3 mini_orch.py")
        return

    reason = result["reason"]

    if reason == "task_not_found":
        print(f"Approval failed: task not found: {task_id}")
    elif reason == "approval_not_required":
        print(f"Approval not required for task: {task_id}")
    elif reason == "already_done":
        print(f"Task is already completed: {task_id}")
    elif reason == "already_decided":
        print(f"Approval failed: task was rejected by a human: {task_id}")
    else:
        print(f"Approval failed ({reason}): {task_id}")


def run_queue():
    STATE_DIR.mkdir(exist_ok=True)

    with state_lock():
        tasks = load_all_tasks()
        statuses = load_json(STATUS_FILE, {})

    tasks.sort(key=lambda task: task.get("priority", 999))

    print(f"Mini ORCH loaded {len(tasks)} tasks.")

    write_event(
        "orchestrator_started",
        {"id": "orchestrator", "title": "Mini ORCH"},
        "Task queue execution started.",
    )

    for task in tasks:
        # Pick up UI changes (approvals, new drafts) made since the last
        # task; the lock is not held while the task's command runs.
        with state_lock():
            fresh = load_json(STATUS_FILE, {})
        statuses.clear()
        statuses.update(fresh)
        run_task(task, statuses)

    completed_count = sum(
        statuses.get(task["id"], {}).get("status") == "done"
        for task in tasks
    )

    print(f"Queue finished: {completed_count}/{len(tasks)} tasks completed.")

    write_event(
        "orchestrator_finished",
        {"id": "orchestrator", "title": "Mini ORCH"},
        "Task queue execution finished.",
    )


def show_status():
    tasks = load_all_tasks()
    statuses = load_json(STATUS_FILE, {})

    tasks.sort(key=lambda task: task.get("priority", 999))

    print("MINI ORCH STATUS")
    print("=" * 50)

    for task in tasks:
        task_id = task["id"]
        task_state = statuses.get(task_id, {})
        status = task_state.get("status", "todo")
        title = task["title"]

        print(f"\n[{status.upper()}] {task_id}")
        print(f"  {title}")

        if task.get("depends_on"):
            dependencies = ", ".join(task["depends_on"])
            print(f"  Depends on: {dependencies}")

        if task.get("requires_approval", False):
            approval_status = task_state.get(
                "approval_status",
                "waiting_approval",
            )
            print(f"  Approval: {approval_status}")

            if approval_status == "waiting_approval":
                if task_id.startswith(DEMO_TASK_PREFIX):
                    print("  Approve in the Approval Inbox: /inbox")
                else:
                    print(
                        f"  Approve with: "
                        f"python3 mini_orch.py approve {task_id}"
                    )

        if task_state.get("attempt") is not None:
            print(f"  Attempts: {task_state.get('attempt', 0)}")

        if task_state.get("approved_by"):
            print(f"  Approved by: {task_state['approved_by']}")

        if task_state.get("approved_at"):
            print(f"  Approved at: {task_state['approved_at']}")

        if task_state.get("block_reason"):
            print(f"  Block reason: {task_state['block_reason']}")

        policy_results = task_state.get("policy_results", [])

        if policy_results:
            print("  Policies:")

            for result in policy_results:
                policy_id = result.get("policy_id", "unknown")
                policy_status = result.get("status", "unknown")
                print(f"    - {policy_id}: {policy_status}")

                if result.get("artifact"):
                    print(f"      Artifact: {result['artifact']}")

                if result.get("reason"):
                    print(f"      Reason: {result['reason']}")

        if task_state.get("error"):
            print(f"  Error: {task_state['error']}")

    print("\n" + "=" * 50)
    print(f"Total tasks in current queue: {len(tasks)}")


def add_task(
    task_id,
    title,
    command_text,
    priority_text,
    requires_approval=False,
    dependencies=None,
    required_policies=None,
    advisory_preflight=False,
):
    tasks = load_json(QUEUE_FILE, [])
    all_ids = {task.get("id") for task in load_all_tasks()}
    dependencies = dependencies or []
    required_policies = required_policies or []

    if task_id in dependencies:
        print("Add task failed: a task cannot depend on itself.")
        return

    existing_ids = {task["id"] for task in tasks} | all_ids

    missing_dependencies = [
        dependency
        for dependency in dependencies
        if dependency not in existing_ids
    ]

    if missing_dependencies:
        print(
            "Add task failed: dependency task ID does not exist: "
            + ", ".join(missing_dependencies)
        )
        return

    if task_id in existing_ids:
        print(f"Add task failed: task ID already exists: {task_id}")
        return

    try:
        priority = int(priority_text)
    except ValueError:
        print("Add task failed: priority must be a whole number.")
        return

    command = shlex.split(command_text)

    if not command:
        print("Add task failed: command cannot be empty.")
        return

    task = {
        "id": task_id,
        "title": title,
        "command": command,
        "priority": priority,
        "depends_on": dependencies,
        "max_retries": 1,
        "requires_approval": requires_approval,
        "requires_policies": required_policies,
    }

    if advisory_preflight:
        task["advisory_preflight"] = {
            "artifact_logical_names": [],
            "policy_ids": [],
        }

    tasks.append(task)
    tasks.sort(key=lambda item: item.get("priority", 999))

    save_json(QUEUE_FILE, tasks)

    print("Task added successfully.")
    print(f"  ID: {task_id}")
    print(f"  Title: {title}")
    print(f"  Command: {command}")
    print(f"  Priority: {priority}")
    print(
        "  Dependencies: "
        + (", ".join(dependencies) if dependencies else "none")
    )
    print(
        "  Approval required: "
        + ("yes" if requires_approval else "no")
    )
    print(
        "  Required policies: "
        + (
            ", ".join(
                policy.get("id", "unknown")
                for policy in required_policies
            )
            if required_policies
            else "none"
        )
    )


def main():
    if len(sys.argv) == 1:
        run_queue()
        return

    if len(sys.argv) == 2 and sys.argv[1] == "status":
        show_status()
        return

    if len(sys.argv) == 3 and sys.argv[1] == "approve":
        approve_task(sys.argv[2])
        return

    if len(sys.argv) >= 6 and sys.argv[1] == "add-task":
        requires_approval = False
        dependencies = []
        required_policies = []
        advisory_preflight = False
        option_index = 6

        while option_index < len(sys.argv):
            option = sys.argv[option_index]

            if option == "--approval":
                requires_approval = True
                option_index += 1
                continue

            if option == "--advisory-preflight":
                advisory_preflight = True
                requires_approval = True
                option_index += 1
                continue

            if option == "--require-artifact":
                if option_index + 1 >= len(sys.argv):
                    print(
                        "Add task failed: "
                        "--require-artifact requires a relative file path."
                    )
                    return

                artifact_path = sys.argv[option_index + 1]

                if Path(artifact_path).is_absolute():
                    print(
                        "Add task failed: "
                        "--require-artifact does not allow absolute paths."
                    )
                    return

                required_policies.append(
                    {
                        "id": "artifact-exists",
                        "artifact": artifact_path,
                    }
                )

                option_index += 2
                continue

            if option == "--require-json-field":
                if option_index + 3 >= len(sys.argv):
                    print(
                        "Add task failed: --require-json-field requires "
                        "<artifact> <field> <expected_json>."
                    )
                    return

                artifact_path = sys.argv[option_index + 1]
                field_path = sys.argv[option_index + 2]
                expected_json = sys.argv[option_index + 3]

                if Path(artifact_path).is_absolute():
                    print(
                        "Add task failed: --require-json-field does not "
                        "allow absolute paths."
                    )
                    return

                try:
                    expected_value = json.loads(expected_json)
                except json.JSONDecodeError:
                    print(
                        "Add task failed: expected_json must be valid JSON."
                    )
                    return

                required_policies.append(
                    {
                        "id": "json-field-equals",
                        "artifact": artifact_path,
                        "field": field_path,
                        "equals": expected_value,
                    }
                )

                option_index += 4
                continue

            if option == "--depends-on":
                if option_index + 1 >= len(sys.argv):
                    print(
                        "Add task failed: --depends-on requires a task ID."
                    )
                    return

                raw_dependencies = sys.argv[option_index + 1]

                dependencies = [
                    item.strip()
                    for item in raw_dependencies.split(",")
                    if item.strip()
                ]

                option_index += 2
                continue

            print(f"Add task failed: unknown option: {option}")
            return

        add_task(
            sys.argv[2],
            sys.argv[3],
            sys.argv[4],
            sys.argv[5],
            requires_approval,
            dependencies,
            required_policies,
            advisory_preflight=advisory_preflight,
        )
        return

    print("Usage:")
    print("  python3 mini_orch.py")
    print("  python3 mini_orch.py status")
    print("  python3 mini_orch.py approve <task_id>")
    print("  python3 mini_orch.py add-task <id> <title> <command> <priority>")
    print(
        "  python3 mini_orch.py add-task "
        "<id> <title> <command> <priority> --approval"
    )
    print(
        "  python3 mini_orch.py add-task "
        "<id> <title> <command> <priority> "
        "--advisory-preflight"
    )
    print(
        "  python3 mini_orch.py add-task "
        "<id> <title> <command> <priority> "
        "--depends-on <task_id[,task_id]>"
    )
    print(
        "  python3 mini_orch.py add-task "
        "<id> <title> <command> <priority> "
        "--require-artifact <relative_file_path>"
    )
    print(
        "  python3 mini_orch.py add-task "
        "<id> <title> <command> <priority> "
        "--require-json-field <artifact> <field> <expected_json>"
    )


if __name__ == "__main__":
    main()
