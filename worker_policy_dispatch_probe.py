from datetime import datetime, timezone
from pathlib import Path
import json

output_file = Path("output/policy_dispatch_probe.json")

output = {
    "status": "executed",
    "message": "Policy test worker was dispatched.",
    "executed_at_utc": datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ),
}

output_file.write_text(
    json.dumps(output, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)

print("Policy dispatch probe executed.")
