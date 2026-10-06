from datetime import datetime, timezone
from pathlib import Path
import json
import sys

import orch_db
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

# v0.21.0: task state lives in state/orch.db (orch_db); restore it after.
original_statuses = orch_db.load(STATUS_FILE, {})
validation = {}

try:
    statuses = json.loads(json.dumps(original_statuses))

    task_state = statuses.setdefault(
        "task_validate_report_002",
        {},
    )

    task_state["snapshot_regression_probe"] = (
        "temporary_state_change"
    )

    orch_db.put_task_state(STATUS_FILE, "task_validate_report_002", task_state)

    validation = validate_scoped_snapshot(snapshot)
finally:
    original = original_statuses.get("task_validate_report_002")
    if original is None:
        restored = orch_db.load(STATUS_FILE, {})
        restored.pop("task_validate_report_002", None)
        orch_db.save(STATUS_FILE, restored)
    else:
        orch_db.put_task_state(STATUS_FILE, "task_validate_report_002", original)

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
