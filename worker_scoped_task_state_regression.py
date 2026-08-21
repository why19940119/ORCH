from datetime import datetime, timezone
from pathlib import Path
import json
import sys

from snapshot_store import build_scoped_snapshot, validate_scoped_snapshot

STATUS_FILE = Path("state/task_status.json")
OUTPUT_FILE = Path("output/scoped_task_state_regression.json")

TASK_SCOPE = [
    "task_validate_report_002",
    "task_approval_demo_006",
]

POLICY_SCOPE = [
    "artifact-exists",
    "json-field-equals",
]

ARTIFACT_SCOPE = [
    "snapshot_regression_scoped",
]

errors = []

snapshot = build_scoped_snapshot(
    requested_task_ids=TASK_SCOPE,
    artifact_logical_names=ARTIFACT_SCOPE,
    policy_ids=POLICY_SCOPE,
)

original_status_content = STATUS_FILE.read_bytes()
validation = {}

try:
    statuses = json.loads(
        original_status_content.decode("utf-8")
    )

    task_state = statuses.setdefault(
        "task_validate_report_002",
        {},
    )

    task_state["snapshot_regression_probe"] = (
        "temporary_state_change"
    )

    STATUS_FILE.write_text(
        json.dumps(
            statuses,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    validation = validate_scoped_snapshot(snapshot)
finally:
    STATUS_FILE.write_bytes(original_status_content)

if validation.get("status") != "stale":
    errors.append(
        "Scoped task state update must stale the snapshot."
    )

if (
    "task_scope_changed"
    not in validation.get("differences", [])
):
    errors.append(
        "Scoped task state update must report task_scope_changed."
    )

output = {
    "status": "success" if not errors else "failed",
    "task": "task_scoped_task_state_regression_021",
    "tested_at_utc": datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ),
    "snapshot_fingerprint": snapshot.get(
        "snapshot_fingerprint"
    ),
    "validation": validation,
    "errors": errors,
    "message": (
        "Scoped task state regression passed."
        if not errors
        else "Scoped task state regression failed."
    ),
}

OUTPUT_FILE.write_text(
    json.dumps(output, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)

print(output["message"])

if errors:
    for error in errors:
        print(f"- {error}", file=sys.stderr)

    sys.exit(1)
