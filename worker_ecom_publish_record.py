"""v0.18.0 demo worker: confirm a human-approved e-commerce draft.

Runs only after the mini_orch approval gate lets the task through.
It makes NO external call: it checks that the approved version has an
``ecom_audit`` artifact with a publish channel recorded, and prints a
summary. "Publishing" in the demo is the audit record itself.
"""

import json
import sys
from pathlib import Path

import commerce_demo
import orch_db


def main():
    if len(sys.argv) != 2 or not commerce_demo.is_demo_task_id(sys.argv[1]):
        print("usage: worker_ecom_publish_record.py <task_ecom_...>",
              file=sys.stderr)
        return 2

    task_id = sys.argv[1]
    statuses = orch_db.load(commerce_demo.STATUS_FILE, {}) or {}   # v0.21.0
    state = statuses.get(task_id) or {}
    decision = (state.get("ecom") or {}).get("decision") or {}

    if state.get("approval_status") != "approved" or not decision:
        print(f"{task_id} has no human approval; refusing.", file=sys.stderr)
        return 1

    audit = commerce_demo.read_artifact(decision.get("audit_artifact_id"))
    if not audit or audit.get("decision") != "approved":
        print(f"{task_id} audit record missing.", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "status": "success",
                "task_id": task_id,
                "publish_mode": audit.get("publish_mode"),
                "publish_channel": audit.get("publish_channel"),
                "version": audit.get("version"),
                "approver": audit.get("approver"),
                "audit_artifact_id": decision.get("audit_artifact_id"),
                "external_call": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
