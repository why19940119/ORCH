from datetime import datetime, timezone
import hashlib
import json

from openrouter_advisory import validate_advisory


def utc_now():
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )


def task_fingerprint(task):
    canonical_task = json.dumps(
        task,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return (
        "sha256:"
        + hashlib.sha256(canonical_task).hexdigest()
    )


def create_advisory_preflight(task, advisory_result):
    if not isinstance(task, dict):
        raise ValueError("task must be an object.")

    if not isinstance(advisory_result, dict):
        raise ValueError(
            "advisory_result must be an object."
        )

    provider = advisory_result.get("provider")
    response_model = advisory_result.get("response_model")
    response_id = advisory_result.get("response_id")

    if not isinstance(provider, str) or not provider:
        raise ValueError(
            "advisory_result.provider is required."
        )

    if (
        not isinstance(response_model, str)
        or not response_model
    ):
        raise ValueError(
            "advisory_result.response_model is required."
        )

    if not isinstance(response_id, str) or not response_id:
        raise ValueError(
            "advisory_result.response_id is required."
        )

    advisory = validate_advisory(
        advisory_result.get("advisory")
    )

    recommended_action = advisory["recommended_action"]

    if recommended_action == "request_human_review":
        status = "human_review_required"
    elif recommended_action == "no_action":
        status = "no_action_recommended"
    else:
        status = "advisory_ready"

    return {
        "status": status,
        "created_at_utc": utc_now(),
        "task_id": task.get("task_id") or task.get("id"),
        "task_fingerprint": task_fingerprint(task),
        "provider": provider,
        "response_model": response_model,
        "response_id": response_id,
        "usage": advisory_result.get("usage", {}),
        "advisory": advisory,
        "execution_authority": "none",
    }


def validate_advisory_preflight(preflight, current_task):
    if not isinstance(preflight, dict):
        raise ValueError("preflight must be an object.")

    if preflight.get("execution_authority") != "none":
        return {
            "status": "invalid",
            "reason": "invalid_execution_authority",
        }

    expected_fingerprint = preflight.get(
        "task_fingerprint"
    )

    current_fingerprint = task_fingerprint(current_task)

    if expected_fingerprint != current_fingerprint:
        return {
            "status": "stale",
            "reason": "task_fingerprint_changed",
            "original_task_fingerprint": (
                expected_fingerprint
            ),
            "current_task_fingerprint": (
                current_fingerprint
            ),
        }

    status = preflight.get("status")

    if status == "human_review_required":
        return {
            "status": "human_review_required",
            "reason": "advisory_requested_human_review",
        }

    if status == "no_action_recommended":
        return {
            "status": "no_action_recommended",
            "reason": "advisory_recommended_no_action",
        }

    if status != "advisory_ready":
        return {
            "status": "invalid",
            "reason": "unsupported_preflight_status",
        }

    return {
        "status": "valid",
        "reason": "advisory_preflight_matches_task",
    }
