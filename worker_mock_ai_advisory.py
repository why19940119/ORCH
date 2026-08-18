from datetime import datetime, timezone
from pathlib import Path
import json

queue_file = Path("task_queue.json")
output_file = Path("output/ai_advisory.json")

tasks = json.loads(queue_file.read_text(encoding="utf-8"))

advisory = {
    "status": "success",
    "mode": "advisory",
    "provider": "mock",
    "model": "deterministic-test-adapter",
    "generated_at_utc": datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ),
    "input_context": {
        "task_queue_file": str(queue_file),
        "task_count": len(tasks),
    },
    "summary": (
        "Mock AI advisory generated from the current Mini ORCH "
        "task queue. This output is deterministic and has no "
        "execution authority."
    ),
    "recommendations": [
        {
            "priority": "normal",
            "message": (
                "Review task dependencies and policy requirements "
                "before adding new side-effecting integrations."
            ),
        }
    ],
    "proposed_actions": [],
    "execution_authority": "none",
    "external_api_called": False,
}

output_file.write_text(
    json.dumps(advisory, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)

print("Mock AI advisory generated with execution_authority=none.")
