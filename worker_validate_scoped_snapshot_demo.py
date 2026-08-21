from datetime import datetime, timezone
from pathlib import Path
import json
import sys

from snapshot_store import validate_scoped_snapshot

source_file = Path("output/scoped_snapshot_demo.json")
output_file = Path("output/scoped_snapshot_validation_demo.json")

errors = []

if not source_file.exists():
    errors.append("scoped_snapshot_demo.json does not exist.")

payload = {}

if source_file.exists():
    try:
        payload = json.loads(
            source_file.read_text(encoding="utf-8")
        )
    except json.JSONDecodeError as error:
        errors.append(
            f"scoped_snapshot_demo.json is invalid JSON: {error}"
        )

snapshot = payload.get("snapshot", {})
validation_result = {}

if not errors:
    validation_result = validate_scoped_snapshot(snapshot)

    if validation_result.get("status") != "valid":
        errors.append(
            validation_result.get(
                "reason",
                "Scoped snapshot validation failed.",
            )
        )

output = {
    "status": "success" if not errors else "failed",
    "task": "task_validate_scoped_snapshot_demo_019",
    "validated_at_utc": datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ),
    "snapshot_id": snapshot.get("snapshot_id"),
    "snapshot_fingerprint": snapshot.get(
        "snapshot_fingerprint"
    ),
    "validation": validation_result,
    "errors": errors,
    "message": (
        "Scoped snapshot matches the current read set."
        if not errors
        else "Scoped snapshot validation failed."
    ),
}

output_file.write_text(
    json.dumps(output, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)

print(output["message"])

if errors:
    for error in errors:
        print(f"- {error}", file=sys.stderr)

    sys.exit(1)
