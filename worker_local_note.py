from datetime import datetime
from pathlib import Path
import json

output_dir = Path("output")
output_dir.mkdir(exist_ok=True)

result = {
    "status": "success",
    "task": "task_local_note_007",
    "message": "Task 007 was created through the Mini ORCH add-task command.",
    "created_at": datetime.now().isoformat(timespec="seconds"),
}

output_file = output_dir / "task_007_artifact.json"

output_file.write_text(
    json.dumps(result, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print(f"Task 007 completed: {output_file}")
