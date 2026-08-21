from datetime import datetime, timezone
from pathlib import Path
import json
import sys

from artifact_store import publish_staged_artifact, stage_json
from snapshot_store import build_scoped_snapshot, validate_scoped_snapshot

POLICY_FILE = Path("policy_contracts.json")
OUTPUT_FILE = Path("output/scoped_snapshot_regression.json")

TASK_SCOPE = [
    "task_validate_report_002",
    "task_approval_demo_006",
]

POLICY_SCOPE = [
    "artifact-exists",
    "json-field-equals",
]

SCOPED_ARTIFACT = "snapshot_regression_scoped"
UNRELATED_ARTIFACT = "snapshot_regression_unrelated"

errors = []


def publish_json(logical_name, payload):
    staging_path = stage_json(logical_name, payload)

    return publish_staged_artifact(
        staging_path=staging_path,
        logical_name=logical_name,
        producer_task_id="task_scoped_snapshot_regression_020",
        schema_version="1.0",
    )


def expect(condition, message):
    if not condition:
        errors.append(message)


publish_json(
    SCOPED_ARTIFACT,
    {
        "status": "success",
        "revision": 1,
        "scope": "snapshot_regression",
    },
)

initial_snapshot = build_scoped_snapshot(
    requested_task_ids=TASK_SCOPE,
    artifact_logical_names=[SCOPED_ARTIFACT],
    policy_ids=POLICY_SCOPE,
)

initial_validation = validate_scoped_snapshot(initial_snapshot)

expect(
    initial_validation.get("status") == "valid",
    "Initial scoped snapshot must be valid.",
)

publish_json(
    UNRELATED_ARTIFACT,
    {
        "status": "success",
        "scope": "unrelated_regression_artifact",
    },
)

unrelated_validation = validate_scoped_snapshot(initial_snapshot)

expect(
    unrelated_validation.get("status") == "valid",
    "Unrelated artifact update must not stale the snapshot.",
)

publish_json(
    SCOPED_ARTIFACT,
    {
        "status": "success",
        "revision": 2,
        "scope": "snapshot_regression",
    },
)

artifact_validation = validate_scoped_snapshot(initial_snapshot)

expect(
    artifact_validation.get("status") == "stale",
    "Scoped artifact update must stale the snapshot.",
)

expect(
    "artifact_scope_changed"
    in artifact_validation.get("differences", []),
    "Scoped artifact update must report artifact_scope_changed.",
)

policy_snapshot = build_scoped_snapshot(
    requested_task_ids=TASK_SCOPE,
    artifact_logical_names=[SCOPED_ARTIFACT],
    policy_ids=POLICY_SCOPE,
)

original_policy_content = POLICY_FILE.read_bytes()
policy_validation = {}

try:
    policy_contracts = json.loads(
        original_policy_content.decode("utf-8")
    )

    policy_contracts["registry_version"] = (
        "1.0-regression-change"
    )

    POLICY_FILE.write_text(
        json.dumps(
            policy_contracts,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    policy_validation = validate_scoped_snapshot(
        policy_snapshot
    )
finally:
    POLICY_FILE.write_bytes(original_policy_content)

expect(
    policy_validation.get("status") == "stale",
    "Policy contract update must stale the snapshot.",
)

expect(
    "policy_contract_changed"
    in policy_validation.get("differences", []),
    "Policy contract update must report policy_contract_changed.",
)

output = {
    "status": "success" if not errors else "failed",
    "task": "task_scoped_snapshot_regression_020",
    "tested_at_utc": datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ),
    "cases": {
        "initial_snapshot": initial_validation,
        "unrelated_artifact_change": unrelated_validation,
        "scoped_artifact_change": artifact_validation,
        "policy_contract_change": policy_validation,
    },
    "errors": errors,
    "message": (
        "Scoped snapshot regression suite passed."
        if not errors
        else "Scoped snapshot regression suite failed."
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
