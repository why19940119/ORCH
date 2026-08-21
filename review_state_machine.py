from datetime import datetime, timezone
import uuid


def utc_now():
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )


def add_history(workflow, event, details=None):
    workflow["history"].append(
        {
            "timestamp_utc": utc_now(),
            "event": event,
            "details": details or {},
        }
    )


def create_review_workflow(
    max_revision_cycles=1,
    max_review_attempts=2,
):
    workflow = {
        "workflow_id": f"review_{uuid.uuid4().hex}",
        "state": "review_pending",
        "draft_revision": 1,
        "review_attempts": 0,
        "revision_cycles": 0,
        "max_revision_cycles": max_revision_cycles,
        "max_review_attempts": max_review_attempts,
        "deadlock_reason": None,
        "history": [],
    }

    add_history(
        workflow,
        "draft_generated",
        {"draft_revision": 1},
    )

    add_history(
        workflow,
        "review_pending",
        {"review_attempt": 1},
    )

    return workflow


def submit_review(workflow, verdict):
    if workflow.get("state") != "review_pending":
        raise ValueError(
            "Review can only be submitted while state is "
            "review_pending."
        )

    allowed_verdicts = {
        "pass",
        "needs_revision",
        "insufficient_evidence",
        "human_decision_required",
    }

    if verdict not in allowed_verdicts:
        raise ValueError(f"Unsupported review verdict: {verdict}")

    workflow["review_attempts"] += 1

    add_history(
        workflow,
        "review_submitted",
        {
            "review_attempt": workflow["review_attempts"],
            "draft_revision": workflow["draft_revision"],
            "verdict": verdict,
        },
    )

    if verdict == "pass":
        workflow["state"] = "review_passed"

        add_history(
            workflow,
            "review_passed",
            {
                "draft_revision": workflow["draft_revision"],
            },
        )

        return workflow

    if verdict in {
        "insufficient_evidence",
        "human_decision_required",
    }:
        workflow["state"] = "human_review_required"

        add_history(
            workflow,
            "human_review_required",
            {"reason": verdict},
        )

        return workflow

    revision_budget_exhausted = (
        workflow["revision_cycles"]
        >= workflow["max_revision_cycles"]
    )

    review_budget_exhausted = (
        workflow["review_attempts"]
        >= workflow["max_review_attempts"]
    )

    if revision_budget_exhausted or review_budget_exhausted:
        workflow["state"] = "review_deadlock"

        if revision_budget_exhausted:
            workflow["deadlock_reason"] = (
                "max_revision_cycles_exhausted"
            )
        else:
            workflow["deadlock_reason"] = (
                "max_review_attempts_exhausted"
            )

        add_history(
            workflow,
            "review_deadlock",
            {
                "reason": workflow["deadlock_reason"],
            },
        )

        workflow["state"] = "human_review_required"

        add_history(
            workflow,
            "human_review_required",
            {
                "reason": workflow["deadlock_reason"],
            },
        )

        return workflow

    workflow["state"] = "revision_allowed"

    add_history(
        workflow,
        "revision_allowed",
        {
            "next_revision_cycle": (
                workflow["revision_cycles"] + 1
            ),
        },
    )

    return workflow


def submit_revision(workflow):
    if workflow.get("state") != "revision_allowed":
        raise ValueError(
            "Revision can only be submitted while state is "
            "revision_allowed."
        )

    workflow["revision_cycles"] += 1
    workflow["draft_revision"] += 1
    workflow["state"] = "review_pending"

    add_history(
        workflow,
        "draft_generated",
        {
            "draft_revision": workflow["draft_revision"],
            "revision_cycle": workflow["revision_cycles"],
        },
    )

    add_history(
        workflow,
        "review_pending",
        {
            "review_attempt": (
                workflow["review_attempts"] + 1
            ),
        },
    )

    return workflow
