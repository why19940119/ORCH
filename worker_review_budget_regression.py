from datetime import datetime, timezone
from pathlib import Path
import json
import sys

from review_state_machine import (
    create_review_workflow,
    submit_review,
    submit_revision,
)

output_file = Path("output/review_budget_regression.json")
errors = []

workflow = create_review_workflow(
    max_revision_cycles=1,
    max_review_attempts=2,
)

submit_review(workflow, "needs_revision")

if workflow.get("state") != "revision_allowed":
    errors.append(
        "First needs_revision must allow one revision cycle."
    )

submit_revision(workflow)

if workflow.get("state") != "review_pending":
    errors.append(
        "Submitted revision must return to review_pending."
    )

submit_review(workflow, "needs_revision")

if workflow.get("state") != "human_review_required":
    errors.append(
        "Second needs_revision must require human review."
    )

if workflow.get("deadlock_reason") != (
    "max_revision_cycles_exhausted"
):
    errors.append(
        "Deadlock reason must be max_revision_cycles_exhausted."
    )

if workflow.get("draft_revision") != 2:
    errors.append("Draft revision must equal 2.")

if workflow.get("review_attempts") != 2:
    errors.append("Review attempts must equal 2.")

if workflow.get("revision_cycles") != 1:
    errors.append("Revision cycles must equal 1.")

history_events = [
    item.get("event")
    for item in workflow.get("history", [])
]

for required_event in [
    "revision_allowed",
    "review_deadlock",
    "human_review_required",
]:
    if required_event not in history_events:
        errors.append(
            f"Workflow history is missing event: {required_event}"
        )

output = {
    "status": "success" if not errors else "failed",
    "task": "task_review_budget_regression_022",
    "tested_at_utc": datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ),
    "workflow": workflow,
    "errors": errors,
    "message": (
        "Bounded review deadlock regression passed."
        if not errors
        else "Bounded review deadlock regression failed."
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
