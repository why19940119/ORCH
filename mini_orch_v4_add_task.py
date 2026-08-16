import sys
import shlex
from datetime import datetime
from pathlib import Path
import json
import subprocess
import time

STATE_DIR = Path("state")
QUEUE_FILE = Path("task_queue.json")
STATUS_FILE = STATE_DIR / "task_status.json"
EVENTS_FILE = STATE_DIR / "events.jsonl"


def now():
    return datetime.now().isoformat(timespec="seconds")


def load_json(file_path, default_value):
    if not file_path.exists():
        return default_value

    return json.loads(file_path.read_text(encoding="utf-8"))


def save_json(file_path, data):
    file_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_event(event, task, message):
    record = {
        "timestamp": now(),
        "event": event,
        "task_id": task["id"],
        "task_title": task["title"],
        "message": message,
    }

    with EVENTS_FILE.open("a", encoding="utf-8") as file:
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
        save_json(STATUS_FILE, statuses)

        write_event("task_blocked", task, "A dependency has not completed.")
        return

    if task.get("requires_approval", False):
        approval_status = task_state.get("approval_status", "waiting_approval")

        if approval_status != "approved":
            previous_status = task_state.get("status")

            task_state["status"] = "waiting_approval"
            task_state["approval_status"] = "waiting_approval"
            task_state["updated_at"] = now()
            statuses[task_id] = task_state
            save_json(STATUS_FILE, statuses)

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
        save_json(STATUS_FILE, statuses)

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
            task_state["finished_at"] = now()
            task_state["output"] = result.stdout.strip()
            statuses[task_id] = task_state
            save_json(STATUS_FILE, statuses)

            write_event("task_completed", task, task_state["output"])
            return

        task_state["status"] = "retrying"
        task_state["error"] = error_message
        statuses[task_id] = task_state
        save_json(STATUS_FILE, statuses)

        write_event("task_failed", task, error_message)

        if task_state["attempt"] < max_attempts:
            write_event("task_retrying", task, "Retrying in 2 seconds.")
            time.sleep(2)

    task_state["status"] = "failed"
    task_state["finished_at"] = now()
    statuses[task_id] = task_state
    save_json(STATUS_FILE, statuses)

    write_event("task_abandoned", task, "No retries remain.")


def approve_task(task_id):
    tasks = load_json(QUEUE_FILE, [])
    statuses = load_json(STATUS_FILE, {})

    task = next((item for item in tasks if item["id"] == task_id), None)

    if task is None:
        print(f"Approval failed: task not found: {task_id}")
        return

    if not task.get("requires_approval", False):
        print(f"Approval not required for task: {task_id}")
        return

    task_state = get_task_state(task, statuses)

    if task_state.get("status") == "done":
        print(f"Task is already completed: {task_id}")
        return

    task_state["status"] = "approved"
    task_state["approval_status"] = "approved"
    task_state["approved_at"] = now()
    task_state["approved_by"] = "local_terminal_user"

    statuses[task_id] = task_state
    save_json(STATUS_FILE, statuses)

    write_event(
        "task_approved",
        task,
        "Approved by local_terminal_user. Task can run on next queue execution.",
    )

    print(f"Approved: {task_id}")
    print("Next step: python3 mini_orch.py")


def run_queue():
    STATE_DIR.mkdir(exist_ok=True)

    tasks = load_json(QUEUE_FILE, [])
    statuses = load_json(STATUS_FILE, {})

    tasks.sort(key=lambda task: task.get("priority", 999))

    print(f"Mini ORCH loaded {len(tasks)} tasks.")

    write_event(
        "orchestrator_started",
        {"id": "orchestrator", "title": "Mini ORCH"},
        "Task queue execution started.",
    )

    for task in tasks:
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
    tasks = load_json(QUEUE_FILE, [])
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

        if task_state.get("error"):
            print(f"  Error: {task_state['error']}")

    print("\n" + "=" * 50)
    print(f"Total tasks in current queue: {len(tasks)}")


def add_task(task_id, title, command_text, priority_text):
    tasks = load_json(QUEUE_FILE, [])

    if any(task["id"] == task_id for task in tasks):
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
        "depends_on": [],
        "max_retries": 1,
    }

    tasks.append(task)
    tasks.sort(key=lambda item: item.get("priority", 999))

    save_json(QUEUE_FILE, tasks)

    print("Task added successfully.")
    print(f"  ID: {task_id}")
    print(f"  Title: {title}")
    print(f"  Command: {command}")
    print(f"  Priority: {priority}")
    print("  Dependencies: none")
    print("  Approval required: no")


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

    if len(sys.argv) == 6 and sys.argv[1] == "add-task":
        add_task(
            sys.argv[2],
            sys.argv[3],
            sys.argv[4],
            sys.argv[5],
        )
        return

    print("Usage:")
    print("  python3 mini_orch.py")
    print("  python3 mini_orch.py status")
    print("  python3 mini_orch.py approve <task_id>")
    print("  python3 mini_orch.py add-task <id> <title> <command> <priority>")


if __name__ == "__main__":
    main()
