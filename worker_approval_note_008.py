from datetime import datetime
from pathlib import Path
import json

output_dir = Path("output")
output_dir.mkdir(exist_ok=True)

result = {
    "status": "success",
    "task": "task_approval_note_008",
    "message": "Task 008 completed only after explicit human approval.",
    "executed_at": datetime.now().isoformat(timespec="seconds"),
}

output_file = output_dir / "task_008_approved_artifact.json"

output_file.write_text(
    json.dumps(result, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print(f"Task 008 completed: {output_file}")
