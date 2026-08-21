from datetime import datetime, timezone
from pathlib import Path
import json
import sys

from openrouter_advisory import request_advisory


output_file = Path("output/openrouter_advisory_probe.json")

task = {
    "task_id": "task_openrouter_advisory_probe_023",
    "title": "Generate an advisory-only AI review probe",
    "command": [
        "python3",
        "worker_openrouter_advisory_probe.py",
    ],
    "priority": 23,
    "approval_required": False,
    "required_policies": [],
    "execution_authority": "none",
}

errors = []
result = None

try:
    result = request_advisory(task)
except Exception as error:
    errors.append(str(error))

output = {
    "status": "success" if not errors else "failed",
    "task": task["task_id"],
    "tested_at_utc": datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ),
    "execution_authority": "none",
    "result": result,
    "errors": errors,
    "message": (
        "OpenRouter advisory probe passed."
        if not errors
        else "OpenRouter advisory probe failed."
    ),
}

output_file.write_text(
    json.dumps(output, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)

print(output["message"])

if errors:
    for error in errors:
        print(f"- {error}", file=sys.stderr)

    sys.exit(1)
