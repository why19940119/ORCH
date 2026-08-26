from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import uuid

QUEUE_FILE = Path("task_queue.json")
STATUS_FILE = Path("state/task_status.json")
POLICY_CONTRACTS_FILE = Path("policy_contracts.json")

RUNTIME_STATE_FIELDS = {
    "status",
    "attempt",
    "started_at",
    "updated_at",
    "finished_at",
    "approval_status",
    "approved_at",
    "approved_by",
    "output",
    "error",
    "block_reason",
    "blocked_at",
    "advisory_preflight",
}


def project_task_state(
    task_id,
    task_state,
    ignore_runtime_state_for_task_ids,
):
    if task_id not in ignore_runtime_state_for_task_ids:
        return task_state

    return {
        field: value
        for field, value in task_state.items()
        if field not in RUNTIME_STATE_FIELDS
    }


def utc_now():
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )


def canonical_bytes(data):
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_value(data):
    return "sha256:" + hashlib.sha256(
        canonical_bytes(data)
    ).hexdigest()


def load_json(path, default_value=None):
    if not path.exists():
        if default_value is not None:
            return default_value

        raise FileNotFoundError(f"Missing required file: {path}")

    return json.loads(path.read_text(encoding="utf-8"))


def resolve_task_scope(tasks_by_id, requested_task_ids):
    resolved_ids = set()
    visiting_ids = set()

    def visit(task_id):
        if task_id in resolved_ids:
            return

        if task_id in visiting_ids:
            raise ValueError(
                f"Dependency cycle detected at task: {task_id}"
            )

        task = tasks_by_id.get(task_id)

        if task is None:
            raise ValueError(
                f"Requested or dependency task does not exist: {task_id}"
            )

        visiting_ids.add(task_id)

        for dependency_id in task.get("depends_on", []):
            visit(dependency_id)

        visiting_ids.remove(task_id)
        resolved_ids.add(task_id)

    for task_id in requested_task_ids:
        visit(task_id)

    return sorted(resolved_ids)


def load_artifact_fingerprint(logical_name):
    if not isinstance(logical_name, str) or not logical_name:
        raise ValueError(
            "Artifact logical name must be a non-empty string."
        )

    latest_path = Path("artifacts/latest") / f"{logical_name}.json"

    if not latest_path.exists():
        raise FileNotFoundError(
            f"Artifact latest pointer does not exist: {latest_path}"
        )

    pointer = load_json(latest_path)

    required_fields = [
        "artifact_id",
        "manifest_path",
        "content_sha256",
    ]

    missing_fields = [
        field
        for field in required_fields
        if not pointer.get(field)
    ]

    if missing_fields:
        raise ValueError(
            "Artifact latest pointer is missing fields: "
            + ", ".join(missing_fields)
        )

    manifest_path = Path(pointer["manifest_path"])

    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Artifact manifest does not exist: {manifest_path}"
        )

    manifest = load_json(manifest_path)

    if manifest.get("artifact_id") != pointer["artifact_id"]:
        raise ValueError(
            "Artifact manifest artifact_id does not match "
            "latest pointer."
        )

    if manifest.get("content_sha256") != pointer["content_sha256"]:
        raise ValueError(
            "Artifact manifest content hash does not match "
            "latest pointer."
        )

    return {
        "artifact_id": pointer["artifact_id"],
        "content_sha256": pointer["content_sha256"],
        "manifest_fingerprint": sha256_value(manifest),
    }


def build_scoped_snapshot(
    requested_task_ids,
    artifact_logical_names,
    policy_ids,
    ignore_runtime_state_for_task_ids=None,
):
    if not isinstance(requested_task_ids, list) or not requested_task_ids:
        raise ValueError(
            "requested_task_ids must be a non-empty list."
        )

    if not isinstance(artifact_logical_names, list):
        raise ValueError(
            "artifact_logical_names must be a list."
        )

    if not isinstance(policy_ids, list):
        raise ValueError("policy_ids must be a list.")

    if ignore_runtime_state_for_task_ids is None:
        ignore_runtime_state_for_task_ids = []

    if not isinstance(
        ignore_runtime_state_for_task_ids,
        list,
    ):
        raise ValueError(
            "ignore_runtime_state_for_task_ids must be a list."
        )

    if not all(
        isinstance(task_id, str) and task_id
        for task_id in ignore_runtime_state_for_task_ids
    ):
        raise ValueError(
            "ignore_runtime_state_for_task_ids must contain "
            "non-empty task IDs."
        )

    tasks = load_json(QUEUE_FILE, [])
    statuses = load_json(STATUS_FILE, {})
    policy_contracts = load_json(POLICY_CONTRACTS_FILE)

    tasks_by_id = {
        task["id"]: task
        for task in tasks
    }

    resolved_task_ids = resolve_task_scope(
        tasks_by_id,
        requested_task_ids,
    )

    ignored_runtime_state_ids = sorted(
        set(ignore_runtime_state_for_task_ids)
    )

    unknown_ignored_runtime_state_ids = [
        task_id
        for task_id in ignored_runtime_state_ids
        if task_id not in resolved_task_ids
    ]

    if unknown_ignored_runtime_state_ids:
        raise ValueError(
            "Ignored runtime-state task is outside snapshot scope: "
            + ", ".join(unknown_ignored_runtime_state_ids)
        )

    task_fingerprints = {}

    for task_id in resolved_task_ids:
        task_definition = tasks_by_id[task_id]
        task_state = statuses.get(task_id, {})

        projected_state = project_task_state(
            task_id,
            task_state,
            ignored_runtime_state_ids,
        )

        task_fingerprints[task_id] = {
            "task_definition_sha256": sha256_value(
                task_definition
            ),
            "task_state_sha256": sha256_value(
                projected_state
            ),
            "task_fingerprint": sha256_value(
                {
                    "task": task_definition,
                    "state": projected_state,
                }
            ),
        }

    artifact_fingerprints = {}

    for logical_name in sorted(set(artifact_logical_names)):
        artifact_fingerprints[logical_name] = (
            load_artifact_fingerprint(logical_name)
        )

    unknown_policies = [
        policy_id
        for policy_id in policy_ids
        if policy_id not in policy_contracts.get("policies", {})
    ]

    if unknown_policies:
        raise ValueError(
            "Unknown policy IDs in snapshot scope: "
            + ", ".join(unknown_policies)
        )

    selected_policy_contracts = {
        policy_id: policy_contracts["policies"][policy_id]
        for policy_id in sorted(set(policy_ids))
    }

    fingerprint_payload = {
        "snapshot_version": "1.0",
        "scope": {
            "requested_task_ids": sorted(
                set(requested_task_ids)
            ),
            "resolved_task_ids": resolved_task_ids,
            "artifact_logical_names": sorted(
                set(artifact_logical_names)
            ),
            "policy_ids": sorted(set(policy_ids)),
            "ignore_runtime_state_for_task_ids": (
                ignored_runtime_state_ids
            ),
        },
        "task_fingerprints": task_fingerprints,
        "artifact_fingerprints": artifact_fingerprints,
        "policy_contract_fingerprint": sha256_value(
            {
                "registry_version": policy_contracts.get(
                    "registry_version"
                ),
                "policies": selected_policy_contracts,
            }
        ),
    }

    snapshot = {
        "snapshot_version": "1.0",
        "snapshot_id": f"snapshot_{uuid.uuid4().hex}",
        "created_at_utc": utc_now(),
        **fingerprint_payload,
        "snapshot_fingerprint": sha256_value(
            fingerprint_payload
        ),
    }

    return snapshot


def validate_scoped_snapshot(snapshot):
    required_fields = [
        "snapshot_version",
        "snapshot_id",
        "scope",
        "task_fingerprints",
        "artifact_fingerprints",
        "policy_contract_fingerprint",
        "snapshot_fingerprint",
    ]

    missing_fields = [
        field
        for field in required_fields
        if field not in snapshot
    ]

    if missing_fields:
        return {
            "status": "invalid",
            "reason": (
                "Snapshot is missing required fields: "
                + ", ".join(missing_fields)
            ),
        }

    scope = snapshot["scope"]

    try:
        current_snapshot = build_scoped_snapshot(
            requested_task_ids=scope.get(
                "requested_task_ids",
                [],
            ),
            artifact_logical_names=scope.get(
                "artifact_logical_names",
                [],
            ),
            policy_ids=scope.get("policy_ids", []),
            ignore_runtime_state_for_task_ids=scope.get(
                "ignore_runtime_state_for_task_ids",
                [],
            ),
        )
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
        return {
            "status": "invalid",
            "reason": str(error),
        }

    original_fingerprint = snapshot["snapshot_fingerprint"]
    current_fingerprint = current_snapshot["snapshot_fingerprint"]

    if original_fingerprint == current_fingerprint:
        return {
            "status": "valid",
            "reason": (
                "Scoped snapshot matches the current "
                "task, artifact, and policy read set."
            ),
            "current_snapshot_fingerprint": current_fingerprint,
        }

    differences = []

    if (
        snapshot.get("task_fingerprints")
        != current_snapshot.get("task_fingerprints")
    ):
        differences.append("task_scope_changed")

    if (
        snapshot.get("artifact_fingerprints")
        != current_snapshot.get("artifact_fingerprints")
    ):
        differences.append("artifact_scope_changed")

    if (
        snapshot.get("policy_contract_fingerprint")
        != current_snapshot.get("policy_contract_fingerprint")
    ):
        differences.append("policy_contract_changed")

    return {
        "status": "stale",
        "reason": (
            "Scoped snapshot does not match the current "
            "read set."
        ),
        "differences": differences,
        "current_snapshot_fingerprint": current_fingerprint,
        "original_snapshot_fingerprint": original_fingerprint,
    }
