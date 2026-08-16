from datetime import datetime
from pathlib import Path
import json
import sys

report_file = Path("output/report.json")
validation_file = Path("output/validation.json")

if not report_file.exists():
    print("Validation failed: output/report.json does not exist.", file=sys.stderr)
    sys.exit(1)

report = json.loads(report_file.read_text(encoding="utf-8"))

is_valid = (
    report.get("status") == "success"
    and report.get("task") == "daily_report"
    and bool(report.get("generated_at"))
)

validation = {
    "status": "success" if is_valid else "failed",
    "validated_task": report.get("task"),
    "report_file": str(report_file),
    "message": (
        "Report passed validation."
        if is_valid
        else "Report format or required values are invalid."
    ),
    "validated_at": datetime.now().isoformat(timespec="seconds"),
}

validation_file.write_text(
    json.dumps(validation, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print(validation["message"])

if not is_valid:
    sys.exit(1)
