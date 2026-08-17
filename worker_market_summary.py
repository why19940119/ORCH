from datetime import datetime, timezone
from pathlib import Path
import json
import statistics
import sys

RADAR_DIR = Path.home() / "projects" / "umm-v2-radar"

PRICE_FILE = RADAR_DIR / "stock_move_radar_prices.json"
WATCHLIST_FILE = RADAR_DIR / "watchlist.json"

OUTPUT_FILE = Path("output/market_summary.json")

FRESHNESS_THRESHOLD_MINUTES = 60


def load_json(file_path):
    if not file_path.exists():
        print(f"Required source file not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    return json.loads(file_path.read_text(encoding="utf-8"))


price_snapshot = load_json(PRICE_FILE)
watchlist = load_json(WATCHLIST_FILE)

saved_at_text = price_snapshot.get("saved_at")
session = price_snapshot.get("session")
prices = price_snapshot.get("prices", {})

if not isinstance(prices, dict) or not prices:
    print("Price snapshot contains no usable price dictionary.", file=sys.stderr)
    sys.exit(1)

try:
    saved_at = datetime.fromisoformat(saved_at_text)
except (TypeError, ValueError):
    print("Price snapshot saved_at value is invalid.", file=sys.stderr)
    sys.exit(1)

now_utc = datetime.now(timezone.utc)
age_minutes = max(
    0,
    round((now_utc - saved_at.astimezone(timezone.utc)).total_seconds() / 60),
)

valid_prices = []

for ticker, record in prices.items():
    price = record.get("price")

    if isinstance(price, (int, float)):
        valid_prices.append(
            {
                "ticker": ticker,
                "price": round(float(price), 2),
                "trade_time": record.get("trade_time"),
            }
        )

if not valid_prices:
    print("No numeric prices were found in the snapshot.", file=sys.stderr)
    sys.exit(1)

prices_only = [item["price"] for item in valid_prices]

top_five = sorted(
    valid_prices,
    key=lambda item: item["price"],
    reverse=True,
)[:5]

bottom_five = sorted(
    valid_prices,
    key=lambda item: item["price"],
)[:5]

is_fresh = age_minutes <= FRESHNESS_THRESHOLD_MINUTES

summary = {
    "status": "success",
    "task": "task_market_summary_010",
    "generated_at_utc": now_utc.isoformat(timespec="seconds"),
    "source_policy": {
        "source_type": "local_read_only_snapshot",
        "safe_for_alerting": False,
        "safe_for_trading": False,
        "external_api_called": False,
    },
    "data_quality": {
        "status": "fresh_snapshot" if is_fresh else "stale_snapshot",
        "freshness_threshold_minutes": FRESHNESS_THRESHOLD_MINUTES,
        "snapshot_age_minutes": age_minutes,
        "reason": (
            "Price snapshot is within the freshness threshold."
            if is_fresh
            else "Price snapshot is older than the freshness threshold."
        ),
    },
    "price_snapshot": {
        "source_file": str(PRICE_FILE),
        "saved_at": saved_at_text,
        "session": session,
        "ticker_count": len(valid_prices),
    },
    "watchlist_metadata": {
        "source_file": str(WATCHLIST_FILE),
        "version": watchlist.get("version"),
        "generated_at_et": watchlist.get("generated_at_et"),
        "generated_at_hkt": watchlist.get("generated_at_hkt"),
        "market_data_as_of_et": watchlist.get("market_data_as_of_et"),
        "session": watchlist.get("session"),
        "feed": watchlist.get("feed"),
        "data_delayed": watchlist.get("data_delayed"),
        "delay_minutes": watchlist.get("delay_minutes"),
        "universe_size": watchlist.get("universe_size"),
    },
    "price_statistics": {
        "minimum": round(min(prices_only), 2),
        "maximum": round(max(prices_only), 2),
        "average": round(statistics.mean(prices_only), 2),
        "median": round(statistics.median(prices_only), 2),
    },
    "top_five_by_price": top_five,
    "bottom_five_by_price": bottom_five,
}

OUTPUT_FILE.write_text(
    json.dumps(summary, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print(f"Market summary completed: {OUTPUT_FILE}")
print(
    f"Data quality: {summary['data_quality']['status']} "
    f"({age_minutes} minutes old)"
)
print(f"Tickers summarized: {len(valid_prices)}")
