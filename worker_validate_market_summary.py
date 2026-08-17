from datetime import datetime, timezone
from pathlib import Path
import json
import sys

SOURCE_FILE = Path("output/market_summary.json")
OUTPUT_FILE = Path("output/market_summary_validation.json")

errors = []


def add_error(message):
    errors.append(message)


if not SOURCE_FILE.exists():
    print("Validation failed: market_summary.json does not exist.", file=sys.stderr)
    sys.exit(1)

try:
    summary = json.loads(SOURCE_FILE.read_text(encoding="utf-8"))
except json.JSONDecodeError as error:
    print(f"Validation failed: invalid JSON: {error}", file=sys.stderr)
    sys.exit(1)

if summary.get("status") != "success":
    add_error("Summary status must be success.")

if summary.get("task") != "task_market_summary_010":
    add_error("Summary task ID is incorrect.")

source_policy = summary.get("source_policy", {})

if source_policy.get("source_type") != "local_read_only_snapshot":
    add_error("Source type must be local_read_only_snapshot.")

if source_policy.get("external_api_called") is not False:
    add_error("external_api_called must be false.")

if source_policy.get("safe_for_alerting") is not False:
    add_error("safe_for_alerting must be false.")

if source_policy.get("safe_for_trading") is not False:
    add_error("safe_for_trading must be false.")

data_quality = summary.get("data_quality", {})
quality_status = data_quality.get("status")

if quality_status not in {"fresh_snapshot", "stale_snapshot"}:
    add_error("Data quality status is invalid.")

if not isinstance(data_quality.get("snapshot_age_minutes"), int):
    add_error("snapshot_age_minutes must be an integer.")

price_snapshot = summary.get("price_snapshot", {})

if not isinstance(price_snapshot.get("ticker_count"), int):
    add_error("ticker_count must be an integer.")

if price_snapshot.get("ticker_count", 0) <= 0:
    add_error("ticker_count must be greater than zero.")

if not price_snapshot.get("saved_at"):
    add_error("Price snapshot saved_at is missing.")

if not price_snapshot.get("session"):
    add_error("Price snapshot session is missing.")

price_statistics = summary.get("price_statistics", {})

for field in ["minimum", "maximum", "average", "median"]:
    if not isinstance(price_statistics.get(field), (int, float)):
        add_error(f"Price statistic is missing or invalid: {field}")

if (
    isinstance(price_statistics.get("minimum"), (int, float))
    and isinstance(price_statistics.get("maximum"), (int, float))
    and price_statistics["minimum"] > price_statistics["maximum"]
):
    add_error("Minimum price cannot be greater than maximum price.")

top_five = summary.get("top_five_by_price", [])
bottom_five = summary.get("bottom_five_by_price", [])

if not isinstance(top_five, list) or not top_five:
    add_error("top_five_by_price must contain data.")

if not isinstance(bottom_five, list) or not bottom_five:
    add_error("bottom_five_by_price must contain data.")

validation = {
    "status": "success" if not errors else "failed",
    "task": "task_validate_market_summary_011",
    "validated_input": str(SOURCE_FILE),
    "validated_at_utc": datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ),
    "summary_data_quality": quality_status,
    "ticker_count": price_snapshot.get("ticker_count"),
    "errors": errors,
    "message": (
        "Market summary passed safety and structure validation."
        if not errors
        else "Market summary failed validation."
    ),
}

OUTPUT_FILE.write_text(
    json.dumps(validation, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print(validation["message"])

if errors:
    for error in errors:
        print(f"- {error}", file=sys.stderr)

    sys.exit(1)
