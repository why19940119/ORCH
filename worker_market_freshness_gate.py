from datetime import datetime, timezone
from pathlib import Path
import json
import sys

SOURCE_FILE = Path("output/market_summary.json")
VALIDATION_FILE = Path("output/market_summary_validation.json")
OUTPUT_FILE = Path("output/market_freshness_gate.json")

errors = []


def fail(message):
    errors.append(message)


if not SOURCE_FILE.exists():
    fail("market_summary.json does not exist.")

if not VALIDATION_FILE.exists():
    fail("market_summary_validation.json does not exist.")

summary = {}
validation = {}

if SOURCE_FILE.exists():
    try:
        summary = json.loads(SOURCE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        fail(f"market_summary.json is invalid JSON: {error}")

if VALIDATION_FILE.exists():
    try:
        validation = json.loads(VALIDATION_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        fail(f"market_summary_validation.json is invalid JSON: {error}")

if validation.get("status") != "success":
    fail("Market summary validation did not pass.")

data_quality = summary.get("data_quality", {})
source_policy = summary.get("source_policy", {})

quality_status = data_quality.get("status")
snapshot_age_minutes = data_quality.get("snapshot_age_minutes")
safe_for_alerting = source_policy.get("safe_for_alerting")
safe_for_trading = source_policy.get("safe_for_trading")

allow_alerting = (
    not errors
    and quality_status == "fresh_snapshot"
    and safe_for_alerting is True
)

allow_trading = (
    not errors
    and quality_status == "fresh_snapshot"
    and safe_for_trading is True
)

reasons = []

if errors:
    reasons.extend(errors)

if quality_status != "fresh_snapshot":
    reasons.append(
        f"Market data quality is {quality_status}; "
        "fresh market data is required."
    )

if safe_for_alerting is not True:
    reasons.append(
        "Source policy does not permit alerting."
    )

if safe_for_trading is not True:
    reasons.append(
        "Source policy does not permit trading."
    )

gate_status = (
    "open"
    if allow_alerting and allow_trading
    else "blocked"
)

output = {
    "status": "success" if not errors else "failed",
    "task": "task_market_freshness_gate_012",
    "generated_at_utc": datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ),
    "gate_status": gate_status,
    "allow_alerting": allow_alerting,
    "allow_trading": allow_trading,
    "market_data_quality": quality_status,
    "snapshot_age_minutes": snapshot_age_minutes,
    "source_policy": {
        "safe_for_alerting": safe_for_alerting,
        "safe_for_trading": safe_for_trading,
    },
    "reasons": reasons,
    "message": (
        "Market freshness gate is open."
        if gate_status == "open"
        else "Market freshness gate is blocked; no alerting or trading is allowed."
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
