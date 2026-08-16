from datetime import datetime
from pathlib import Path
import json
import subprocess
import sys
import time

STATE_DIR = Path("state")
TASKS_FILE = STATE_DIR / "tasks.json"
EVENTS_FILE = STATE_DIR / "events.jsonl"

TASK = {
    "id": "task_daily_report_001",
    "title": "Generate daily JSON report",
    "command": [sys.executable, "worker_report.py"],
    "status": "todo",
    "max_retries": 2,
    "attempt": 0,
}


def now():
    return datetime.now().isoformat(timespec="seconds")


def write_event(event, task, message):
    record = {
        "timestamp": now(),
        "event": event,
        "task_id": task["id"],
        "status": task["status"],
        "attempt": task["attempt"],
        "message": message,
    }

    with EVENTS_FILE.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"[{record['timestamp']}] {event}: {message}")


def save_task(task):
    TASKS_FILE.write_text(
        json.dumps(task, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run_task(task):
    while task["attempt"] <= task["max_retries"]:
        task["attempt"] += 1
        task["status"] = "running"
        save_task(task)
        write_event("task_started", task, "Starting worker command.")

        result = subprocess.run(
            task["command"],
            text=True,
            capture_output=True,
        )

        if result.returncode == 0:
            task["status"] = "done"
            task["finished_at"] = now()
            task["output"] = result.stdout.strip()
            save_task(task)
            write_event("task_completed", task, task["output"])
            return

        task["status"] = "retrying"
        task["error"] = result.stderr.strip() or result.stdout.strip()
        save_task(task)
        write_event("task_failed", task, task["error"])

        if task["attempt"] <= task["max_retries"]:
            write_event("task_retrying", task, "Retrying in 2 seconds.")
            time.sleep(2)

    task["status"] = "failed"
    task["finished_at"] = now()
    save_task(task)
    write_event("task_abandoned", task, "No retries remain.")


def main():
    STATE_DIR.mkdir(exist_ok=True)
    write_event("orchestrator_started", TASK, "Mini ORCH is running.")
    run_task(TASK)
    write_event("orchestrator_finished", TASK, f"Final status: {TASK['status']}")


if __name__ == "__main__":
    main()
