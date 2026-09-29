"""ORCH store-data CSV importer (v0.19.0, WP-ORCH-10).

Fixed schema (Google Sheets tabs exported as CSV):

    products.csv  sku, name, price_hkd, stock, category
    orders.csv    order_id, date (YYYY-MM-DD), sku, quantity, amount_hkd
    traffic.csv   date (YYYY-MM-DD), page, pageviews, source

Usage:
    python commerce_import.py            import CSVs from data/import/
    python commerce_import.py --reset    delete imported data (back to sample)
    python commerce_import.py --status   show what is imported
    add --lang en | zh-Hans | zh-Hant    (default zh-Hant) for messages

Rules:
- Valid rows are kept, invalid rows are reported per row (partial import).
- A file whose header is wrong is skipped entirely.
- A UTF-8 BOM (Google Sheets / Excel export) is accepted.
- Files not supplied in a run keep their previously imported rows.
- The normalised result goes to gitignored state/ecom_import.json with
  the import time. Nothing here calls a model or an external system.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import mini_orch
from ui_i18n import DEFAULT_LOCALE, normalize_locale, ui_strings

PROJECT_ROOT = Path(__file__).resolve().parent
IMPORT_DIR = PROJECT_ROOT / "data" / "import"
IMPORT_STATE_FILE = PROJECT_ROOT / "state" / "ecom_import.json"
LOCK_FILE = PROJECT_ROOT / "state" / ".ecom_demo.lock"

IMPORT_VERSION = "v0.19.0"
SCHEMA_VERSION = "1.0"

SCHEMAS = {
    "products": ("sku", "name", "price_hkd", "stock", "category"),
    "orders": ("order_id", "date", "sku", "quantity", "amount_hkd"),
    "traffic": ("date", "page", "pageviews", "source"),
}
FILE_ORDER = ("products", "orders", "traffic")

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ROWS_PER_FILE = 20_000
MAX_TEXT_CHARS = {"sku": 64, "name": 200, "category": 80, "order_id": 64,
                  "page": 300, "source": 80}
MAX_STORED_ERRORS = 500

VELOCITY_WINDOW_DAYS = 30
LOW_COVER_DAYS = 14

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_time_text(value):
    """UTC ISO timestamp -> box-local 'YYYY-MM-DD HH:MM' for CLI output."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return str(value or "")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Parsing / validation
# ---------------------------------------------------------------------------

def _error(file_kind, row, code, field="", value="", **params):
    return {
        "file": file_kind,
        "row": row,
        "field": field,
        "value": str(value)[:80],
        "code": code,
        "params": params,
    }


def _clean_number_text(value):
    text = (value or "").strip().replace(",", "").replace(" ", "")
    for prefix in ("HK$", "hk$", "$"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text


def _parse_decimal(value):
    text = _clean_number_text(value)
    if not re.fullmatch(r"-?\d+(\.\d+)?", text):
        return None
    return round(float(text), 2)


def _parse_int(value):
    text = _clean_number_text(value)
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.0+", text):
        return int(float(text))
    return None


def _parse_date(value):
    text = (value or "").strip()
    if not DATE_RE.fullmatch(text):
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def decode_csv_bytes(data):
    """UTF-8 (with or without BOM). Returns text or None."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _read_rows(file_kind, text, errors):
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        errors.append(_error(file_kind, 1, "empty_file"))
        return None, []
    except csv.Error:
        errors.append(_error(file_kind, 1, "empty_file"))
        return None, []
    normalized = [cell.strip().lstrip("\ufeff").strip().lower() for cell in header]
    expected = list(SCHEMAS[file_kind])
    if sorted(normalized) != sorted(expected) or len(normalized) != len(expected):
        errors.append(
            _error(
                file_kind, 1, "bad_header",
                value=",".join(header)[:80],
                expected=", ".join(expected),
                found=", ".join(cell.strip() for cell in header)[:120] or "-",
            )
        )
        return None, []
    rows = []
    try:
        for line_number, cells in enumerate(reader, start=2):
            if not any((cell or "").strip() for cell in cells):
                continue
            if len(rows) >= MAX_ROWS_PER_FILE:
                errors.append(
                    _error(file_kind, line_number, "too_many_rows",
                           max=MAX_ROWS_PER_FILE)
                )
                break
            if len(cells) != len(normalized):
                errors.append(
                    _error(file_kind, line_number, "wrong_columns",
                           found=len(cells), expected=len(normalized))
                )
                continue
            rows.append((line_number, dict(zip(normalized, cells))))
    except csv.Error as error:
        errors.append(
            _error(file_kind, reader.line_num or 1, "empty_file",
                   value=str(error)[:60])
        )
    return normalized, rows


def _text_field(file_kind, line, record, field, errors):
    value = (record.get(field) or "").strip()
    if not value:
        errors.append(_error(file_kind, line, "missing_value", field))
        return None
    limit = MAX_TEXT_CHARS.get(field, 200)
    if len(value) > limit:
        errors.append(_error(file_kind, line, "too_long", field, value[:40], max=limit))
        return None
    return value


def _number_field(file_kind, line, record, field, errors, integer=False,
                  positive=False):
    raw = record.get(field)
    if not (raw or "").strip():
        errors.append(_error(file_kind, line, "missing_value", field))
        return None
    value = _parse_int(raw) if integer else _parse_decimal(raw)
    if value is None:
        errors.append(
            _error(file_kind, line, "not_integer" if integer else "not_number",
                   field, raw)
        )
        return None
    if value < 0:
        errors.append(_error(file_kind, line, "negative", field, raw))
        return None
    if positive and value == 0:
        errors.append(_error(file_kind, line, "not_positive", field, raw))
        return None
    return value


def _date_field(file_kind, line, record, field, errors):
    raw = record.get(field)
    if not (raw or "").strip():
        errors.append(_error(file_kind, line, "missing_value", field))
        return None
    value = _parse_date(raw)
    if value is None:
        errors.append(_error(file_kind, line, "bad_date", field, raw))
    return value


def validate_products(rows, errors):
    out, seen = [], {}
    for line, record in rows:
        before = len(errors)
        sku = _text_field("products", line, record, "sku", errors)
        name = _text_field("products", line, record, "name", errors)
        price = _number_field("products", line, record, "price_hkd", errors)
        stock = _number_field("products", line, record, "stock", errors, integer=True)
        category = _text_field("products", line, record, "category", errors)
        if len(errors) != before:
            continue
        key = sku.upper()
        if key in seen:
            errors.append(_error("products", line, "duplicate", "sku", sku,
                                 first_row=seen[key]))
            continue
        seen[key] = line
        out.append({"sku": sku, "name": name, "price_hkd": price,
                    "stock": stock, "category": category, "row": line})
    return out


def validate_orders(rows, errors, known_skus):
    out, seen = [], {}
    known = {sku.upper(): sku for sku in known_skus}
    for line, record in rows:
        before = len(errors)
        order_id = _text_field("orders", line, record, "order_id", errors)
        day = _date_field("orders", line, record, "date", errors)
        sku = _text_field("orders", line, record, "sku", errors)
        quantity = _number_field("orders", line, record, "quantity", errors,
                                 integer=True, positive=True)
        amount = _number_field("orders", line, record, "amount_hkd", errors)
        if len(errors) != before:
            continue
        if sku.upper() not in known:
            errors.append(_error("orders", line, "unknown_sku", "sku", sku))
            continue
        key = (order_id.upper(), sku.upper())
        if key in seen:
            errors.append(_error("orders", line, "duplicate", "order_id",
                                 f"{order_id} / {sku}", first_row=seen[key]))
            continue
        seen[key] = line
        out.append({"order_id": order_id, "date": day, "sku": known[sku.upper()],
                    "quantity": quantity, "amount_hkd": amount, "row": line})
    return out


def validate_traffic(rows, errors):
    out, seen = [], {}
    for line, record in rows:
        before = len(errors)
        day = _date_field("traffic", line, record, "date", errors)
        page = _text_field("traffic", line, record, "page", errors)
        pageviews = _number_field("traffic", line, record, "pageviews", errors,
                                  integer=True)
        source = _text_field("traffic", line, record, "source", errors)
        if len(errors) != before:
            continue
        key = (day, page.lower(), source.lower())
        if key in seen:
            errors.append(_error("traffic", line, "duplicate", "date",
                                 f"{day} / {page} / {source}", first_row=seen[key]))
            continue
        seen[key] = line
        out.append({"date": day, "page": page, "pageviews": pageviews,
                    "source": source, "row": line})
    return out


def run_import(files, previous=None, source="cli"):
    """Validate a set of CSV files.

    ``files`` maps ``products``/``orders``/``traffic`` to
    ``(filename, bytes)``. Returns ``(dataset, report)``; ``dataset`` is
    the merged normalised data (previous rows kept for files not given)
    or ``None`` when nothing valid was supplied.
    """
    previous = previous or {}
    errors = []
    file_reports = {}
    parsed = {}

    for kind in FILE_ORDER:
        if kind not in files:
            continue
        filename, data = files[kind]
        report = {"filename": filename, "sha256": hashlib.sha256(data).hexdigest(),
                  "bytes": len(data), "rows_total": 0, "rows_valid": 0,
                  "rows_rejected": 0, "accepted": False}
        file_reports[kind] = report
        if len(data) > MAX_FILE_BYTES:
            errors.append(_error(kind, 0, "file_too_large",
                                 max_mb=MAX_FILE_BYTES // (1024 * 1024)))
            continue
        text = decode_csv_bytes(data)
        if text is None:
            errors.append(_error(kind, 0, "not_utf8"))
            continue
        before = len(errors)
        header, rows = _read_rows(kind, text, errors)
        report["rows_total"] = len(rows) + sum(
            1 for item in errors[before:] if item["code"] == "wrong_columns"
        )
        if header is None:
            continue
        parsed[kind] = rows

    products = previous.get("products") or []
    if "products" in parsed:
        before = len(errors)
        products = validate_products(parsed["products"], errors)
        _finish(file_reports["products"], products, errors[before:])
    known_skus = [item["sku"] for item in products]

    orders = previous.get("orders") or []
    if "orders" in parsed:
        before = len(errors)
        orders = validate_orders(parsed["orders"], errors, known_skus)
        _finish(file_reports["orders"], orders, errors[before:])
    elif "products" in parsed and orders:
        # Products were replaced: drop earlier orders for vanished SKUs.
        known = {sku.upper() for sku in known_skus}
        orders = [row for row in orders if row["sku"].upper() in known]

    traffic = previous.get("traffic") or []
    if "traffic" in parsed:
        before = len(errors)
        traffic = validate_traffic(parsed["traffic"], errors)
        _finish(file_reports["traffic"], traffic, errors[before:])

    accepted_any = any(item.get("accepted") for item in file_reports.values())
    imported_at = utc_now()
    report = {
        "imported_at_utc": imported_at,
        "source": source,
        "files": file_reports,
        "missing_files": [kind for kind in FILE_ORDER if kind not in files],
        "error_count": len(errors),
        "errors": errors[:MAX_STORED_ERRORS],
        "accepted": accepted_any,
    }
    if not accepted_any:
        return None, report

    dataset = {
        "products": products,
        "orders": orders,
        "traffic": traffic,
    }
    return dataset, report


def _finish(file_report, valid_rows, file_errors):
    rejected = len({(e["row"]) for e in file_errors if e["row"]})
    file_report["rows_valid"] = len(valid_rows)
    file_report["rows_rejected"] = rejected
    file_report["rows_total"] = max(file_report["rows_total"],
                                    len(valid_rows) + rejected)
    file_report["accepted"] = len(valid_rows) > 0


# ---------------------------------------------------------------------------
# State (gitignored state/ecom_import.json)
# ---------------------------------------------------------------------------

def load_state():
    path = Path(IMPORT_STATE_FILE)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _atomic_write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=".ecom_import_", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def import_files(files, source="cli"):
    """Validate + store. Returns the report (always stored as last_report)."""
    with mini_orch.state_lock(LOCK_FILE):
        state = load_state() or {}
        previous = state.get("data") if state.get("data") else None
        dataset, report = run_import(files, previous=previous, source=source)
        if dataset is not None:
            state.update(
                {
                    "schema_version": SCHEMA_VERSION,
                    "import_version": IMPORT_VERSION,
                    "imported_at_utc": report["imported_at_utc"],
                    "active": True,
                    "data": dataset,
                    "counts": {key: len(dataset[key]) for key in FILE_ORDER},
                }
            )
        state["last_report"] = report
        _atomic_write(IMPORT_STATE_FILE, state)
    return report


def import_from_folder(folder=None, source="folder"):
    folder = Path(folder or IMPORT_DIR)
    files = {}
    for kind in FILE_ORDER:
        path = folder / f"{kind}.csv"
        if path.is_file():
            files[kind] = (path.name, path.read_bytes()[: MAX_FILE_BYTES + 1])
    if not files:
        return None
    return import_files(files, source=source)


def set_active(active):
    with mini_orch.state_lock(LOCK_FILE):
        state = load_state()
        if not state or not state.get("data"):
            return False
        state["active"] = bool(active)
        _atomic_write(IMPORT_STATE_FILE, state)
    return True


def reset_import():
    with mini_orch.state_lock(LOCK_FILE):
        path = Path(IMPORT_STATE_FILE)
        if path.exists():
            path.unlink()
            return True
    return False


def active_import():
    """The imported dataset when present AND switched on, else None."""
    state = load_state()
    if not state or not state.get("active") or not state.get("data"):
        return None
    return state


# ---------------------------------------------------------------------------
# Products in the shape the demo pages / drafts use
# ---------------------------------------------------------------------------

IMPORTED_CLAIMS_POLICY = (
    "Imported store data: no efficacy, health or 'best/No.1' claims; "
    "anything beyond these fields is [HUMAN TO CONFIRM]."
)


def product_as_sku(row):
    facts = [
        f"Price: HK${row['price_hkd']:g}",
        f"Stock: {row['stock']} units",
        f"Category: {row['category']}",
    ]
    return {
        "sku": row["sku"],
        "category": row["category"],
        "name_en": row["name"],
        "name_zh": row["name"],
        "list_price_hkd": row["price_hkd"],
        "stock_units": row["stock"],
        "approved_facts": facts,
        "claims_policy": IMPORTED_CLAIMS_POLICY,
        "imported": True,
    }


def imported_skus(state=None):
    state = state if state is not None else active_import()
    if not state:
        return None
    products = state["data"].get("products") or []
    if not products:
        return None
    return [product_as_sku(row) for row in products]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _pct(numerator, denominator, digits=1):
    if not denominator:
        return 0.0
    return round(100.0 * numerator / denominator, digits)


def _iso_week(day_text):
    day = date.fromisoformat(day_text)
    year, week, _ = day.isocalendar()
    monday = day - timedelta(days=day.weekday())
    return f"{year}-W{week:02d}", monday.isoformat()


def compute_metrics(state=None, today=None):
    state = state if state is not None else active_import()
    if not state:
        return None
    data = state["data"]
    products = data.get("products") or []
    orders = data.get("orders") or []
    traffic = data.get("traffic") or []
    names = {row["sku"]: row["name"] for row in products}

    revenue = round(sum(row["amount_hkd"] for row in orders), 2)
    order_ids = {row["order_id"] for row in orders}
    units = sum(row["quantity"] for row in orders)
    pageviews = sum(row["pageviews"] for row in traffic)
    order_dates = sorted(row["date"] for row in orders)
    traffic_dates = sorted(row["date"] for row in traffic)

    by_sku = defaultdict(lambda: {"units": 0, "revenue": 0.0, "orders": set()})
    for row in orders:
        item = by_sku[row["sku"]]
        item["units"] += row["quantity"]
        item["revenue"] += row["amount_hkd"]
        item["orders"].add(row["order_id"])
    sales_by_sku = sorted(
        (
            {
                "sku": sku,
                "name": names.get(sku, sku),
                "units": item["units"],
                "orders": len(item["orders"]),
                "revenue": round(item["revenue"], 2),
                "share_pct": _pct(item["revenue"], revenue),
            }
            for sku, item in by_sku.items()
        ),
        key=lambda row: (-row["revenue"], row["sku"]),
    )

    weeks = defaultdict(lambda: {"revenue": 0.0, "orders": set(), "units": 0})
    week_start = {}
    for row in orders:
        label, monday = _iso_week(row["date"])
        week_start[label] = monday
        weeks[label]["revenue"] += row["amount_hkd"]
        weeks[label]["orders"].add(row["order_id"])
        weeks[label]["units"] += row["quantity"]
    peak = max((item["revenue"] for item in weeks.values()), default=0) or 1
    revenue_by_week = [
        {
            "week": label,
            "week_start": week_start[label],
            "revenue": round(weeks[label]["revenue"], 2),
            "orders": len(weeks[label]["orders"]),
            "units": weeks[label]["units"],
            "bar_pct": int(100 * weeks[label]["revenue"] / peak),
        }
        for label in sorted(weeks)
    ]

    # Days of cover: stock / average daily units over the last N days
    # (window ends on the latest order date, not "today").
    window_end = date.fromisoformat(order_dates[-1]) if order_dates else None
    window_start = (
        window_end - timedelta(days=VELOCITY_WINDOW_DAYS - 1) if window_end else None
    )
    recent_units = defaultdict(int)
    if window_end:
        for row in orders:
            day = date.fromisoformat(row["date"])
            if window_start <= day <= window_end:
                recent_units[row["sku"]] += row["quantity"]
    stock_cover = []
    for row in products:
        raw_velocity = recent_units.get(row["sku"], 0) / VELOCITY_WINDOW_DAYS
        velocity = round(raw_velocity, 2)
        if row["stock"] == 0:
            status, cover = "out", 0.0
        elif raw_velocity <= 0:
            status, cover = "no_sales", None
        else:
            cover = round(row["stock"] / raw_velocity, 1)
            status = "low" if cover < LOW_COVER_DAYS else "ok"
        stock_cover.append(
            {
                "sku": row["sku"],
                "name": row["name"],
                "stock": row["stock"],
                "units_window": recent_units.get(row["sku"], 0),
                "velocity_per_day": velocity,
                "days_of_cover": cover,
                "status": status,
            }
        )
    order_rank = {"out": 0, "low": 1, "ok": 2, "no_sales": 3}
    stock_cover.sort(
        key=lambda row: (
            order_rank[row["status"]],
            row["days_of_cover"] if row["days_of_cover"] is not None else 1e9,
            row["sku"],
        )
    )

    by_source = defaultdict(int)
    for row in traffic:
        by_source[row["source"]] += row["pageviews"]
    traffic_by_source = sorted(
        (
            {"source": source, "pageviews": views, "share_pct": _pct(views, pageviews)}
            for source, views in by_source.items()
        ),
        key=lambda row: (-row["pageviews"], row["source"]),
    )

    conversion = None
    if order_dates and traffic_dates:
        start = max(order_dates[0], traffic_dates[0])
        end = min(order_dates[-1], traffic_dates[-1])
        if start <= end:
            overlap_orders = {
                row["order_id"] for row in orders if start <= row["date"] <= end
            }
            overlap_views = sum(
                row["pageviews"] for row in traffic if start <= row["date"] <= end
            )
            conversion = {
                "start": start,
                "end": end,
                "orders": len(overlap_orders),
                "pageviews": overlap_views,
                "rate_pct": _pct(len(overlap_orders), overlap_views, 2),
            }

    return {
        "imported_at_utc": state.get("imported_at_utc"),
        "totals": {
            "revenue": revenue,
            "orders": len(order_ids),
            "order_lines": len(orders),
            "units": units,
            "pageviews": pageviews,
            "products": len(products),
            "aov": round(revenue / len(order_ids), 2) if order_ids else 0.0,
        },
        "order_period": [order_dates[0], order_dates[-1]] if order_dates else None,
        "traffic_period": [traffic_dates[0], traffic_dates[-1]] if traffic_dates else None,
        "sales_by_sku": sales_by_sku,
        "revenue_by_week": revenue_by_week,
        "stock_cover": stock_cover,
        "velocity_window_days": VELOCITY_WINDOW_DAYS,
        "low_cover_days": LOW_COVER_DAYS,
        "traffic_by_source": traffic_by_source,
        "conversion": conversion,
    }


def metrics_digest(metrics, sku=None):
    """Compact, model-safe summary used as draft source."""
    digest = {
        "totals": metrics["totals"],
        "order_period": metrics["order_period"],
        "top_skus": [
            {k: row[k] for k in ("sku", "name", "units", "revenue", "share_pct")}
            for row in metrics["sales_by_sku"][:5]
        ],
        "low_cover": [
            {k: row[k] for k in ("sku", "name", "stock", "velocity_per_day",
                                 "days_of_cover", "status")}
            for row in metrics["stock_cover"]
            if row["status"] in {"out", "low"}
        ][:5],
        "traffic_by_source": metrics["traffic_by_source"][:5],
        "conversion": metrics["conversion"],
        "revenue_last_weeks": [
            {k: row[k] for k in ("week", "revenue", "orders")}
            for row in metrics["revenue_by_week"][-4:]
        ],
    }
    if sku:
        digest["sku_sales"] = next(
            (row for row in metrics["sales_by_sku"] if row["sku"] == sku), None
        )
        digest["sku_cover"] = next(
            (row for row in metrics["stock_cover"] if row["sku"] == sku), None
        )
    return digest


# ---------------------------------------------------------------------------
# Localised messages
# ---------------------------------------------------------------------------

def error_message(error, t):
    template = t.get(f"imp_err_{error.get('code')}", t["imp_err_generic"])
    field_label = t.get(f"imp_field_{error.get('field')}", error.get("field") or "")
    params = {
        "field": field_label,
        "value": error.get("value", ""),
        **(error.get("params") or {}),
    }
    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError):
        return t["imp_err_generic"]


def print_report(report, locale):
    t = ui_strings(locale)
    for kind in FILE_ORDER:
        info = report["files"].get(kind)
        label = t[f"imp_file_{kind}"]
        if not info:
            print(t["imp_cli_file_missing"].format(file=label))
            continue
        print(
            t["imp_cli_summary"].format(
                file=label,
                valid=info["rows_valid"],
                total=info["rows_total"],
                rejected=info["rows_rejected"],
            )
        )
    for error in report["errors"]:
        print(
            t["imp_cli_error_line"].format(
                file=t[f"imp_file_{error['file']}"],
                row=error["row"] or "-",
                message=error_message(error, t),
            )
        )
    hidden = report["error_count"] - len(report["errors"])
    if hidden > 0:
        print(t["imp_errors_more"].format(n=hidden))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    locale = DEFAULT_LOCALE
    if "--lang" in argv:
        index = argv.index("--lang")
        if index + 1 < len(argv):
            locale = normalize_locale(argv[index + 1]) or DEFAULT_LOCALE
            del argv[index:index + 2]
    t = ui_strings(locale)

    if argv == ["--reset"]:
        print(t["imp_cli_reset_done"] if reset_import() else t["imp_cli_reset_none"])
        return 0
    if argv == ["--status"]:
        state = load_state()
        if not state or not state.get("data"):
            print(t["imp_status_none"])
        else:
            print(
                (t["imp_status_active"] if state.get("active") else t["imp_status_inactive"])
                + " · "
                + t["imp_cli_counts"].format(**state.get("counts", {}),
                                             time=local_time_text(state.get("imported_at_utc")))
            )
        return 0
    if argv:
        print(t["imp_cli_usage"])
        return 2

    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = import_from_folder()
    if report is None:
        print(t["imp_cli_no_files"].format(path=IMPORT_DIR))
        return 1
    print_report(report, locale)
    if report["accepted"]:
        print(t["imp_cli_saved"].format(path=IMPORT_STATE_FILE,
                                        time=local_time_text(report["imported_at_utc"])))
        return 0
    print(t["imp_msg_nothing_valid"])
    return 1


if __name__ == "__main__":
    sys.exit(main())
