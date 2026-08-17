from datetime import datetime, timezone
from pathlib import Path
import json
import sys

CONFIG_FILE = Path("market_data_provider_config.json")
SUMMARY_FILE = Path("output/market_summary.json")
OUTPUT_FILE = Path("output/market_data_refresh.json")


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


errors = []

if not CONFIG_FILE.exists():
    errors.append("market_data_provider_config.json does not exist.")

if not SUMMARY_FILE.exists():
    errors.append("market_summary.json does not exist.")

config = {}
summary = {}

if not errors:
    try:
        config = load_json(CONFIG_FILE)
    except json.JSONDecodeError as error:
        errors.append(f"Provider config is invalid JSON: {error}")

    try:
        summary = load_json(SUMMARY_FILE)
    except json.JSONDecodeError as error:
        errors.append(f"Market summary is invalid JSON: {error}")

provider_id = config.get("provider_id")
refresh_mode = config.get("refresh_mode")
external_api_allowed = config.get("external_api_allowed")
supports_external_refresh = config.get("supports_external_refresh")
threshold_minutes = config.get("freshness_threshold_minutes")

price_snapshot = summary.get("price_snapshot", {})
snapshot_saved_at = price_snapshot.get("saved_at")

snapshot_age_minutes = None

if snapshot_saved_at:
    try:
        saved_at = datetime.fromisoformat(
            snapshot_saved_at.replace("Z", "+00:00")
        )
        snapshot_age_minutes = int(
            (datetime.now(timezone.utc) - saved_at.astimezone(timezone.utc))
            .total_seconds()
            // 60
        )
    except ValueError:
        errors.append("Price snapshot saved_at is not a valid ISO timestamp.")
else:
    errors.append("Price snapshot saved_at is missing.")

fresh_snapshot_available = (
    not errors
    and isinstance(threshold_minutes, int)
    and snapshot_age_minutes is not None
    and snapshot_age_minutes <= threshold_minutes
)

refresh_performed = False

if refresh_mode != "revalidate_only":
    errors.append("Only refresh_mode=revalidate_only is supported in v0.8a.")

if external_api_allowed is not False:
    errors.append("v0.8a requires external_api_allowed=false.")

if supports_external_refresh is not False:
    errors.append("v0.8a requires supports_external_refresh=false.")

if fresh_snapshot_available:
    refresh_status = "fresh_snapshot_revalidated"
    message = (
        "Local snapshot was revalidated and remains within the "
        "freshness threshold."
    )
else:
    refresh_status = "no_fresh_snapshot_available"
    message = (
        "No fresh market snapshot is available. "
        "v0.8a did not call an external API and did not claim a refresh."
    )

output = {
    "status": "success" if not errors else "failed",
    "task": "task_market_data_refresh_interface_014",
    "generated_at_utc": datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ),
    "provider": {
        "provider_id": provider_id,
        "provider_kind": config.get("provider_kind"),
        "refresh_mode": refresh_mode,
        "external_api_allowed": external_api_allowed,
        "supports_external_refresh": supports_external_refresh,
    },
    "refresh_performed": refresh_performed,
    "external_api_called": False,
    "refresh_status": refresh_status,
    "fresh_snapshot_available": fresh_snapshot_available,
    "freshness_threshold_minutes": threshold_minutes,
    "snapshot_saved_at": snapshot_saved_at,
    "snapshot_age_minutes_recalculated": snapshot_age_minutes,
    "safe_for_alerting": False,
    "safe_for_trading": False,
    "errors": errors,
    "message": message,
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
