from datetime import datetime, timezone
from pathlib import Path
import json

from snapshot_store import build_scoped_snapshot

snapshot = build_scoped_snapshot(
    requested_task_ids=[
        "task_validate_report_002",
        "task_approval_demo_006",
    ],
    artifact_logical_names=[
        "manifest_demo",
    ],
    policy_ids=[
        "artifact-exists",
        "json-field-equals",
    ],
)

output = {
    "status": "success",
    "task": "task_create_scoped_snapshot_demo_018",
    "generated_at_utc": datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ),
    "snapshot": snapshot,
    "message": (
        "Scoped read-set snapshot was created successfully."
    ),
}

output_file = Path("output/scoped_snapshot_demo.json")
output_file.write_text(
    json.dumps(output, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)

print(output["message"])
