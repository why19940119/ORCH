from artifact_store import publish_staged_artifact, stage_json
from advisory_preflight import (
    create_advisory_preflight,
    validate_advisory_preflight,
)
from openrouter_advisory import request_advisory
from snapshot_store import (
    build_scoped_snapshot,
    validate_scoped_snapshot,
)


def get_advisory_config(task):
    config = task.get("advisory_preflight")

    if config is None:
        return None

    if not isinstance(config, dict):
        raise ValueError(
            "advisory_preflight must be an object."
        )

    artifact_logical_names = config.get(
        "artifact_logical_names",
        [],
    )

    policy_ids = config.get("policy_ids", [])

    if not isinstance(artifact_logical_names, list):
        raise ValueError(
            "advisory_preflight.artifact_logical_names "
            "must be a list."
        )

    if not isinstance(policy_ids, list):
        raise ValueError(
            "advisory_preflight.policy_ids must be a list."
        )

    if not all(
        isinstance(name, str) and name
        for name in artifact_logical_names
    ):
        raise ValueError(
            "advisory_preflight.artifact_logical_names "
            "must contain non-empty strings."
        )

    if not all(
        isinstance(policy_id, str) and policy_id
        for policy_id in policy_ids
    ):
        raise ValueError(
            "advisory_preflight.policy_ids must contain "
            "non-empty strings."
        )

    return {
        "artifact_logical_names": artifact_logical_names,
        "policy_ids": policy_ids,
    }


def artifact_logical_name(task):
    task_id = task.get("id")

    if not isinstance(task_id, str) or not task_id:
        raise ValueError(
            "Task must contain a non-empty id."
        )

    return f"advisory_preflight_{task_id}"


def blocked_result(reason, status="blocked"):
    return {
        "status": status,
        "reason": reason,
        "execution_authority": "none",
    }


def validate_existing_preflight(task, existing):
    snapshot = existing.get("snapshot")
    preflight = existing.get("preflight")

    if not isinstance(snapshot, dict):
        return blocked_result(
            "Stored advisory preflight is missing its snapshot."
        )

    if not isinstance(preflight, dict):
        return blocked_result(
            "Stored advisory preflight is missing its advisory."
        )

    snapshot_validation = validate_scoped_snapshot(snapshot)

    if snapshot_validation.get("status") != "valid":
        return {
            **blocked_result(
                "Stored advisory preflight snapshot is stale."
            ),
            "snapshot_validation": snapshot_validation,
            "preflight_validation": None,
        }

    preflight_validation = validate_advisory_preflight(
        preflight,
        task,
    )

    if preflight_validation.get("status") != "valid":
        return {
            **blocked_result(
                "Stored advisory preflight is not dispatchable.",
                preflight_validation.get("status", "blocked"),
            ),
            "snapshot_validation": snapshot_validation,
            "preflight_validation": preflight_validation,
        }

    return {
        "status": "allowed",
        "reason": "Stored advisory preflight remains valid.",
        "snapshot": snapshot,
        "preflight": preflight,
        "snapshot_validation": snapshot_validation,
        "preflight_validation": preflight_validation,
        "artifact": existing.get("artifact"),
        "execution_authority": "none",
    }


def run_advisory_preflight(task, existing=None):
    config = get_advisory_config(task)

    if config is None:
        return {
            "status": "not_required",
            "reason": "Task does not enable advisory preflight.",
            "execution_authority": "none",
        }

    if existing:
        return validate_existing_preflight(task, existing)

    if not task.get("requires_approval", False):
        return blocked_result(
            "Advisory-preflight tasks must require "
            "human approval."
        )

    snapshot = build_scoped_snapshot(
        requested_task_ids=[task["id"]],
        artifact_logical_names=config[
            "artifact_logical_names"
        ],
        policy_ids=config["policy_ids"],
    )

    advisory_result = request_advisory(task)

    preflight = create_advisory_preflight(
        task,
        advisory_result,
    )

    snapshot_validation = validate_scoped_snapshot(snapshot)

    preflight_validation = validate_advisory_preflight(
        preflight,
        task,
    )

    artifact_payload = {
        "artifact_version": "1.0",
        "task_id": task["id"],
        "snapshot": snapshot,
        "preflight": preflight,
        "snapshot_validation": snapshot_validation,
        "preflight_validation": preflight_validation,
        "execution_authority": "none",
    }

    staged_path = stage_json(
        artifact_logical_name(task),
        artifact_payload,
    )

    artifact = publish_staged_artifact(
        staged_path=staged_path,
        logical_name=artifact_logical_name(task),
        producer_task_id=task["id"],
        schema_version="1.0",
    )

    if snapshot_validation.get("status") != "valid":
        return {
            **blocked_result(
                "Advisory preflight snapshot became stale."
            ),
            "snapshot": snapshot,
            "preflight": preflight,
            "snapshot_validation": snapshot_validation,
            "preflight_validation": preflight_validation,
            "artifact": artifact,
        }

    if preflight_validation.get("status") != "valid":
        return {
            **blocked_result(
                "Advisory preflight requires a human decision.",
                preflight_validation.get("status", "blocked"),
            ),
            "snapshot": snapshot,
            "preflight": preflight,
            "snapshot_validation": snapshot_validation,
            "preflight_validation": preflight_validation,
            "artifact": artifact,
        }

    return {
        "status": "allowed",
        "reason": (
            "Scoped snapshot and advisory preflight "
            "are valid."
        ),
        "snapshot": snapshot,
        "preflight": preflight,
        "snapshot_validation": snapshot_validation,
        "preflight_validation": preflight_validation,
        "artifact": artifact,
        "execution_authority": "none",
    }
