from datetime import datetime, timezone
from pathlib import Path
import json

output_file = Path("output/multi_policy_probe.json")

output = {
    "status": "executed",
    "message": "Multi-policy probe executed after all policies allowed dispatch.",
    "executed_at_utc": datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ),
}

output_file.write_text(
    json.dumps(output, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)

print("Multi-policy probe executed.")
