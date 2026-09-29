"""v0.20.0 test helpers: a temp account store and signed-in test clients.

Existing UI tests call ``signed_in(self, self.client)`` so they run as a
logged-in account (auth is always on once an account exists). Hashes use
a cheap PBKDF2 setting to keep the suite fast; production uses
werkzeug's default.
"""

import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import orch_auth

TEST_PASSWORD = "Correct-Horse-42"
FAST_HASH = "pbkdf2:sha256:1000"


def use_temp_auth(testcase, users=(("Test Admin", "admin"),)):
    tmp = Path(tempfile.mkdtemp(prefix="orch_auth_test_"))
    patcher = patch.object(orch_auth, "AUTH_DIR", tmp)
    patcher.start()
    testcase.addCleanup(patcher.stop)
    testcase.addCleanup(shutil.rmtree, tmp, True)
    seed_users(users)
    return tmp


def seed_users(users):
    store = orch_auth.load_store()
    for username, role in users:
        user = orch_auth._new_user(
            username, role, generate_password_hash(TEST_PASSWORD, method=FAST_HASH), "test"
        )
        store["users"][orch_auth._key(username)] = user
    orch_auth._save_store(store)


def sign_in(client, username):
    user = orch_auth.get_user(username)
    with client.session_transaction() as stored:
        for key in orch_auth.SESSION_KEYS:
            stored.pop(key, None)
        stored["auth_user"] = user["username"]
        stored["auth_epoch"] = user["session_epoch"]
        stored["auth_seen"] = time.time()
        stored["auth_login_at"] = time.time()


def signed_in(testcase, client, username="Test Admin", role="admin", users=None):
    use_temp_auth(testcase, users or ((username, role),))
    sign_in(client, username)
    return client


# Accounts used by the e-commerce demo tests (names match the old typed names).
DEMO_USERS = (
    ("Admin One", "admin"),
    ("Amy Chan", "editor"),
    ("Cara Wong", "editor"),
    ("Ben Lee", "approver"),
    ("Dan Ho", "approver"),
)


def demo_signed_in(testcase, client, username="Amy Chan"):
    return signed_in(testcase, client, username, users=DEMO_USERS)
