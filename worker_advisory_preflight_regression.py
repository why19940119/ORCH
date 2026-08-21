from datetime import datetime, timezone
from pathlib import Path
import json
import sys

from advisory_preflight import (
    create_advisory_preflight,
    validate_advisory_preflight,
)


output_file = Path(
    "output/advisory_preflight_regression.json"
)

errors = []

task = {
    "task_id": "task_advisory_preflight_regression_023",
    "title": "Verify advisory preflight task binding",
    "command": [
        "python3",
        "worker_advisory_preflight_regression.py",
    ],
    "priority": 23,
    "approval_required": False,
    "required_policies": [],
}

advisory_only_result = {
    "provider": "openrouter",
    "response_model": "mistralai/mistral-medium-3.1",
    "response_id": "deterministic-advisory-001",
    "usage": {
        "total_tokens": 0,
        "cost": 0,
    },
    "advisory": {
        "summary": "Task is advisory-only.",
        "risks": ["No execution authority."],
        "recommended_action": "advisory_only",
        "confidence": 1.0,
    },
}

preflight = create_advisory_preflight(
    task,
    advisory_only_result,
)

matching_validation = validate_advisory_preflight(
    preflight,
    task,
)

if matching_validation.get("status") != "valid":
    errors.append(
        "Matching task must validate advisory preflight."
    )

changed_task = {
    **task,
    "title": "Modified task after advisory generation",
}

stale_validation = validate_advisory_preflight(
    preflight,
    changed_task,
)

if stale_validation.get("status") != "stale":
    errors.append(
        "Modified task must stale advisory preflight."
    )

if stale_validation.get("reason") != (
    "task_fingerprint_changed"
):
    errors.append(
        "Stale preflight reason must identify task fingerprint."
    )

human_review_result = {
    **advisory_only_result,
    "response_id": "deterministic-advisory-002",
    "advisory": {
        **advisory_only_result["advisory"],
        "recommended_action": "request_human_review",
    },
}

human_preflight = create_advisory_preflight(
    task,
    human_review_result,
)

human_validation = validate_advisory_preflight(
    human_preflight,
    task,
)

if human_validation.get("status") != (
    "human_review_required"
):
    errors.append(
        "Human-review advisory must require human review."
    )

if preflight.get("execution_authority") != "none":
    errors.append(
        "Preflight must never grant execution authority."
    )

output = {
    "status": "success" if not errors else "failed",
    "task": "task_advisory_preflight_regression_023",
    "tested_at_utc": datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ),
    "preflight": preflight,
    "matching_validation": matching_validation,
    "stale_validation": stale_validation,
    "human_validation": human_validation,
    "errors": errors,
    "message": (
        "Advisory preflight regression passed."
        if not errors
        else "Advisory preflight regression failed."
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
