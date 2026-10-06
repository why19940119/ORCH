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
    ORCH_PROXY_FIX=N          behind N reverse proxies: use X-Forwarded-For /
                              -Proto / -Host, but ONLY on requests whose TCP
                              peer is in ORCH_TRUSTED_PROXY; from any other
                              peer those headers are removed (all servers)
    ORCH_TRUSTED_PROXY        proxy addresses / networks, comma separated
                              (e.g. 127.0.0.1 or 172.16.0.0/12); default
                              127.0.0.1,::1; "*" trusts every peer (only if
                              nothing but the proxy can reach the port)
    ORCH_TRUSTED_HOSTS        extra host names, comma separated
    ORCH_SETUP_TOKEN          the /setup (first admin) wizard asks for it.
                              v0.21.1: /setup always needs a token - this one,
                              or (if unset) a one-time token generated per
                              process and printed to the log / console
                              (docker logs) at startup
    ORCH_SETUP_LOCAL_NO_TOKEN=1  local development only: /setup without a
                              token (startup warning; ignored when
                              ORCH_TRUSTED_HOSTS / ORCH_PROXY_FIX is set)
    ORCH_HOST / ORCH_PORT     bind address for serve.py (default 127.0.0.1:5050)
"""

import ipaddress
import json
import logging
import os
import re
import secrets
import sys
import threading
from pathlib import Path

LOG = logging.getLogger("orch.deploy")

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


DEFAULT_TRUSTED_PROXIES = ("127.0.0.1", "::1")


def seed_task_queue(project_root=None):
    """Docker: /app/task_queue.json is a symlink into the state volume; on
    first start (target missing) copy the image's defaults/task_queue.json
    there. Never overwrites; a plain checkout (real file) is untouched.
    Returns the seeded path or None."""
    root = Path(project_root or PROJECT_ROOT)
    link = root / "task_queue.json"
    default = root / "defaults" / "task_queue.json"
    if not link.is_symlink() or link.exists() or not default.is_file():
        return None
    target = root / os.readlink(link) if not os.path.isabs(os.readlink(link)) \
        else Path(os.readlink(link))
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(target, "xb") as out:     # exclusive: never clobber
            out.write(default.read_bytes())
    except FileExistsError:
        return None
    return target


def trusted_proxies():
    items = [p.strip() for p in (os.getenv("ORCH_TRUSTED_PROXY") or "").split(",") if p.strip()]
    return items or list(DEFAULT_TRUSTED_PROXIES)


def peer_is_trusted_proxy(peer, trusted=None):
    trusted = trusted_proxies() if trusted is None else trusted
    if "*" in trusted:
        return True
    try:
        addr = ipaddress.ip_address((peer or "").split("%", 1)[0])
    except ValueError:
        return False
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    for item in trusted:
        try:
            if addr in ipaddress.ip_network(item, strict=False):
                return True
        except ValueError:
            continue
    return False


class ProxyHeadersMiddleware:
    """v0.21.0 review fix: X-Forwarded-* / Forwarded are honoured only when
    ORCH_PROXY_FIX is set AND the TCP peer is a trusted proxy; otherwise
    they are removed before Flask sees them, so a client that reaches the
    port directly cannot spoof its IP, scheme or Host (the TRUSTED_HOSTS
    check uses the real Host header). Same behaviour under waitress and the
    dev server (serve.py tells waitress to pass the headers through)."""

    def __init__(self, app, hops=None, trusted=None):
        from werkzeug.middleware.proxy_fix import ProxyFix
        self.app = app
        self.hops = proxy_hops() if hops is None else hops
        self.trusted = trusted_proxies() if trusted is None else list(trusted)
        self.proxied = (ProxyFix(app, x_for=self.hops, x_proto=self.hops,
                                 x_host=self.hops, x_prefix=0) if self.hops else None)
        self._warned = False

    def __call__(self, environ, start_response):
        peer = environ.get("REMOTE_ADDR", "")
        if self.proxied is not None and peer_is_trusted_proxy(peer, self.trusted):
            return self.proxied(environ, start_response)
        stripped = [key for key in environ
                    if key.startswith("HTTP_X_FORWARDED_") or key == "HTTP_FORWARDED"]
        if stripped and self.hops and not self._warned:
            self._warned = True
            LOG.warning("ignored X-Forwarded-* headers from untrusted peer %s "
                        "(ORCH_TRUSTED_PROXY=%s)", peer, ",".join(self.trusted))
        for key in stripped:
            del environ[key]
        return self.app(environ, start_response)


def extra_trusted_hosts():
    return [h.strip() for h in (os.getenv("ORCH_TRUSTED_HOSTS") or "").split(",") if h.strip()]


def setup_token():
    return (os.getenv("ORCH_SETUP_TOKEN") or "").strip()


def exposed_install():
    """Configured to be reached from other machines (a public host name or a
    reverse proxy in front). v0.21.1: informational only - a tunnel that
    rewrites Host to 127.0.0.1 looks local, so /setup needs a token anyway."""
    return bool(extra_trusted_hosts()) or proxy_hops() > 0


_TRUTHY = {"1", "true", "yes", "on"}


def configure_app_logging():
    """v0.21.1: send the app's own loggers (orch.*: chat provider failures,
    DB refusals, proxy warnings) to stderr = console / docker logs, with
    time and level. No-op when a handler is already configured."""
    log = logging.getLogger("orch")
    if log.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def setup_local_no_token():
    """ORCH_SETUP_LOCAL_NO_TOKEN=1: explicit local-dev opt-out of the token."""
    return (os.getenv("ORCH_SETUP_LOCAL_NO_TOKEN") or "").strip().lower() in _TRUTHY


_generated_setup_token = None
_generated_lock = threading.Lock()


def effective_setup_token():
    """(token, source) for the /setup wizard.

    v0.21.1: a token is required BY DEFAULT. ORCH_SETUP_TOKEN wins ('env');
    otherwise ORCH_SETUP_LOCAL_NO_TOKEN=1 opens the wizard without one
    ('opt-out', local dev only, ignored on an install configured as exposed);
    otherwise a one-time token is generated per process and printed to the
    log / console once ('generated')."""
    global _generated_setup_token
    token = setup_token()
    if token:
        return token, "env"
    if setup_local_no_token() and not exposed_install():
        return "", "opt-out"
    with _generated_lock:
        if _generated_setup_token is None:
            _generated_setup_token = secrets.token_urlsafe(18)
            print("ORCH: no ORCH_SETUP_TOKEN set - the first-admin page /setup requires this "
                  f"one-time token: {_generated_setup_token}  (valid until the first "
                  "admin exists or the server restarts; set ORCH_SETUP_TOKEN to choose your "
                  "own)", file=sys.stderr, flush=True)
        return _generated_setup_token, "generated"
