from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import fcntl
import hashlib
import json
import os
import re
import uuid

from artifact_store import (
    publish_staged_artifact,
    stage_json,
)


PROJECT_ROOT = Path(__file__).resolve().parent
STATE_DIR = PROJECT_ROOT / "state"

BUDGET_FILE = STATE_DIR / "chat_budget.json"
USAGE_FILE = STATE_DIR / "chat_usage.jsonl"
LOCK_FILE = STATE_DIR / ".chat_budget.lock"

BUDGET_VERSION = "1.0"
AUDIT_SCHEMA_VERSION = "1.0"

OPENROUTER_KEY_PATTERN = re.compile(
    r"\bsk-[A-Za-z0-9_-]{8,}\b"
)

BEARER_TOKEN_PATTERN = re.compile(
    r"(?i)\bBearer\s+"
    r"[A-Za-z0-9._~+/=-]{8,}"
)

JWT_PATTERN = re.compile(
    r"\beyJ[A-Za-z0-9_-]{5,}"
    r"\.[A-Za-z0-9_-]{5,}"
    r"\.[A-Za-z0-9_-]{5,}\b"
)


class ChatBudgetError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )


def utc_date():
    return datetime.now(timezone.utc).date().isoformat()


def canonical_bytes(data):
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_value(data):
    if isinstance(data, str):
        content = data.encode("utf-8")
    else:
        content = canonical_bytes(data)

    return "sha256:" + hashlib.sha256(content).hexdigest()


def env_positive_int(name, default):
    raw_value = os.getenv(name, str(default)).strip()

    try:
        value = int(raw_value)
    except ValueError:
        return default

    return value if value > 0 else default


def env_positive_float(name, default):
    raw_value = os.getenv(name, str(default)).strip()

    try:
        value = float(raw_value)
    except ValueError:
        return default

    return value if value > 0 else default


def get_budget_limits():
    return {
        "max_requests_per_session": env_positive_int(
            "ORCH_CHAT_MAX_REQUESTS_PER_SESSION",
            10,
        ),
        "max_total_tokens_per_session": env_positive_int(
            "ORCH_CHAT_MAX_TOTAL_TOKENS_PER_SESSION",
            4000,
        ),
        "max_cost_usd_per_session": env_positive_float(
            "ORCH_CHAT_MAX_COST_USD_PER_SESSION",
            0.01,
        ),
        "max_requests_per_day": env_positive_int(
            "ORCH_CHAT_MAX_REQUESTS_PER_DAY",
            30,
        ),
        "max_total_tokens_per_day": env_positive_int(
            "ORCH_CHAT_MAX_TOTAL_TOKENS_PER_DAY",
            12000,
        ),
        "max_cost_usd_per_day": env_positive_float(
            "ORCH_CHAT_MAX_COST_USD_PER_DAY",
            0.03,
        ),
        "token_reservation": env_positive_int(
            "ORCH_CHAT_TOKEN_RESERVATION",
            1200,
        ),
        "cost_reservation_usd": env_positive_float(
            "ORCH_CHAT_COST_RESERVATION_USD",
            0.001,
        ),
    }


def redact_sensitive_text(text):
    if not isinstance(text, str):
        raise ValueError("Text to redact must be a string.")

    categories = []

    def redact_openrouter_key(match):
        categories.append("openrouter_key")
        return "[REDACTED_OPENROUTER_KEY]"

    def redact_bearer_token(match):
        categories.append("bearer_token")
        return "Bearer [REDACTED_BEARER_TOKEN]"

    def redact_jwt(match):
        categories.append("jwt")
        return "[REDACTED_JWT]"

    redacted = OPENROUTER_KEY_PATTERN.sub(
        redact_openrouter_key,
        text,
    )

    redacted = BEARER_TOKEN_PATTERN.sub(
        redact_bearer_token,
        redacted,
    )

    redacted = JWT_PATTERN.sub(
        redact_jwt,
        redacted,
    )

    return {
        "redacted_text": redacted,
        "original_sha256": sha256_value(text),
        "detection_categories": sorted(set(categories)),
    }


def empty_totals():
    return {
        "request_count": 0,
        "total_tokens": 0,
        "total_cost_usd": 0.0,
    }


def default_budget_state():
    return {
        "budget_version": BUDGET_VERSION,
        "budget_date_utc": utc_date(),
        "daily_totals": empty_totals(),
        "sessions": {},
        "reservations": {},
    }


def normalize_budget_state(state):
    if not isinstance(state, dict):
        return default_budget_state()

    if state.get("budget_version") != BUDGET_VERSION:
        return default_budget_state()

    if state.get("budget_date_utc") != utc_date():
        return default_budget_state()

    state.setdefault("daily_totals", empty_totals())
    state.setdefault("sessions", {})
    state.setdefault("reservations", {})

    return state


def atomic_write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = path.with_name(
        f".{path.name}.{uuid.uuid4().hex}.tmp"
    )

    temporary_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    os.replace(temporary_path, path)


def load_budget_state():
    if not BUDGET_FILE.exists():
        return default_budget_state()

    try:
        state = json.loads(
            BUDGET_FILE.read_text(encoding="utf-8")
        )
    except json.JSONDecodeError:
        return default_budget_state()

    return normalize_budget_state(state)


@contextmanager
def budget_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

    with LOCK_FILE.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def ensure_session_totals(state, session_id_sha256):
    return state["sessions"].setdefault(
        session_id_sha256,
        empty_totals(),
    )


def totals_with_reservations(state, session_id_sha256):
    daily = dict(state["daily_totals"])
    session = dict(
        ensure_session_totals(
            state,
            session_id_sha256,
        )
    )

    for reservation in state["reservations"].values():
        daily["request_count"] += 1
        daily["total_tokens"] += reservation[
            "reserved_tokens"
        ]
        daily["total_cost_usd"] += reservation[
            "reserved_cost_usd"
        ]

        if (
            reservation["session_id_sha256"]
            == session_id_sha256
        ):
            session["request_count"] += 1
            session["total_tokens"] += reservation[
                "reserved_tokens"
            ]
            session["total_cost_usd"] += reservation[
                "reserved_cost_usd"
            ]

    return daily, session


def budget_exceeded(reason):
    raise ChatBudgetError(
        "Chat budget exhausted: "
        + reason
    )


def reserve_chat_budget(
    session_id_sha256,
    reserved_tokens=None,
):
    limits = get_budget_limits()

    if reserved_tokens is None:
        reserved_tokens = limits["token_reservation"]

    if (
        isinstance(reserved_tokens, bool)
        or not isinstance(reserved_tokens, int)
        or reserved_tokens <= 0
    ):
        raise ChatBudgetError(
            "Chat token reservation must be a positive integer."
        )

    with budget_lock():
        state = load_budget_state()

        daily, session = totals_with_reservations(
            state,
            session_id_sha256,
        )

        if (
            session["request_count"]
            >= limits["max_requests_per_session"]
        ):
            budget_exceeded(
                "per-session request limit reached."
            )

        if (
            daily["request_count"]
            >= limits["max_requests_per_day"]
        ):
            budget_exceeded(
                "daily request limit reached."
            )

        if (
            session["total_tokens"]
            + reserved_tokens
            > limits["max_total_tokens_per_session"]
        ):
            budget_exceeded(
                "per-session token limit reached."
            )

        if (
            daily["total_tokens"]
            + reserved_tokens
            > limits["max_total_tokens_per_day"]
        ):
            budget_exceeded(
                "daily token limit reached."
            )

        if (
            session["total_cost_usd"]
            + limits["cost_reservation_usd"]
            > limits["max_cost_usd_per_session"]
        ):
            budget_exceeded(
                "per-session cost limit reached."
            )

        if (
            daily["total_cost_usd"]
            + limits["cost_reservation_usd"]
            > limits["max_cost_usd_per_day"]
        ):
            budget_exceeded(
                "daily cost limit reached."
            )

        reservation_id = f"reservation_{uuid.uuid4().hex}"

        state["reservations"][reservation_id] = {
            "created_at_utc": utc_now(),
            "session_id_sha256": session_id_sha256,
            "reserved_tokens": reserved_tokens,
            "reserved_cost_usd": (
                limits["cost_reservation_usd"]
            ),
        }

        atomic_write_json(BUDGET_FILE, state)

    return reservation_id


def usage_number(usage, field, default_value=0):
    value = usage.get(field, default_value)

    if isinstance(value, bool):
        return default_value

    if not isinstance(value, (int, float)):
        return default_value

    return value


def append_usage_record(record):
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)

    with USAGE_FILE.open("a", encoding="utf-8") as file:
        file.write(
            json.dumps(record, ensure_ascii=False) + "\n"
        )
        file.flush()
        os.fsync(file.fileno())


def settle_chat_budget(
    reservation_id,
    provider_result,
    session_id_sha256,
):
    usage = provider_result.get("usage", {})

    total_tokens = int(
        usage_number(usage, "total_tokens")
    )

    cost = float(
        usage_number(usage, "cost")
    )

    with budget_lock():
        state = load_budget_state()

        reservation = state["reservations"].pop(
            reservation_id,
            None,
        )

        if reservation is None:
            raise ChatBudgetError(
                "Chat budget reservation was not found."
            )

        if (
            reservation["session_id_sha256"]
            != session_id_sha256
        ):
            raise ChatBudgetError(
                "Chat budget reservation session mismatch."
            )

        daily = state["daily_totals"]
        session = ensure_session_totals(
            state,
            session_id_sha256,
        )

        for totals in [daily, session]:
            totals["request_count"] += 1
            totals["total_tokens"] += total_tokens
            totals["total_cost_usd"] = round(
                totals["total_cost_usd"] + cost,
                8,
            )

        atomic_write_json(BUDGET_FILE, state)

        append_usage_record(
            {
                "timestamp_utc": utc_now(),
                "record_type": "orch_chat_usage",
                "session_id_sha256": session_id_sha256,
                "provider": provider_result.get("provider"),
                "model": provider_result.get(
                    "response_model"
                ),
                "response_id": provider_result.get(
                    "response_id"
                ),
                "usage": {
                    "prompt_tokens": usage_number(
                        usage,
                        "prompt_tokens",
                    ),
                    "completion_tokens": usage_number(
                        usage,
                        "completion_tokens",
                    ),
                    "total_tokens": total_tokens,
                    "cost": cost,
                },
                "execution_authority": "none",
            }
        )

    return {
        "daily_totals": daily,
        "session_totals": session,
        "limits": get_budget_limits(),
    }


def release_chat_budget(reservation_id):
    with budget_lock():
        state = load_budget_state()

        if reservation_id in state["reservations"]:
            state["reservations"].pop(reservation_id)
            atomic_write_json(BUDGET_FILE, state)


def publish_chat_audit_artifact(
    session_id_sha256,
    turn_number,
    mode,
    question,
    answer,
    provider_result,
    context,
):
    chat = provider_result["chat"]

    payload = {
        "artifact_version": AUDIT_SCHEMA_VERSION,
        "artifact_type": "orch_chat_audit",
        "timestamp_utc": utc_now(),
        "session_id_sha256": session_id_sha256,
        "turn_number": turn_number,
        "mode": mode,
        "question": redact_sensitive_text(question),
        "answer": redact_sensitive_text(
            chat["answer"]
        ),
        "provider": provider_result["provider"],
        "model": provider_result["response_model"],
        "response_id": provider_result.get(
            "response_id"
        ),
        "usage": provider_result.get("usage", {}),
        "referenced_task_ids": chat[
            "referenced_task_ids"
        ],
        "referenced_artifact_ids": chat[
            "referenced_artifact_ids"
        ],
        "context_fingerprint": sha256_value(context),
        "execution_authority": "none",
    }

    staged_path = stage_json("chat_audit", payload)

    return publish_staged_artifact(
        staging_path=staged_path,
        logical_name="chat_audit",
        producer_task_id="orch_chat_panel",
        schema_version=AUDIT_SCHEMA_VERSION,
    )
