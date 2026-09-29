"""v0.21.0 (WP-ORCH-12): deployment settings for the Docker / server build.

Everything here is optional; a plain ``python orch_ui.py`` on a laptop
behaves exactly as before.

Environment (all optional):

    ORCH_UI_SECRET_KEY        session secret (wins over everything)
    ORCH_PERSIST_SECRET_KEY=1 no secret in env: generate one once and keep it
                              in state/secret_key (0600) so sessions survive
                              restarts (set by the Docker image)
    ORCH_CLIENT_NAME          client / shop name shown in the header
    ORCH_LOGO                 logo: an https:// URL, or a file path relative
                              to state/ (e.g. branding/logo.png)
    ORCH_TARGET_MARKET        e.g. "Hong Kong" (shown under the header)
    ORCH_BRANDING_FILE        JSON file with the same keys (client_name, logo,
                              target_market); default state/branding.json.
                              Environment values win over the file.
    ORCH_PROXY_FIX=N          behind N reverse proxies: trust X-Forwarded-*
    ORCH_TRUSTED_HOSTS        extra host names, comma separated
    ORCH_SETUP_TOKEN          if set, the /setup wizard asks for it
    ORCH_HOST / ORCH_PORT     bind address for serve.py (default 127.0.0.1:5050)
"""

import json
import os
import re
import secrets
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
STATE_DIR = PROJECT_ROOT / "state"
SECRET_FILE_NAME = "secret_key"
LOGO_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
MAX_TEXT = 80


def _truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Session secret
# ---------------------------------------------------------------------------

def persisted_secret_key(state_dir=None):
    """Read state/secret_key, creating it (0600, O_EXCL) on first use."""
    path = Path(state_dir or STATE_DIR) / SECRET_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        key = path.read_text(encoding="utf-8").strip()
        if len(key) < 32:
            raise RuntimeError(f"{path} is too short; delete it to regenerate")
        return key
    key = secrets.token_urlsafe(48)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(key + "\n")
    return key


def resolve_secret_key(state_dir=None):
    """(key, source): env, persisted file, or (None, 'ephemeral')."""
    env_key = (os.getenv("ORCH_UI_SECRET_KEY") or "").strip()
    if env_key:
        return env_key, "env"
    if _truthy(os.getenv("ORCH_PERSIST_SECRET_KEY")):
        return persisted_secret_key(state_dir), "file"
    return None, "ephemeral"


# ---------------------------------------------------------------------------
# Branding
# ---------------------------------------------------------------------------

def _clean_text(value):
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or "")).strip()
    return text[:MAX_TEXT]


def load_branding(state_dir=None):
    state_dir = Path(state_dir or STATE_DIR)
    data = {}
    file_path = Path(os.getenv("ORCH_BRANDING_FILE") or (state_dir / "branding.json"))
    if file_path.is_file():
        try:
            loaded = json.loads(file_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    pick = lambda env, key: os.getenv(env) or data.get(key) or ""
    logo = str(pick("ORCH_LOGO", "logo")).strip()
    return {
        "client_name": _clean_text(pick("ORCH_CLIENT_NAME", "client_name")),
        "target_market": _clean_text(pick("ORCH_TARGET_MARKET", "target_market")),
        "logo": logo,
        "logo_src": logo_src(logo, state_dir),
    }


def logo_file(logo, state_dir=None):
    """A local logo path resolved inside state/ (never outside), or None."""
    if not logo or re.match(r"^[a-z]+://", logo, re.I):
        return None
    base = Path(state_dir or STATE_DIR).resolve()
    try:
        path = (base / logo).resolve()
        path.relative_to(base)
    except (ValueError, OSError):
        return None
    if path.suffix.lower() not in LOGO_SUFFIXES or not path.is_file():
        return None
    return path


def logo_src(logo, state_dir=None):
    if re.match(r"^https://[^\s\"'<>]+$", logo or ""):
        return logo
    return "/branding/logo" if logo_file(logo, state_dir) else ""


# ---------------------------------------------------------------------------
# Proxy / hosts
# ---------------------------------------------------------------------------

def proxy_hops():
    value = (os.getenv("ORCH_PROXY_FIX") or "").strip().lower()
    if value in {"", "0", "false", "no", "off"}:
        return 0
    if value in {"true", "yes", "on"}:
        return 1
    try:
        return max(0, min(int(value), 5))
    except ValueError:
        return 0


def extra_trusted_hosts():
    return [h.strip() for h in (os.getenv("ORCH_TRUSTED_HOSTS") or "").split(",") if h.strip()]


def setup_token():
    return (os.getenv("ORCH_SETUP_TOKEN") or "").strip()
