from datetime import datetime
from pathlib import Path
import json

output_dir = Path("output")
output_dir.mkdir(exist_ok=True)

report = {
    "status": "success",
    "task": "daily_report",
    "message": "Python worker completed the automation task.",
    "generated_at": datetime.now().isoformat(timespec="seconds"),
}

report_file = output_dir / "report.json"
report_file.write_text(
    json.dumps(report, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print(f"Worker completed successfully: {report_file}")
