from datetime import datetime, timezone
from pathlib import Path
import json

output_file = Path("output/json_policy_probe.json")

output = {
    "status": "executed",
    "message": (
        "JSON field policy probe executed after required "
        "field value matched."
    ),
    "executed_at_utc": datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ),
}

output_file.write_text(
    json.dumps(output, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)

print("JSON field policy probe executed.")
