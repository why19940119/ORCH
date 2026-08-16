from datetime import datetime
from pathlib import Path
import json

output_dir = Path("output")
output_dir.mkdir(exist_ok=True)

result = {
    "status": "success",
    "task": "approved_action",
    "message": "Human-approved action was executed successfully.",
    "executed_at": datetime.now().isoformat(timespec="seconds"),
}

result_file = output_dir / "approved_action.json"

result_file.write_text(
    json.dumps(result, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print(f"Approved action completed: {result_file}")
