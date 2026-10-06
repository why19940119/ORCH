"""ORCH local accounts, roles and governance (v0.20.0, WP-ORCH-11).

Everything lives in the SQLite state DB ``state/orch.db`` (or ``ORCH_AUTH_DIR/orch.db``; v0.21.0):

    state/auth.json          users (password hashes only), module approver
                             assignments, pending account changes, settings
    state/auth_audit.jsonl   append-only governance audit (logins, lockouts,
                             account changes, approvals, purges, exports)

Roles:
    admin     manages accounts (changes need a second admin), module
              approvers, retention/purge, permissions list
    editor    creates and revises AI drafts, imports CSV data
    approver  approves / rejects drafts for assigned modules; never their own

CLI (passwords are read with getpass, never from arguments or logs):
    python orch_auth.py create-admin [--username NAME]   first-run bootstrap
    python orch_auth.py status | list-users
    python orch_auth.py unlock NAME
    python orch_auth.py reset-password NAME              break-glass, audited
    python orch_auth.py purge                            apply retention now
    python orch_auth.py allow-single-admin               re-open the single-admin
                                                         exception (audited)
CLI events record the OS user (getpass.getuser()); shell access to this
machine is trusted.
"""

from __future__ import annotations

import csv
import getpass
import hashlib
import io
import json
import os
import re
import secrets
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

import mini_orch
import orch_db

PROJECT_ROOT = Path(__file__).resolve().parent
AUTH_DIR = Path(os.getenv("ORCH_AUTH_DIR") or (PROJECT_ROOT / "state"))

AUTH_VERSION = "v0.21.0"   # app version recorded in audit rows
ROLES = ("admin", "editor", "approver")
USERNAME_PATTERN = re.compile(r"^[\w .@'\-]{2,40}$", re.UNICODE)
MIN_PASSWORD_CHARS = 10
MAX_PASSWORD_CHARS = 128
CHANGE_KINDS = ("create_user", "change_role", "disable", "enable", "reset_password")

DEFAULT_SETTINGS = {
    "draft_retention_days": 180,
    "upload_retention_days": 1,
    "default_deadline_hours": 48,
}
DEADLINE_CHOICES = (4, 24, 48, 72, 168)

MODULES = (
    "sales_hub",
    "content_studio",
    "knowledge_base",
    "lead_desk",
    "campaign_engine",
    "market_dashboard",
)


class AuthError(ValueError):
    """User-facing governance error; ``code`` maps to ``auth_err_<code>``."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------
# Config (env, read at call time so tests / deployments can change it)
# ---------------------------------------------------------------------------

def _env_int(name, default, minimum=1):
    try:
        value = int(os.getenv(name, "").strip() or default)
    except ValueError:
        value = default
    return max(minimum, value)


def idle_timeout_seconds():
    return _env_int("ORCH_SESSION_IDLE_MINUTES", 30) * 60


def max_failed_logins():
    return _env_int("ORCH_LOGIN_MAX_FAILURES", 5)


def lockout_seconds():
    return _env_int("ORCH_LOGIN_LOCKOUT_MINUTES", 15) * 60


def max_session_seconds():
    """Absolute session lifetime (review fix): re-login after N hours."""
    return _env_int("ORCH_SESSION_MAX_HOURS", 12) * 3600


def os_user():
    """OS account running a CLI command (recorded on CLI audit events)."""
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def display_time(value):
    """ISO UTC -> local display time with an explicit offset (default HKT)."""
    parsed = _parse(value)
    if not parsed:
        return ""
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(os.getenv("ORCH_DISPLAY_TZ", "").strip() or "Asia/Hong_Kong")
    except Exception:
        zone = timezone(timedelta(hours=8))
    local = parsed.astimezone(zone)
    offset = local.strftime("%z")
    return local.strftime("%Y-%m-%d %H:%M:%S ") + offset[:3] + ":" + offset[3:]


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def auth_file():
    return Path(AUTH_DIR) / "auth.json"


def audit_file():
    return Path(AUTH_DIR) / "auth_audit.jsonl"


def lock_file():
    return Path(AUTH_DIR) / ".auth.lock"


def _empty_store():
    return {
        "schema_version": "1.0",
        "users": {},
        "module_approvers": {},
        "pending_changes": [],
        "settings": dict(DEFAULT_SETTINGS),
        # Review fix: the single-admin exception is first-run only.
        "bootstrap": {"complete": False},
        # Failed sign-ins for unknown usernames (keyed by a hash only).
        "unknown_logins": {},
    }


def _migrate_bootstrap(data, store):
    """Stores from before the bootstrap flag: bootstrap is complete when a
    second admin has ever existed or any change was approved by a second
    admin."""
    if isinstance(data, dict) and isinstance(data.get("bootstrap"), dict):
        return
    admins = [u for u in store["users"].values() if u.get("role") == "admin"]
    second_admin_decided = any(
        change.get("status") == "applied" and not change.get("bootstrap_exception")
        for change in store.get("pending_changes") or []
    )
    if len(admins) >= 2 or second_admin_decided:
        store["bootstrap"] = {"complete": True, "completed_at_utc": _iso(_now()),
                              "reason": "migrated"}


def load_store():
    # v0.21.0: docs['auth'] in the SQLite state DB (AUTH_DIR/orch.db).
    data = orch_db.load(auth_file(), None)
    if not isinstance(data, dict):
        return _empty_store()
    store = _empty_store()
    if isinstance(data, dict):
        store.update(data)
    store["settings"] = {**DEFAULT_SETTINGS, **(store.get("settings") or {})}
    if not isinstance(store.get("unknown_logins"), dict):
        store["unknown_logins"] = {}
    _migrate_bootstrap(data, store)
    return store


def _save_store(store):
    orch_db.save(auth_file(), store)
    _restrict_db_file()


def _restrict_db_file():
    """The DB holds password hashes: keep it owner-only (0600)."""
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(orch_db.db_path_for(auth_file())) + suffix)
        try:
            if path.exists() and (path.stat().st_mode & 0o077):
                os.chmod(path, 0o600)
        except OSError:
            pass


class _Locked:
    def __enter__(self):
        Path(AUTH_DIR).mkdir(parents=True, exist_ok=True)
        self._cm = mini_orch.state_lock(lock_file())
        self._cm.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)


def audit(event, actor, **details):
    """Append one governance audit record (never passwords or hashes)."""
    record = {
        "ts_utc": _iso(_now()),
        "event": event,
        "actor": actor,
        "audit_version": AUTH_VERSION,
        **details,
    }
    # v0.21.0: append-only auth_audit table; inside _Locked() it commits in
    # the same transaction as the change it records.
    orch_db.append(audit_file(), record)
    return record


def read_audit(limit=200):
    return orch_db.read_log(audit_file(), limit=limit, newest_first=True)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def _key(username):
    return str(username or "").strip().casefold()


def has_users():
    return bool(load_store()["users"])


def get_user(username, store=None):
    store = store or load_store()
    return store["users"].get(_key(username))


def public_user(user):
    if not user:
        return None
    return {
        key: user.get(key)
        for key in ("username", "role", "disabled", "created_at_utc",
                    "created_by", "last_login_utc", "locked_until_utc",
                    "failed_logins", "session_epoch")
    }


def list_users(store=None):
    store = store or load_store()
    return sorted(
        (public_user(user) for user in store["users"].values()),
        key=lambda user: (ROLES.index(user["role"]), user["username"].casefold()),
    )


def active_admins(store):
    return [
        user for user in store["users"].values()
        if user["role"] == "admin" and not user.get("disabled")
    ]


def validate_username(username):
    username = str(username or "").strip()
    if not USERNAME_PATTERN.fullmatch(username):
        raise AuthError("invalid_username")
    return username


def validate_password(password):
    if not isinstance(password, str) or not (
        MIN_PASSWORD_CHARS <= len(password) <= MAX_PASSWORD_CHARS
    ):
        raise AuthError("weak_password")
    if password.strip() != password or len(set(password)) < 4:
        raise AuthError("weak_password")
    return password


def hash_password(password):
    return generate_password_hash(validate_password(password))


def _new_user(username, role, password_hash, created_by):
    return {
        "username": username,
        "role": role,
        "password_hash": password_hash,
        "disabled": False,
        "created_at_utc": _iso(_now()),
        "created_by": created_by,
        "failed_logins": 0,
        "locked_until_utc": None,
        "last_login_utc": None,
        "session_epoch": 1,
    }


def bootstrap_admin(username, password, actor="cli", os_account=None):
    """First-run admin. Refused as soon as any account exists."""
    username = validate_username(username)
    password_hash = hash_password(password)
    with _Locked():
        store = load_store()
        if store["users"]:
            raise AuthError("already_bootstrapped")
        store["users"][_key(username)] = _new_user(username, "admin", password_hash, actor)
        _save_store(store)
    audit("bootstrap_admin", actor, target=username, role="admin",
          **({"os_user": os_account} if os_account else {}))
    return username


# Timing-equaliser for unknown usernames.
_DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(16))


UNKNOWN_LOGIN_CAP = 1000


def typed_username_ref(username):
    """What the audit keeps of a typed, unknown username: a short hash."""
    typed = str(username or "").strip()
    return hashlib.sha256(_key(typed).encode("utf-8")).hexdigest()[:16]


def _unknown_login_failure(store, username, now):
    ref = typed_username_ref(username)
    bucket = store["unknown_logins"]
    entry = dict(bucket.get(ref) or {"failed": 0, "locked_until_utc": None})
    locked_until = _parse(entry.get("locked_until_utc"))
    if locked_until and locked_until > now:
        reason = "locked"
    else:
        if locked_until:
            entry = {"failed": 0, "locked_until_utc": None}
        entry["failed"] = int(entry.get("failed") or 0) + 1
        reason = "invalid"
        if entry["failed"] >= max_failed_logins():
            entry["locked_until_utc"] = _iso(now + timedelta(seconds=lockout_seconds()))
            reason = "locked"
    entry["last_utc"] = _iso(now)
    bucket[ref] = entry
    if len(bucket) > UNKNOWN_LOGIN_CAP:
        keep = sorted(bucket.items(), key=lambda kv: kv[1].get("last_utc") or "")[-UNKNOWN_LOGIN_CAP:]
        store["unknown_logins"] = dict(keep)
    return ref, entry, reason


def authenticate(username, password, now=None):
    """Returns ``(user, reason)``; reason is ok|invalid|locked|disabled.

    The web UI shows ONE generic failure message for every non-ok reason.
    A password hash is checked on every path (real, locked, disabled or
    unknown user) so timing does not reveal which usernames exist, and
    unknown usernames get the same failure counter and lockout.
    """
    now = now or _now()
    key = _key(username)
    password = str(password or "")
    with _Locked():
        store = load_store()
        user = store["users"].get(key)
        if user is None:
            check_password_hash(_DUMMY_HASH, password)
            ref, entry, reason = _unknown_login_failure(store, username, now)
            _save_store(store)
            audit("login_failed", "(unknown)", reason="unknown_user",
                  typed_username_sha256=ref, failed_logins=entry["failed"])
            return None, reason
        locked_until = _parse(user.get("locked_until_utc"))
        if locked_until and locked_until > now:
            check_password_hash(user["password_hash"], password)   # equal timing
            audit("login_refused_locked", user["username"])
            return None, "locked"
        if locked_until and locked_until <= now:
            user["locked_until_utc"] = None
            user["failed_logins"] = 0
        if not check_password_hash(user["password_hash"], password):
            user["failed_logins"] = int(user.get("failed_logins") or 0) + 1
            locked = user["failed_logins"] >= max_failed_logins()
            if locked:
                user["locked_until_utc"] = _iso(now + timedelta(seconds=lockout_seconds()))
            _save_store(store)
            audit("login_failed", user["username"], reason="bad_password",
                  failed_logins=user["failed_logins"])
            if locked:
                audit("account_locked", user["username"],
                      locked_until_utc=user["locked_until_utc"])
                return None, "locked"
            return None, "invalid"
        if user.get("disabled"):
            audit("login_refused_disabled", user["username"])
            return None, "disabled"
        user["failed_logins"] = 0
        user["locked_until_utc"] = None
        user["last_login_utc"] = _iso(now)
        _save_store(store)
    audit("login_success", user["username"], role=user["role"])
    return public_user(user), "ok"


# ---------------------------------------------------------------------------
# Session helpers (Flask session dict; no Flask import needed here)
# ---------------------------------------------------------------------------

SESSION_KEYS = ("auth_user", "auth_epoch", "auth_seen", "auth_login_at")


def start_session(session, user, now_ts=None):
    locale = session.get("locale")
    session.clear()
    if locale:
        session["locale"] = locale
    now_ts = now_ts or time.time()
    session["auth_user"] = user["username"]
    session["auth_epoch"] = user.get("session_epoch", 1)
    session["auth_seen"] = now_ts
    session["auth_login_at"] = now_ts


def end_session(session, reason=None):
    locale = session.get("locale")
    session.clear()
    if locale:
        session["locale"] = locale
    if reason:
        session["auth_notice"] = reason


def session_user(session, now_ts=None):
    """Validate the session (user active, epoch, idle timeout)."""
    username = session.get("auth_user")
    if not username:
        return None
    now_ts = now_ts or time.time()
    user = get_user(username)
    if (
        user is None
        or user.get("disabled")
        or user.get("session_epoch", 1) != session.get("auth_epoch")
    ):
        end_session(session, "revoked")
        return None
    seen = float(session.get("auth_seen") or 0)
    if now_ts - seen > idle_timeout_seconds():
        audit("session_expired", user["username"], kind="idle")
        end_session(session, "expired")
        return None
    login_at = float(session.get("auth_login_at") or 0)
    if now_ts - login_at > max_session_seconds():
        audit("session_expired", user["username"], kind="absolute")
        end_session(session, "expired")
        return None
    # Only refresh occasionally to keep the cookie stable.
    if now_ts - seen > 30:
        session["auth_seen"] = now_ts
    return public_user(user)


def revoke_sessions(username):
    """Review fix: logout bumps the user's session epoch, so a copied or
    replayed cookie stops working (this signs out every browser of that
    user)."""
    with _Locked():
        store = load_store()
        user = store["users"].get(_key(username))
        if not user:
            return
        user["session_epoch"] = int(user.get("session_epoch", 1)) + 1
        _save_store(store)


# ---------------------------------------------------------------------------
# Account changes with second-admin approval
# ---------------------------------------------------------------------------

def _require_admin(store, actor):
    user = store["users"].get(_key(actor))
    if not user or user.get("disabled") or user["role"] != "admin":
        raise AuthError("forbidden")
    return user


def _validate_change(store, actor_user, kind, target, params):
    if kind not in CHANGE_KINDS:
        raise AuthError("invalid_change")
    if kind == "create_user":
        target = validate_username(target)
        if _key(target) in store["users"]:
            raise AuthError("user_exists")
        if params.get("role") not in ROLES:
            raise AuthError("invalid_role")
        if not params.get("password_hash"):
            raise AuthError("weak_password")
        return target
    user = store["users"].get(_key(target))
    if not user:
        raise AuthError("unknown_user")
    target = user["username"]
    if kind in {"change_role", "disable"} and _key(target) == _key(actor_user["username"]):
        raise AuthError("not_on_self")
    # v0.20.1: a disabled account takes "enable" (and a password reset to
    # prepare re-enabling); a second disable or a role change no longer applies.
    if kind in {"disable", "change_role"} and user.get("disabled"):
        raise AuthError("target_disabled")
    if kind == "change_role":
        if params.get("role") not in ROLES or params["role"] == user["role"]:
            raise AuthError("invalid_role")
    removes_admin = user["role"] == "admin" and not user.get("disabled") and (
        kind == "disable" or (kind == "change_role" and params.get("role") != "admin")
    )
    if removes_admin and len(active_admins(store)) <= 1:
        raise AuthError("last_admin")
    if kind == "disable" and user.get("disabled"):
        raise AuthError("invalid_change")
    if kind == "enable" and not user.get("disabled"):
        raise AuthError("invalid_change")
    if kind == "reset_password" and not params.get("password_hash"):
        raise AuthError("weak_password")
    return target


# v0.20.1: codes meaning a pending request no longer applies to the current
# accounts; such requests are auto-closed (audited) rather than left open.
NO_LONGER_APPLIES = frozenset(
    {"unknown_user", "user_exists", "invalid_role", "invalid_change", "target_disabled",
     "requester_not_admin"}
)


def _requester_is_admin(store, item):
    requester = store["users"].get(_key(item.get("requested_by", "")))
    return bool(requester and not requester.get("disabled") and requester["role"] == "admin")


def _same_request(item, kind, target, params, store=None):
    # Review fix: a request whose requester is no longer an active admin
    # can never be approved, so it does not block a new one.
    if store is not None and not _requester_is_admin(store, item):
        return False
    return (
        item.get("status") == "pending"
        and item.get("kind") == kind
        and _key(item.get("target", "")) == _key(target)
        and (kind != "change_role"
             or (item.get("params") or {}).get("role") == params.get("role"))
    )


def _auto_close(change, reason, closed_by):
    change.update(status="auto_closed", auto_close_reason=reason,
                  decided_by=closed_by, decided_at_utc=_iso(_now()))
    change.pop("params", None) if change.get("kind") in {"create_user", "reset_password"} else None
    if "params" in change:
        change["params"] = _audit_params(change["params"])
    return {"change_id": change["id"], "kind": change["kind"], "target": change["target"],
            "requested_by": change["requested_by"], "reason": reason}


def _sweep_pending(store, closed_by, skip_id=None):
    """Re-validate every other pending request after a change was applied
    and auto-close the ones that no longer apply. Returns audit rows."""
    closed = []
    for item in store["pending_changes"]:
        if item.get("status") != "pending" or item.get("id") == skip_id:
            continue
        if not _requester_is_admin(store, item):
            # Review fix: the requester was disabled / demoted since.
            closed.append(_auto_close(item, "requester_not_admin", closed_by))
            continue
        requester = store["users"][_key(item["requested_by"])]
        try:
            _validate_change(store, requester, item["kind"], item["target"],
                             item.get("params") or {})
        except AuthError as error:
            if error.code in NO_LONGER_APPLIES:
                closed.append(_auto_close(item, error.code, closed_by))
    return closed


def _audit_auto_closed(actor, rows):
    for row in rows:
        audit("account_change_auto_closed", actor, **row)


def _apply_change(store, change, approved_by):
    kind, target, params = change["kind"], change["target"], change.get("params") or {}
    if kind == "create_user":
        store["users"][_key(target)] = _new_user(
            target, params["role"], params["password_hash"], change["requested_by"]
        )
        store["users"][_key(target)]["approved_by"] = approved_by
        return
    user = store["users"][_key(target)]
    if kind == "change_role":
        user["role"] = params["role"]
        if params["role"] != "approver":
            for module, names in store["module_approvers"].items():
                store["module_approvers"][module] = [
                    name for name in names if _key(name) != _key(target)
                ]
    elif kind == "disable":
        user["disabled"] = True
    elif kind == "enable":
        user["disabled"] = False
        user["failed_logins"] = 0
        user["locked_until_utc"] = None
    elif kind == "reset_password":
        user["password_hash"] = params["password_hash"]
        user["failed_logins"] = 0
        user["locked_until_utc"] = None
    # Any change to an account ends that account's sessions.
    user["session_epoch"] = int(user.get("session_epoch", 1)) + 1


def bootstrap_open(store=None):
    """True while the single-admin exception may apply (first run, or
    re-opened with ``orch_auth.py allow-single-admin``)."""
    store = store or load_store()
    return not (store.get("bootstrap") or {}).get("complete")


def _maybe_complete_bootstrap(store):
    """Rule: bootstrap is complete (for good) as soon as two active admins
    exist. Returns True when it was closed just now."""
    boot = store.setdefault("bootstrap", {"complete": False})
    if not boot.get("complete") and len(active_admins(store)) >= 2:
        boot.update(complete=True, completed_at_utc=_iso(_now()), reason="second_admin")
        return True
    return False


def allow_single_admin(actor="cli", os_account=None):
    """Explicit CLI step to re-open the single-admin exception (audited)."""
    with _Locked():
        store = load_store()
        admins = active_admins(store)
        if len(admins) != 1:
            raise AuthError("single_admin_not_applicable")
        store["bootstrap"] = {
            "complete": False,
            "reopened_at_utc": _iso(_now()),
            "reopened_by_os_user": os_account,
        }
        _save_store(store)
    audit("single_admin_reenabled", actor, os_user=os_account,
          admin=admins[0]["username"])


def _audit_params(params):
    return {key: value for key, value in (params or {}).items() if key != "password_hash"}


def request_change(actor, kind, target, role=None, password=None):
    """Queue an account change for a second admin.

    Single-admin bootstrap exception: while the requester is the only
    active admin there is nobody to approve, so the change applies at
    once and is audited with ``bootstrap_exception: true``.
    """
    params = {}
    if role is not None:
        params["role"] = role
    if kind in {"create_user", "reset_password"}:
        params["password_hash"] = hash_password(password)
    with _Locked():
        store = load_store()
        actor_user = _require_admin(store, actor)
        target = _validate_change(store, actor_user, kind, target, params)
        change = {
            "id": "chg_" + uuid.uuid4().hex[:10],
            "kind": kind,
            "target": target,
            "params": params,
            "requested_by": actor_user["username"],
            "requested_at_utc": _iso(_now()),
            "status": "pending",
        }
        lone_admin = len(active_admins(store)) == 1
        single_admin = lone_admin and bootstrap_open(store)
        # v0.20.1: refuse a second identical pending request (under the
        # single-admin exception the change applies now and the older
        # pending copy is auto-closed by the sweep instead).
        if not single_admin and any(
            _same_request(item, kind, target, params, store) for item in store["pending_changes"]
        ):
            raise AuthError("duplicate_request")
        closed = False
        auto_closed = []
        if single_admin:
            _apply_change(store, change, actor_user["username"])
            change.update(status="applied", decided_by=actor_user["username"],
                          decided_at_utc=_iso(_now()), bootstrap_exception=True)
            closed = _maybe_complete_bootstrap(store)
            auto_closed = _sweep_pending(store, "system")
        store["pending_changes"].append(change)
        store["pending_changes"] = store["pending_changes"][-500:]
        _save_store(store)
        # v0.21.0: audit rows in the same transaction as the store write.
        audit("account_change_requested", actor_user["username"], change_id=change["id"],
              kind=kind, target=target, params=_audit_params(params))
        if single_admin:
            audit("account_change_applied", actor_user["username"], change_id=change["id"],
                  kind=kind, target=target, params=_audit_params(params),
                  bootstrap_exception=True)
        _audit_auto_closed("system", auto_closed)
        if closed:
            audit("bootstrap_complete", actor_user["username"], reason="second_admin")
    result = {k: v for k, v in change.items() if k != "params"} | {
        "params": _audit_params(params)
    }
    # Only admin left but bootstrap is over: nobody can approve until a
    # second admin exists or `orch_auth.py allow-single-admin` is run.
    result["awaiting_second_admin"] = lone_admin and not single_admin
    return result


def decide_change(actor, change_id, decision):
    if decision not in {"approved", "rejected"}:
        raise AuthError("invalid_change")
    with _Locked():
        store = load_store()
        actor_user = _require_admin(store, actor)
        change = next(
            (item for item in store["pending_changes"] if item["id"] == change_id), None
        )
        if not change or change["status"] != "pending":
            raise AuthError("unknown_change")
        if _key(change["requested_by"]) == _key(actor_user["username"]):
            raise AuthError("second_admin_required")
        # Review fix: the account being changed cannot approve its own change.
        if change["kind"] != "create_user" and _key(change["target"]) == _key(actor_user["username"]):
            raise AuthError("not_on_self")
        if decision == "approved":
            requester = store["users"].get(_key(change["requested_by"]))
            try:
                if not requester or requester.get("disabled") or requester["role"] != "admin":
                    raise AuthError("requester_not_admin")
                _validate_change(store, requester, change["kind"], change["target"],
                                 change.get("params") or {})
            except AuthError as error:
                if error.code not in NO_LONGER_APPLIES:
                    raise
                # v0.20.1: re-validated on approval; it no longer applies.
                row = _auto_close(change, error.code, actor_user["username"])
                _save_store(store)
                _audit_auto_closed(actor_user["username"], [row])
                return change
            _apply_change(store, change, actor_user["username"])
        closed = decision == "approved" and _maybe_complete_bootstrap(store)
        auto_closed = (_sweep_pending(store, "system", skip_id=change_id)
                       if decision == "approved" else [])
        change.update(
            status="applied" if decision == "approved" else "rejected",
            decided_by=actor_user["username"],
            decided_at_utc=_iso(_now()),
        )
        change.pop("params", None) if decision == "rejected" else None
        if decision == "approved" and "params" in change:
            change["params"] = _audit_params(change["params"])
        _save_store(store)
        # v0.21.0: the decision and all its audit rows (incl. the v0.20.1
        # auto-closed requests) commit together in one SQLite transaction.
        audit("account_change_" + ("applied" if decision == "approved" else "rejected"),
              actor_user["username"], change_id=change_id, kind=change["kind"],
              target=change["target"], requested_by=change["requested_by"])
        _audit_auto_closed("system", auto_closed)
        if closed:
            audit("bootstrap_complete", actor_user["username"], reason="second_admin")
    return change


def pending_changes(store=None):
    store = store or load_store()
    return [
        {k: v for k, v in item.items() if k != "params"} | {
            "params": _audit_params(item.get("params"))
        }
        for item in reversed(store["pending_changes"])
    ]


def unlock_user(username, actor="cli", via="cli", os_account=None):
    """Clear a lockout. CLI (records the OS user) or an admin in the web UI."""
    with _Locked():
        store = load_store()
        if via == "web":
            actor = _require_admin(store, actor)["username"]
        user = store["users"].get(_key(username))
        if not user:
            raise AuthError("unknown_user")
        user["failed_logins"] = 0
        user["locked_until_utc"] = None
        _save_store(store)
    details = {"via": via}
    if via == "cli":
        details["os_user"] = os_account or os_user()
    audit("account_unlocked", actor, target=user["username"], **details)


def is_locked(user, now=None):
    until = _parse((user or {}).get("locked_until_utc"))
    return bool(until and until > (now or _now()))


def break_glass_reset(username, password, actor="cli", os_account=None):
    """Local CLI recovery (e.g. the only admin forgot the password)."""
    password_hash = hash_password(password)
    with _Locked():
        store = load_store()
        user = store["users"].get(_key(username))
        if not user:
            raise AuthError("unknown_user")
        user["password_hash"] = password_hash
        user["failed_logins"] = 0
        user["locked_until_utc"] = None
        user["session_epoch"] = int(user.get("session_epoch", 1)) + 1
        _save_store(store)
    audit("password_reset_break_glass", actor, target=user["username"],
          os_user=os_account or os_user())


# ---------------------------------------------------------------------------
# Roles and module approvers
# ---------------------------------------------------------------------------

def user_role(username):
    user = get_user(username)
    if not user or user.get("disabled"):
        return None
    return user["role"]


def module_approvers(store=None):
    store = store or load_store()
    return {module: list(store["module_approvers"].get(module) or []) for module in MODULES}


def can_approve(username, module, store=None):
    """Approver role AND (module unassigned -> any approver; else listed)."""
    store = store or load_store()
    user = store["users"].get(_key(username))
    if not user or user.get("disabled") or user["role"] != "approver":
        return False
    assigned = store["module_approvers"].get(module) or []
    return not assigned or any(_key(name) == _key(username) for name in assigned)


def approvers_for(module, store=None):
    store = store or load_store()
    return [
        user["username"] for user in store["users"].values()
        if can_approve(user["username"], module, store)
    ]


def set_module_approvers(actor, module, usernames):
    if module not in MODULES:
        raise AuthError("invalid_module")
    with _Locked():
        store = load_store()
        actor_user = _require_admin(store, actor)
        clean = []
        for name in usernames:
            user = store["users"].get(_key(name))
            if not user or user["role"] != "approver":
                raise AuthError("not_an_approver")
            if user["username"] not in clean:
                clean.append(user["username"])
        before = store["module_approvers"].get(module) or []
        store["module_approvers"][module] = clean
        _save_store(store)
    audit("module_approvers_set", actor_user["username"], module=module,
          before=before, after=clean)
    return clean


# ---------------------------------------------------------------------------
# Settings, retention and purge
# ---------------------------------------------------------------------------

def settings(store=None):
    return dict((store or load_store())["settings"])


def update_settings(actor, **values):
    limits = {
        "draft_retention_days": (1, 3650),
        "upload_retention_days": (1, 3650),
        "default_deadline_hours": (1, 720),
    }
    clean = {}
    for key, raw in values.items():
        if key not in limits:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise AuthError("invalid_setting")
        low, high = limits[key]
        if not low <= value <= high:
            raise AuthError("invalid_setting")
        clean[key] = value
    with _Locked():
        store = load_store()
        actor_user = _require_admin(store, actor)
        before = dict(store["settings"])
        store["settings"].update(clean)
        _save_store(store)
    audit("settings_updated", actor_user["username"], before=before,
          after=dict(store["settings"]))
    return store["settings"]


def purge(actor, now=None, os_account=None):
    """Delete decided drafts and chat uploads older than the retention.

    Audit records (ecom_audit artifacts, events.jsonl, auth_audit.jsonl)
    are never deleted. Pending drafts are never purged.
    """
    import chat_attachments
    import commerce_demo

    now = now or _now()
    store = load_store()
    if actor != "cli":
        _require_admin(store, actor)
    conf = store["settings"]
    draft_cutoff = now - timedelta(days=conf["draft_retention_days"])
    upload_cutoff = now - timedelta(days=conf["upload_retention_days"])
    drafts = commerce_demo.purge_decided_drafts(draft_cutoff)
    uploads = chat_attachments.sweep_old_uploads(
        max_age_seconds=int((now - upload_cutoff).total_seconds()),
        now=now.timestamp(),
    )
    uploads_count = uploads if isinstance(uploads, int) else len(uploads or [])
    record = audit(
        "retention_purge",
        actor,
        draft_cutoff_utc=_iso(draft_cutoff),
        upload_cutoff_utc=_iso(upload_cutoff),
        drafts_removed=drafts["tasks"],
        draft_artifacts_removed=drafts["artifacts"],
        upload_batches_removed=uploads_count,
        audit_kept=True,
        **({"os_user": os_account or os_user()} if actor == "cli" else {}),
    )
    return record


# ---------------------------------------------------------------------------
# 權限清單 (permissions list)
# ---------------------------------------------------------------------------

def permission_rows(store=None):
    store = store or load_store()
    rows = []
    for user in list_users(store):
        if user["role"] == "approver":
            modules = [m for m in MODULES if can_approve(user["username"], m, store)]
        else:
            modules = []
        rows.append({**user, "approve_modules": modules})
    return rows


CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    """Neutralise spreadsheet formulas: cells starting with = + - @ tab or
    CR get a leading apostrophe (OWASP CSV injection guidance)."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(CSV_FORMULA_PREFIXES) else text


class _SafeWriter:
    def __init__(self, stream):
        self._writer = csv.writer(stream)

    def writerow(self, row):
        self._writer.writerow([csv_safe(cell) for cell in row])


def permissions_csv(t, store=None):
    store = store or load_store()
    out = io.StringIO()
    writer = _SafeWriter(out)
    writer.writerow([
        t["perm_col_user"], t["perm_col_role"], t["perm_col_status"],
        t["perm_col_modules"], t["perm_col_created"], t["perm_col_last_login"],
    ])
    for row in permission_rows(store):
        writer.writerow([
            row["username"],
            t[f"role_{row['role']}"],
            t["perm_status_disabled"] if row["disabled"] else t["perm_status_active"],
            " / ".join(t.get(f"mod_{m}_title", m) for m in row["approve_modules"]) or "—",
            display_time(row.get("created_at_utc")),
            display_time(row.get("last_login_utc")),
        ])
    writer.writerow([])
    writer.writerow([t["perm_col_module"], t["perm_col_assigned"]])
    for module, names in module_approvers(store).items():
        writer.writerow([
            t.get(f"mod_{module}_title", module),
            ", ".join(names) if names else t["perm_any_approver"],
        ])
    # BOM so Excel opens the Chinese headers correctly.
    return "\ufeff" + out.getvalue()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read_new_password(prompt_fn=None):
    prompt_fn = prompt_fn or getpass.getpass
    first = prompt_fn("Password (min 10 chars): ")
    second = prompt_fn("Repeat password: ")
    if first != second:
        raise AuthError("password_mismatch")
    return validate_password(first)


CLI_MESSAGES = {
    "already_bootstrapped": "Accounts already exist. Add users in the web UI (Admin > Users).",
    "invalid_username": "Username must be 2-40 letters, digits, spaces or . @ ' -",
    "weak_password": f"Password must be {MIN_PASSWORD_CHARS}-{MAX_PASSWORD_CHARS} characters with some variety and no leading/trailing spaces.",
    "password_mismatch": "The passwords do not match.",
    "unknown_user": "No such user.",
    "single_admin_not_applicable": "allow-single-admin only applies when exactly one active admin exists.",
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else ""
    try:
        if command == "create-admin":
            username = None
            if "--username" in argv:
                index = argv.index("--username")
                username = argv[index + 1] if index + 1 < len(argv) else ""
            if has_users():
                raise AuthError("already_bootstrapped")
            if username is None:
                username = input("Admin username: ").strip()
            validate_username(username)
            password = _read_new_password()
            name = bootstrap_admin(username, password, os_account=os_user())
            print(f"Admin '{name}' created. Start the console and sign in at /login.")
            return 0
        if command in {"status", "list-users"}:
            store = load_store()
            if not store["users"]:
                print("No accounts yet. Run: python orch_auth.py create-admin")
                return 0
            for user in list_users(store):
                flags = []
                if user["disabled"]:
                    flags.append("disabled")
                if user.get("locked_until_utc"):
                    flags.append(f"locked until {user['locked_until_utc']}")
                print(f"{user['username']}\t{user['role']}\t{' '.join(flags)}")
            pending = [c for c in store["pending_changes"] if c["status"] == "pending"]
            print(f"Pending account changes: {len(pending)}")
            return 0
        if command == "unlock" and len(argv) == 2:
            unlock_user(argv[1], actor="cli", via="cli", os_account=os_user())
            print(f"Unlocked {argv[1]}.")
            return 0
        if command == "reset-password" and len(argv) == 2:
            if not get_user(argv[1]):
                raise AuthError("unknown_user")
            break_glass_reset(argv[1], _read_new_password(), os_account=os_user())
            print(f"Password reset for {argv[1]} (audited as break-glass).")
            return 0
        if command == "allow-single-admin":
            allow_single_admin("cli", os_account=os_user())
            print("Single-admin exception re-opened (audited). Account changes by the "
                  "only admin apply at once until a second admin exists.")
            return 0
        if command == "purge":
            record = purge("cli", os_account=os_user())
            print(
                f"Purged {record['drafts_removed']} decided draft(s), "
                f"{record['upload_batches_removed']} upload batch(es). Audit kept."
            )
            return 0
    except AuthError as error:
        print(CLI_MESSAGES.get(error.code, error.code), file=sys.stderr)
        return 1
    print(__doc__.split("CLI", 1)[1].strip() if "CLI" in __doc__ else "usage")
    return 2


if __name__ == "__main__":
    sys.exit(main())
