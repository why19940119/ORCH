from datetime import datetime
from pathlib import Path
import json
import sys

source_file = Path("output/task_008_approved_artifact.json")
output_file = Path("output/task_009_validation.json")

if not source_file.exists():
    print(
        "Validation failed: Task 008 artifact does not exist.",
        file=sys.stderr,
    )
    sys.exit(1)

task_008_result = json.loads(
    source_file.read_text(encoding="utf-8")
)

is_valid = (
    task_008_result.get("status") == "success"
    and task_008_result.get("task") == "task_approval_note_008"
    and bool(task_008_result.get("executed_at"))
)

validation = {
    "status": "success" if is_valid else "failed",
    "task": "task_validate_009",
    "validated_input": str(source_file),
    "message": (
        "Task 008 approved artifact passed validation."
        if is_valid
        else "Task 008 artifact is missing required values."
    ),
    "validated_at": datetime.now().isoformat(timespec="seconds"),
}

output_file.write_text(
    json.dumps(validation, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print(validation["message"])

if not is_valid:
    sys.exit(1)
