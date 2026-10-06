"""v0.21.0 (WP-ORCH-12): SQLite state store, migration and concurrency.

Every test works in a temp directory; the repository's state/ is never
opened (and no state/orch.db is created by a pure read).
"""

import io
import json
import multiprocessing
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import orch_db

REPO = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "state_v0200"
FIXTURE_PASSWORD = "Fixture-Pass-2026"


class TempState(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch_db_test_"))
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.status = self.state / "task_status.json"
        self.events = self.state / "events.jsonl"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class StoreTests(TempState):
    def test_wal_busy_timeout_and_schema_version(self):
        orch_db.save(self.status, {"t1": {"status": "pending"}})
        db = self.state / "orch.db"
        self.assertTrue(db.is_file())
        s = orch_db.summary(self.status)
        self.assertEqual(s["journal_mode"], "wal")
        self.assertEqual(s["schema_version"], orch_db.SCHEMA_VERSION)
        self.assertEqual(orch_db.schema_version(self.state), 1)
        conn = orch_db._open(db)
        try:
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0],
                             orch_db.BUSY_TIMEOUT_MS)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        finally:
            conn.close()
        self.assertEqual(orch_db.integrity_check(self.state), "ok")

    def test_pure_read_does_not_create_db(self):
        self.assertEqual(orch_db.load(self.status, {}), {})
        self.assertEqual(orch_db.read_log(self.events), [])
        self.assertFalse(orch_db.exists(self.state / "auth.json"))
        self.assertFalse((self.state / "orch.db").exists())

    def test_round_trips(self):
        statuses = {"a": {"status": "done", "approval_status": None, "n": "竹"},
                    "b": {"status": "waiting_approval", "approval_status": "waiting_approval"}}
        orch_db.save(self.status, statuses)
        self.assertEqual(orch_db.load(self.status), statuses)
        orch_db.save(self.state / "ecom_demo_queue.json", [{"id": "task_ecom_x"}])
        self.assertEqual(orch_db.load(self.state / "ecom_demo_queue.json"), [{"id": "task_ecom_x"}])
        self.assertTrue(orch_db.delete(self.state / "ecom_demo_queue.json"))
        self.assertIsNone(orch_db.load(self.state / "ecom_demo_queue.json"))
        for i in range(3):
            orch_db.append(self.events, {"event": "e", "task_id": f"task_ecom_{i}", "i": i})
        orch_db.append(self.events, {"event": "e", "task_id": "task_001"})
        self.assertEqual(len(orch_db.read_log(self.events)), 4)
        self.assertEqual(orch_db.read_log(self.events, limit=1, newest_first=True)[0]["task_id"], "task_001")
        self.assertEqual(len(orch_db.read_log(self.events, where_task_prefix="task_ecom_")), 3)
        # LIKE wildcards in the prefix are literal
        self.assertEqual(orch_db.read_log(self.events, where_task_prefix="task%"), [])
        with self.assertRaises(ValueError):
            orch_db.save(self.events, [])
        with self.assertRaises(ValueError):
            orch_db.load(self.state / "other.json")

    def test_audit_tables_are_append_only(self):
        orch_db.append(self.events, {"event": "task_approved", "task_id": "t"})
        orch_db.append(self.state / "auth_audit.jsonl", {"event": "login_ok", "actor": "x"})
        conn = sqlite3.connect(str(self.state / "orch.db"))
        try:
            for sql in ("UPDATE events SET data='{}'", "DELETE FROM events",
                        "UPDATE auth_audit SET actor='y'", "DELETE FROM auth_audit"):
                with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                    conn.execute(sql)
        finally:
            conn.close()

    def test_conditional_update(self):
        put = orch_db.put_task_state_if
        self.assertTrue(put(self.status, "t", {"approval_status": "waiting_approval"}, None))
        self.assertFalse(put(self.status, "t", {"approval_status": "approved"}, None))
        self.assertTrue(put(self.status, "t", {"approval_status": "approved"}, "waiting_approval"))
        self.assertFalse(put(self.status, "t", {"approval_status": "rejected"}, "waiting_approval"))
        self.assertEqual(orch_db.load_task_state(self.status, "t")["approval_status"], "approved")

    def test_transaction_rolls_back_everything(self):
        orch_db.save(self.status, {"t": {"status": "pending"}})
        with self.assertRaises(RuntimeError):
            with orch_db.transaction(self.status):
                orch_db.save(self.status, {"t": {"status": "done"}})
                orch_db.append(self.events, {"event": "task_done", "task_id": "t"})
                raise RuntimeError("boom")
        self.assertEqual(orch_db.load(self.status)["t"]["status"], "pending")
        self.assertEqual(orch_db.read_log(self.events), [])

    def test_backup_api_copy(self):
        orch_db.save(self.status, {"t": {"status": "done"}})
        target = orch_db.backup(self.state, self.tmp / "bk" / "orch.db")
        conn = sqlite3.connect(str(target))
        try:
            self.assertEqual(conn.execute("SELECT task_id FROM task_status").fetchall(), [("t",)])
        finally:
            conn.close()


class MigrationTests(TempState):
    def setUp(self):
        super().setUp()
        for f in FIXTURE.iterdir():            # work on a COPY of the fixture state
            shutil.copy2(f, self.state / f.name)

    def fixture_counts(self):
        def lines(n):
            return sum(1 for l in (FIXTURE / n).read_text(encoding="utf-8").splitlines()
                       if l.strip().endswith("}"))
        return {"task_status": len(json.loads((FIXTURE / "task_status.json").read_text("utf-8"))),
                "events": lines("events.jsonl"), "auth_audit": lines("auth_audit.jsonl"),
                "chat_usage": lines("chat_usage.jsonl")}

    def test_cli_migrate_backup_idempotent_and_export_round_trip(self):
        out = io.StringIO()
        with redirect_stdout(out), patch.dict(os.environ, {"ORCH_AUTH_DIR": str(self.state)}):
            self.assertEqual(orch_db.main(["--state-dir", str(self.state), "migrate"]), 0)
        self.assertIn("schema_version 1", out.getvalue())
        # every legacy file moved into one timestamped backup folder, unchanged
        self.assertEqual(orch_db.legacy_files(self.state), [])
        backups = sorted(self.state.glob("json-backup-*"))
        self.assertEqual(len(backups), 1)
        for f in FIXTURE.iterdir():
            self.assertEqual((backups[0] / f.name).read_bytes(), f.read_bytes())
        # data imported (torn last lines in the old logs are skipped)
        counts = orch_db.summary(self.state)["counts"]
        for table, n in self.fixture_counts().items():
            self.assertEqual(counts[table], n, table)
        self.assertEqual(orch_db.summary(self.state)["docs"],
                         ["auth", "ecom_demo_queue", "ecom_import"])
        self.assertEqual(orch_db.load(self.status),
                         json.loads((FIXTURE / "task_status.json").read_text("utf-8")))
        self.assertEqual(len(orch_db.migration_history(self.state)), 7)
        # idempotent: running again changes nothing
        with redirect_stdout(io.StringIO()) as again:
            orch_db.main(["--state-dir", str(self.state), "migrate"])
        self.assertIn("nothing to migrate", again.getvalue())
        self.assertEqual(orch_db.summary(self.state)["counts"], counts)
        # the same file dropped back in: refused (DB has data), file untouched
        shutil.copy2(FIXTURE / "events.jsonl", self.events)
        with self.assertRaises(orch_db.MigrationRefused):
            orch_db.migrate(self.state)
        self.assertTrue(self.events.is_file())
        # --force: recognised by sha256 and skipped
        report = orch_db.migrate(self.state, force=True)
        self.assertTrue(report[0]["skipped_already_imported"])
        self.assertEqual(orch_db.summary(self.state)["counts"], counts)
        # export (rollback to v0.20 files) round-trips the content
        out_dir = self.tmp / "export"
        orch_db.export_json(self.state, out_dir)
        self.assertEqual(json.loads((out_dir / "task_status.json").read_text("utf-8")),
                         json.loads((FIXTURE / "task_status.json").read_text("utf-8")))
        self.assertEqual(json.loads((out_dir / "auth.json").read_text("utf-8")),
                         json.loads((FIXTURE / "auth.json").read_text("utf-8")))
        self.assertEqual(stat.S_IMODE((out_dir / "auth.json").stat().st_mode), 0o600)
        exported_events = [json.loads(l) for l in
                           (out_dir / "events.jsonl").read_text("utf-8").splitlines()]
        self.assertEqual(len(exported_events), self.fixture_counts()["events"])
        # a fresh state dir fed with the export migrates to the same content
        fresh = self.tmp / "fresh"
        shutil.copytree(out_dir, fresh)
        orch_db.migrate(fresh)
        self.assertEqual(orch_db.summary(fresh)["counts"], counts)

    def test_auto_migration_on_first_open_and_app_modules_read_it(self):
        import commerce_demo
        import orch_auth
        with patch.object(orch_auth, "AUTH_DIR", self.state), \
                patch.object(commerce_demo, "STATUS_FILE", self.status), \
                patch.object(commerce_demo, "EVENTS_FILE", self.events):
            user, reason = orch_auth.authenticate("Ben Lee", FIXTURE_PASSWORD)
            self.assertEqual(reason, "ok")
            self.assertEqual(user["role"], "approver")
            self.assertTrue(orch_auth.can_approve("Ben Lee", "content"))
            self.assertEqual(len(orch_db.read_log(self.events)), 5)
            self.assertTrue(orch_db.load_task_state(self.status, "task_policy_gate"))
        self.assertEqual(orch_db.legacy_files(self.state), [])
        self.assertEqual(len(list(self.state.glob("json-backup-*"))), 1)
        # DB with password hashes is owner-only
        self.assertEqual(stat.S_IMODE((self.state / "orch.db").stat().st_mode) & 0o077, 0)

    def test_failed_migration_moves_files_back(self):
        (self.state / "ecom_import.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            orch_db.migrate(self.state)
        self.assertEqual(len(orch_db.legacy_files(self.state)), 7)
        # nothing left behind: no (empty) backup folder, no empty orch.db
        self.assertEqual(list(self.state.glob("json-backup-*")), [])
        for name in ("orch.db", "orch.db-wal", "orch.db-shm"):
            self.assertFalse((self.state / name).exists(), name)
        # the same through the auto path (first open)
        with self.assertRaises(ValueError):
            orch_db.load(self.status)
        self.assertEqual(list(self.state.glob("json-backup-*")), [])
        self.assertFalse((self.state / "orch.db").exists())
        # fixing the bad file lets the next open migrate everything
        shutil.copy2(FIXTURE / "ecom_import.json", self.state / "ecom_import.json")
        self.assertEqual(len(orch_db.migrate(self.state)), 7)


class FilePermissionTests(TempState):
    """Review fix 4: orch.db / -wal / -shm and exports are 0600 from the start."""

    def setUp(self):
        super().setUp()
        self.old_umask = os.umask(0o022)       # a typical permissive umask
        self.addCleanup(os.umask, self.old_umask)

    def mode(self, path):
        return stat.S_IMODE(Path(path).stat().st_mode)

    def test_new_db_wal_and_shm_are_owner_only(self):
        orch_db.append(self.events, {"event": "e", "task_id": "t"})
        conn = orch_db._open(self.state / "orch.db")      # keeps -wal / -shm alive
        try:
            for name in ("orch.db", "orch.db-wal", "orch.db-shm"):
                self.assertTrue((self.state / name).exists(), name)
                self.assertEqual(self.mode(self.state / name), 0o600, name)
        finally:
            conn.close()

    def test_existing_world_readable_db_is_tightened_on_open(self):
        orch_db.save(self.status, {"t": {}})
        os.chmod(self.state / "orch.db", 0o644)
        orch_db.load(self.status)
        self.assertEqual(self.mode(self.state / "orch.db"), 0o600)

    def test_exports_and_backups_are_owner_only(self):
        orch_db.save(self.state / "ecom_import.json", {"orders": [{"customer": "x"}]})
        orch_db.save(self.state / "auth.json", {"users": {}})
        orch_db.append(self.events, {"event": "e", "task_id": "t"})
        out = self.tmp / "export"
        written = orch_db.export_json(self.state, out)
        self.assertIn("ecom_import.json", written)
        for name in written:
            self.assertEqual(self.mode(out / name), 0o600, name)
        self.assertEqual(self.mode(out), 0o700)
        target = orch_db.backup(self.state, self.tmp / "bk" / "orch.db")
        self.assertEqual(self.mode(target), 0o600)


class MigrationSafetyTests(TempState):
    """Review fix 1: auto-migration only into an EMPTY DB; --force backs up
    first; log re-imports are idempotent."""

    def seed_db(self, tasks=25, events=82):
        orch_db.save(self.status, {f"task_{i:03d}": {"status": "done", "approval_status": None}
                                   for i in range(tasks)})
        for i in range(events):
            orch_db.append(self.events, {"timestamp": f"2026-09-29T00:00:{i % 60:02d}",
                                         "event": "task_done", "task_id": f"task_{i:03d}", "n": i})

    def counts(self):
        return orch_db.summary(self.state)["counts"]

    def test_stray_status_json_never_replaces_rows(self):
        self.seed_db()
        before = orch_db.load(self.status)
        self.status.write_text(json.dumps({"task_999": {"status": "pending"}}), encoding="utf-8")
        with self.assertLogs("orch.db", "ERROR") as logged:
            self.assertEqual(orch_db.load(self.status), before)      # auto path: refused
            orch_db.load(self.status)                                # warned only once
        self.assertEqual(len(logged.records), 1)
        self.assertIn("REFUSED", logged.output[0])
        self.assertIn("task_status.json", logged.output[0])
        self.assertEqual(self.counts()["task_status"], 25)
        self.assertTrue(self.status.is_file())                       # left untouched
        self.assertEqual(list(self.state.glob("json-backup-*")), [])
        # explicit migrate without --force: refused too, CLI exits 1
        with self.assertRaises(orch_db.MigrationRefused):
            orch_db.migrate(self.state)
        err = io.StringIO()
        with patch("sys.stderr", err), redirect_stdout(io.StringIO()), \
                patch.dict(os.environ, {"ORCH_AUTH_DIR": str(self.state)}):
            self.assertEqual(orch_db.main(["--state-dir", str(self.state), "migrate"]), 1)
        self.assertIn("migrate --force", err.getvalue())
        self.assertEqual(self.counts()["task_status"], 25)

    def test_startup_auto_migrate_refuses_without_crashing(self):
        self.seed_db(tasks=3, events=2)
        self.status.write_text(json.dumps({"x": {}}), encoding="utf-8")
        orch_db._refusal_warned.clear()
        with self.assertLogs("orch.db", "ERROR"):
            self.assertEqual(orch_db.auto_migrate(self.state), [])
        self.assertEqual(self.counts()["task_status"], 3)

    def test_force_backs_up_first_and_upserts_without_deleting(self):
        self.seed_db()
        self.status.write_text(json.dumps({"task_000": {"status": "pending"},
                                           "task_999": {"status": "pending"}}), encoding="utf-8")
        report = orch_db.migrate(self.state, force=True)
        backup = Path(report[0]["db_backup"])
        self.assertTrue(backup.name.startswith("orch.db.pre-migrate-"))
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        conn = sqlite3.connect(str(backup))
        try:   # the backup holds the state from BEFORE the import
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM task_status").fetchone()[0], 25)
            self.assertEqual(conn.execute("SELECT data FROM task_status WHERE task_id='task_000'")
                             .fetchone()[0], json.dumps({"status": "done", "approval_status": None}))
        finally:
            conn.close()
        statuses = orch_db.load(self.status)
        self.assertEqual(len(statuses), 26)                           # nothing deleted
        self.assertEqual(statuses["task_000"], {"status": "pending"})
        self.assertFalse(self.status.exists())                        # moved to json-backup-*

    def test_log_reimport_and_export_round_trip_are_idempotent(self):
        self.seed_db(tasks=2, events=82)
        out = self.tmp / "export"
        orch_db.export_json(self.state, out)
        before = self.counts()
        # re-import the export into the same (non-empty) DB, twice
        for attempt in range(2):
            for f in out.iterdir():
                shutil.copy2(f, self.state / f.name)
            # change the bytes so the sha256 shortcut cannot hide duplicates
            with self.events.open("a", encoding="utf-8") as fh:
                fh.write("\n" * (attempt + 1))
            report = orch_db.migrate(self.state, force=True)
            events_row = [r for r in report if r["file"] == "events.jsonl"][0]
            self.assertEqual(events_row["records"], 0)
            self.assertEqual(events_row["duplicates_skipped"], 82)
            self.assertEqual(self.counts(), before)
        # genuinely new records (incl. two identical ones) are still appended
        new = {"timestamp": "2026-09-30T00:00:00", "event": "task_done", "task_id": "z"}
        self.events.write_text(json.dumps(new) + "\n" + json.dumps(new) + "\n", encoding="utf-8")
        orch_db.migrate(self.state, force=True)
        self.assertEqual(self.counts()["events"], 84)


# ---------------------------------------------------------------------------
# Multiprocessing: two processes decide the same draft
# ---------------------------------------------------------------------------

def _sandbox_modules(root):
    """Child-process setup: point every module at the temp sandbox."""
    os.environ["ORCH_DEMO_FORCE_MOCK"] = "1"
    import artifact_store
    import commerce_demo
    import commerce_import
    import mini_orch
    import orch_auth
    root = Path(root)
    state, art = root / "state", root / "artifacts"
    commerce_demo.QUEUE_FILE = state / "ecom_demo_queue.json"
    commerce_demo.MAIN_QUEUE_FILE = root / "task_queue.json"
    commerce_demo.STATUS_FILE = state / "task_status.json"
    commerce_demo.EVENTS_FILE = state / "events.jsonl"
    commerce_demo.LOCK_FILE = state / ".ecom_demo.lock"
    mini_orch.STATUS_FILE = state / "task_status.json"
    mini_orch.EVENTS_FILE = state / "events.jsonl"
    mini_orch.DEMO_QUEUE_FILE = state / "ecom_demo_queue.json"
    mini_orch.QUEUE_FILE = root / "task_queue.json"
    mini_orch.LOCK_FILE = state / ".ecom_demo.lock"
    commerce_import.IMPORT_STATE_FILE = state / "ecom_import.json"
    commerce_import.LOCK_FILE = state / ".ecom_import.lock"
    orch_auth.AUTH_DIR = root / "auth"        # no accounts: typed-name mode
    artifact_store.ARTIFACT_ROOT = art
    artifact_store.STAGING_DIR = art / "staging"
    artifact_store.OBJECTS_DIR = art / "objects" / "sha256"
    artifact_store.MANIFESTS_DIR = art / "manifests"
    artifact_store.LATEST_DIR = art / "latest"
    return commerce_demo, mini_orch


def _race_demo_decide(root, task_id, approver, barrier, results):
    commerce_demo, _ = _sandbox_modules(root)
    barrier.wait(timeout=60)
    try:
        commerce_demo.decide(task_id, "approved", approver, 1,
                             channel="marketplace_listing")
        results.put(("ok", approver))
    except commerce_demo.DemoError as exc:
        results.put(("refused", str(exc.args[0])))


def _race_gate(root, task_id, approver, barrier, results):
    _, mini_orch = _sandbox_modules(root)
    barrier.wait(timeout=60)
    result = mini_orch.decide_approval(task_id, "approved", approver,
                                       queue_file=Path(root) / "task_queue.json",
                                       status_file=mini_orch.STATUS_FILE,
                                       events_file=mini_orch.EVENTS_FILE)
    results.put(("ok", approver) if result["ok"] else ("refused", result["reason"]))


def _race_conditional(db_dir, barrier, results):
    barrier.wait(timeout=60)
    results.put(orch_db.put_task_state_if(Path(db_dir) / "task_status.json", "t",
                                          {"approval_status": "approved", "pid": os.getpid()},
                                          "waiting_approval"))


def _append_many(db_dir, n, barrier):
    barrier.wait(timeout=60)
    for i in range(n):
        orch_db.append(Path(db_dir) / "events.jsonl",
                       {"event": "tick", "task_id": f"p{os.getpid()}", "i": i})


class MultiProcessTests(TempState):
    ROUNDS = 5

    def run_pair(self, target, args_a, args_b):
        ctx = multiprocessing.get_context("spawn")
        barrier, results = ctx.Barrier(2), ctx.Queue()
        procs = [ctx.Process(target=target, args=(*args, barrier, results))
                 for args in (args_a, args_b)]
        for p in procs:
            p.start()
        out = [results.get(timeout=120) for _ in procs]
        for p in procs:
            p.join(timeout=60)
            self.assertEqual(p.exitcode, 0)
        return out

    def test_two_processes_conditional_update_exactly_one_wins(self):
        for _ in range(self.ROUNDS):
            orch_db.save(self.status, {"t": {"approval_status": "waiting_approval"}})
            out = self.run_pair(_race_conditional, (str(self.state),), (str(self.state),))
            self.assertEqual(sorted(out), [False, True])

    def test_concurrent_appends_are_all_kept(self):
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(2)
        procs = [ctx.Process(target=_append_many, args=(str(self.state), 60, barrier))
                 for _ in range(2)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=120)
            self.assertEqual(p.exitcode, 0)
        self.assertEqual(len(orch_db.read_log(self.events)), 120)

    def test_two_processes_approve_same_draft_exactly_one_audit(self):
        (self.tmp / "task_queue.json").write_text("[]", encoding="utf-8")
        commerce_demo, _ = _sandbox_modules(self.tmp)
        try:
            for round_no in range(self.ROUNDS):
                with redirect_stdout(io.StringIO()):
                    task_id = commerce_demo.create_draft(
                        "content", {"sku": "SAMPLE-001", "content_type": "product_page"},
                        "Amy Chan", language="zh-Hant")["task_id"]
                out = self.run_pair(_race_demo_decide,
                                    (str(self.tmp), task_id, "Ben Lee"),
                                    (str(self.tmp), task_id, "Cara Wong"))
                kinds = sorted(k for k, _ in out)
                self.assertEqual(kinds, ["ok", "refused"], out)
                self.assertIn(("refused", "not_pending"), out)
                approved = [e for e in orch_db.read_log(self.events)
                            if e.get("task_id") == task_id and e.get("event") == "task_approved"]
                self.assertEqual(len(approved), 1, round_no)
                audits = [m for m in (self.tmp / "artifacts" / "manifests").glob("*.json")
                          if json.loads(m.read_text("utf-8")).get("logical_name") == "ecom_audit"
                          and json.loads(m.read_text("utf-8")).get("producer_task_id") == task_id]
                self.assertEqual(len(audits), 1, round_no)
                state = orch_db.load_task_state(self.status, task_id)
                winner = [who for k, who in out if k == "ok"][0]
                self.assertEqual(state["approval_status"], "approved")
                self.assertEqual(state["ecom"]["decision"]["approver"], winner)
                self.assertTrue(state["ecom"]["decision"]["audit_artifact_id"])
        finally:
            import importlib
            for name in ("commerce_demo", "mini_orch", "commerce_import", "orch_auth",
                         "artifact_store"):
                importlib.reload(sys.modules[name])

    def test_two_processes_on_the_mini_orch_gate(self):
        queue = [{"id": f"task_gate_{i}", "title": "gate", "command": ["true"], "priority": 1,
                  "depends_on": [], "max_retries": 0, "requires_approval": True}
                 for i in range(self.ROUNDS)]
        (self.tmp / "task_queue.json").write_text(json.dumps(queue), encoding="utf-8")
        for task in queue:
            orch_db.put_task_state(self.status, task["id"],
                                   {"status": "waiting_approval",
                                    "approval_status": "waiting_approval"})
            out = self.run_pair(_race_gate, (str(self.tmp), task["id"], "Ben Lee"),
                                (str(self.tmp), task["id"], "Cara Wong"))
            self.assertEqual(sorted(k for k, _ in out), ["ok", "refused"], out)
            approved = [e for e in orch_db.read_log(self.events)
                        if e.get("task_id") == task["id"] and e.get("event") == "task_approved"]
            self.assertEqual(len(approved), 1)


class MiniOrchCliTests(TempState):
    def run_cli(self, *args):
        env = {k: v for k, v in os.environ.items() if k not in ("ORCH_AUTH_DIR", "ORCH_STATE_DIR")}
        return subprocess.run([sys.executable, str(REPO / "mini_orch.py"), *args],
                              cwd=self.tmp, env=env, capture_output=True, text=True, timeout=120)

    def test_cli_status_and_approve_against_the_db(self):
        queue = [{"id": "task_cli_gate", "title": "CLI gate", "command": ["true"],
                  "priority": 1, "depends_on": [], "max_retries": 0, "requires_approval": True}]
        (self.tmp / "task_queue.json").write_text(json.dumps(queue), encoding="utf-8")
        # legacy JSON state from v0.20 is migrated on first use
        self.status.write_text(json.dumps({"task_cli_gate": {
            "status": "waiting_approval", "approval_status": "waiting_approval"}}), encoding="utf-8")
        result = self.run_cli("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("task_cli_gate", result.stdout)
        self.assertFalse(self.status.exists())
        self.assertTrue((self.state / "orch.db").is_file())
        result = self.run_cli("approve", "task_cli_gate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Approved: task_cli_gate", result.stdout)
        state = orch_db.load_task_state(self.status, "task_cli_gate")
        self.assertEqual(state["approval_status"], "approved")
        events = orch_db.read_log(self.events)
        self.assertEqual([e["event"] for e in events].count("task_approved"), 1)
        # v0.20 CLI semantics kept: a rejected task cannot be re-approved.
        orch_db.put_task_state(self.status, "task_cli_gate",
                               {"status": "rejected", "approval_status": "rejected"})
        again = self.run_cli("approve", "task_cli_gate")
        self.assertIn("rejected by a human", again.stdout)
        self.assertEqual([e["event"] for e in orch_db.read_log(self.events)].count("task_approved"), 1)


if __name__ == "__main__":
    unittest.main()
