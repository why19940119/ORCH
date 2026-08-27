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

USAGE_FILE = STATE_DIR / "chat_usage.jsonl"

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


def utc_now():
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )


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


def record_chat_usage(
    *,
    provider_result,
    session_id_sha256,
):
    usage = provider_result.get("usage", {})

    record = {
        "timestamp_utc": utc_now(),
        "record_type": "orch_chat_usage",
        "session_id_sha256": session_id_sha256,
        "provider": provider_result.get("provider"),
        "model": provider_result.get("response_model"),
        "response_id": provider_result.get("response_id"),
        "usage": {
            "prompt_tokens": usage_number(
                usage,
                "prompt_tokens",
            ),
            "completion_tokens": usage_number(
                usage,
                "completion_tokens",
            ),
            "total_tokens": usage_number(
                usage,
                "total_tokens",
            ),
            "cost": usage_number(usage, "cost"),
        },
        "execution_authority": "none",
    }

    append_usage_record(record)
    return record


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
