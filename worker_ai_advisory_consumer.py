from datetime import datetime, timezone
from pathlib import Path
import json

advisory_file = Path("output/ai_advisory.json")
output_file = Path("output/ai_advisory_consumed.json")

advisory = json.loads(advisory_file.read_text(encoding="utf-8"))

output = {
    "status": "success",
    "message": (
        "Validated advisory was consumed by a read-only "
        "downstream worker."
    ),
    "consumed_at_utc": datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ),
    "advisory_provider": advisory.get("provider"),
    "advisory_mode": advisory.get("mode"),
    "execution_authority": advisory.get("execution_authority"),
    "recommendation_count": len(
        advisory.get("recommendations", [])
    ),
    "side_effects_performed": False,
}

output_file.write_text(
    json.dumps(output, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)

print("Validated AI advisory consumed without side effects.")
