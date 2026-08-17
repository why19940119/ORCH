from datetime import datetime, timezone
from pathlib import Path
import json

output_file = Path("output/market_alert_dry_run_executed.json")

output = {
    "status": "executed",
    "message": (
        "This dry-run worker executed. "
        "With a blocked freshness gate, this should not happen."
    ),
    "executed_at_utc": datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ),
}

output_file.write_text(
    json.dumps(output, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)

print("Dry-run market alert worker executed.")
