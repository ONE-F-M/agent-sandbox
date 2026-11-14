"""The session agent: registers once, serves numbered commands on its own checkout, pushes at checkpoints."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

import dev_agent_server as srv
import session_agent as agent

SETTINGS = {
    "accepted": True, "session_key": "k" * 32, "work_item": "WI-1", "attempt": 1, "target_app": "one_bpmn",
    "git_branch": "staging", "agent_name": "Dev Agent", "github_token": "", "idle_timeout_seconds": 600,
}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class FakeClient:
    """Stands in for Processa: hands out the queued commands, then none, and records what the agent sent."""

    def __init__(self, commands=()):
        self.session_id = "SESS-1"
        self.key = SETTINGS["session_key"]
        self.commands = list(commands)
        self.sent = []

    def call(self, endpoint, body):
        self.sent.append((endpoint, body))
        if endpoint == "next_command":
            return {"accepted": True, "command": self.commands.pop(0) if self.commands else None}
        return {"accepted": True}

    def results(self):
        return [body for endpoint, body in self.sent if endpoint == "post_result"]


class SessionCase(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        origin = os.path.join(self.root, "origin.git")
        seed = os.path.join(self.root, "seed")
        os.makedirs(seed)
        git(self.root, "init", "-q", "--bare", origin)
        git(seed, "init", "-q", "-b", "staging")
        for name, text in (("one_bpmn/a.py", "one two\n"), ("one_bpmn/b.py", "three\n")):
            path = os.path.join(seed, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(text)
        git(seed, "add", "-A")
        git(seed, "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q", "-m", "seed")
        git(seed, "push", "-q", origin, "staging")
        self.bench = os.path.join(self.root, "bench")
        self.app = os.path.join(self.bench, "apps", "one_bpmn")
        os.makedirs(os.path.dirname(self.app))
        git(self.root, "clone", "-q", origin, self.app)
        git(self.app, "config", "user.email", "a@b")
        git(self.app, "config", "user.name", "t")
        self.origin = origin
        patcher = patch.object(srv, "BENCH_DIR", self.bench)
        patcher.start()
        self.addCleanup(patcher.stop)
        srv._test_results.clear()

    def _session(self, commands=(), **settings):
        client = FakeClient(commands)
        session = agent.Session(client, {**SETTINGS, **settings})
        session.prepare()
        return session, client

    def _command(self, seq, kind, action="", **payload):
        return {"seq": seq, "kind": kind, "action": action, "payload": payload}

    def _pushed(self, path):
        return git(self.origin, "show", f"WI-1:{path}")


class TestPreparing(SessionCase):
    def test_the_work_items_branch_is_created_and_checked_out_once(self):
        _session, _client = self._session()
        self.assertEqual(git(self.app, "rev-parse", "--abbrev-ref", "HEAD"), "WI-1")
        self.assertIn("WI-1", git(self.origin, "branch"))

    def test_a_branch_that_already_exists_is_resumed_with_its_commits(self):
        session, _ = self._session()
        session.execute(self._command(1, "tool", "edit_file", args={"path": "one_bpmn/a.py", "old_string": "one", "new_string": "1"}))
        session.checkpoint()
        git(self.app, "checkout", "-q", "staging")
        resumed, _ = self._session()
        with open(os.path.join(self.app, "one_bpmn/a.py")) as fh:
            self.assertEqual(fh.read(), "1 two\n")

    def test_the_token_is_exported_for_pushes_and_a_failed_checkout_is_an_error(self):
        self._session(github_token="tok")
        self.assertEqual(os.environ["GITHUB_TOKEN"], "tok")
        os.environ["GITHUB_TOKEN"] = ""
        client = FakeClient()
        broken = agent.Session(client, {**SETTINGS, "git_branch": "no-such-branch", "work_item": "WI-2"})
        with self.assertRaises(RuntimeError):
            broken.prepare()


class TestToolCommands(SessionCase):
    def test_a_read_returns_the_content_changes_nothing_and_reports_its_timing(self):
        session, _ = self._session()
        status, result = session.execute(self._command(1, "tool", "read_file", args={"path": "one_bpmn/a.py"}))
        self.assertEqual((status, result["content"]), ("ok", "one two\n"))
        self.assertIn("tool", result["timings_ms"])
        self.assertFalse(session.unpushed)

    def test_an_edit_is_committed_locally_and_not_pushed_until_a_checkpoint(self):
        session, _ = self._session()
        status, result = session.execute(self._command(1, "tool", "edit_file", args={"path": "one_bpmn/a.py", "old_string": "two", "new_string": "2"}))
        self.assertEqual((status, result["edited"]), ("ok", True))
        self.assertIn("edit_file via Dev Agent", git(self.app, "log", "-1", "--format=%s"))
        self.assertTrue(session.unpushed)
        self.assertEqual(self._pushed("one_bpmn/a.py"), "one two")
        session.checkpoint()
        self.assertEqual(self._pushed("one_bpmn/a.py"), "one 2")
        self.assertFalse(session.unpushed)

    def test_several_edits_make_one_commit(self):
        session, _ = self._session()
        before = git(self.app, "rev-list", "--count", "HEAD")
        edits = [{"path": "one_bpmn/a.py", "old_string": "one", "new_string": "1"}, {"path": "one_bpmn/b.py", "old_string": "three", "new_string": "3"}]
        session.execute(self._command(1, "tool", "apply_edits", args={"edits": edits}))
        self.assertEqual(int(git(self.app, "rev-list", "--count", "HEAD")) - int(before), 1)
        self.assertEqual(sorted(git(self.app, "show", "--name-only", "--format=", "HEAD").split()), ["one_bpmn/a.py", "one_bpmn/b.py"])

    def test_a_write_and_a_delete_are_committed(self):
        session, _ = self._session()
        session.execute(self._command(1, "tool", "write_file", args={"path": "one_bpmn/new.py", "content": "x\n"}))
        session.execute(self._command(2, "tool", "delete_file", args={"path": "one_bpmn/b.py"}))
        session.checkpoint()
        self.assertEqual(self._pushed("one_bpmn/new.py"), "x")
        self.assertNotIn("one_bpmn/b.py", git(self.origin, "ls-tree", "-r", "--name-only", "WI-1"))

    def test_a_failed_edit_is_an_error_result_with_no_commit(self):
        session, _ = self._session()
        status, result = session.execute(self._command(1, "tool", "edit_file", args={"path": "one_bpmn/a.py", "old_string": "absent", "new_string": "x"}))
        self.assertEqual(status, "error")
        self.assertIn("not found", result["error"])
        self.assertFalse(session.unpushed)

    def test_a_search_and_a_listing_work_on_the_checkout(self):
        session, _ = self._session()
        _, found = session.execute(self._command(1, "tool", "grep_files", args={"pattern": "three", "literal": True}))
        self.assertEqual([m["path"] for m in found["matches"]], ["one_bpmn/b.py"])
        _, listed = session.execute(self._command(2, "tool", "list_files", args={"path_prefix": "one_bpmn"}))
        self.assertEqual(sorted(listed["files"]), ["one_bpmn/a.py", "one_bpmn/b.py"])

    def test_an_unknown_kind_or_a_crash_becomes_an_error_result_not_an_exception(self):
        session, _ = self._session()
        status, result = session.execute(self._command(1, "dance"))
        self.assertEqual(status, "error")
        self.assertIn("KeyError", result["error"])
        with patch.object(srv, "_apply_tool", side_effect=RuntimeError("disk full")):
            status, result = session.execute(self._command(2, "tool", "read_file", args={}))
        self.assertEqual((status, result["error"]), ("error", "RuntimeError: disk full"))


class TestSlowCommands(SessionCase):
    def _edited(self):
        session, client = self._session()
        session.execute(self._command(1, "tool", "edit_file", args={"path": "one_bpmn/a.py", "old_string": "two", "new_string": "2"}))
        return session, client

    def test_tests_run_after_the_work_is_pushed_and_report_the_callback_shape(self):
        session, _ = self._edited()
        with patch.object(srv, "_tests_for_tree", return_value=(False, "out", "err", ["test_x (m.T)"])) as tests:
            status, result = session.execute(self._command(2, "run_tests", "run_tests", args={}))
        self.assertEqual(status, "ok")
        self.assertEqual(
            {k: result[k] for k in ("status", "stdout_tail", "stderr_tail", "failing_tests")},
            {"status": "tests_failed", "stdout_tail": "out", "stderr_tail": "err", "failing_tests": ["test_x (m.T)"]},
        )
        tests.assert_called_once_with("one_bpmn", "staging")
        self.assertEqual(result["commit"], git(self.app, "rev-parse", "HEAD"))
        self.assertEqual(self._pushed("one_bpmn/a.py"), "one 2")
        self.assertIn("tests", result["timings_ms"])

    def test_a_passing_run_says_so(self):
        session, _ = self._edited()
        with patch.object(srv, "_tests_for_tree", return_value=(True, "", "", [])):
            _status, result = session.execute(self._command(2, "run_tests", "run_tests", args={}))
        self.assertEqual(result["status"], "tests_passed")

    def test_the_site_is_migrated_only_for_a_schema_change(self):
        session, _ = self._edited()
        with patch.object(srv, "_tests_for_tree", return_value=(True, "", "", [])), patch.object(srv, "_migrate_site") as migrate:
            session.execute(self._command(2, "run_tests", "run_tests", args={}))
        migrate.assert_not_called()
        session.execute(self._command(3, "tool", "write_file", args={"path": "one_bpmn/one_bpmn/doctype/x/x.json", "content": "{}\n"}))
        with patch.object(srv, "_tests_for_tree", return_value=(True, "", "", [])), patch.object(srv, "_migrate_site", return_value=(True, "", "")) as migrate:
            session.execute(self._command(4, "run_tests", "run_tests", args={}))
        migrate.assert_called_once()

    def test_a_failed_migration_is_reported_and_no_tests_run(self):
        session, _ = self._edited()
        session.execute(self._command(3, "tool", "write_file", args={"path": "one_bpmn/patches/p.py", "content": "x\n"}))
        with patch.object(srv, "_migrate_site", return_value=(False, "out", "boom")), patch.object(srv, "_tests_for_tree") as tests:
            _status, result = session.execute(self._command(4, "run_tests", "run_tests", args={}))
        self.assertEqual((result["status"], result["error"]), ("failed", "boom"))
        tests.assert_not_called()

    def test_finalize_pushes_then_opens_the_pull_request_and_returns_its_url(self):
        session, _ = self._edited()
        with patch.object(srv, "_tool_open_pull_request", return_value={"pr_url": "https://github.com/o/r/pull/9"}) as open_pr:
            status, result = session.execute(self._command(2, "finalize", "open_pull_request", args={"summary": "Did it."}, work_item_description="Fix it."))
        self.assertEqual((status, result["status"], result["pr_url"]), ("ok", "tests_passed", "https://github.com/o/r/pull/9"))
        self.assertEqual(self._pushed("one_bpmn/a.py"), "one 2")
        target_app, args, run_ctx = open_pr.call_args.args
        self.assertEqual((target_app, args), ("one_bpmn", {"summary": "Did it."}))
        self.assertEqual((run_ctx["work_item_id"], run_ctx["work_item_description"], run_ctx["git_branch"]), ("WI-1", "Fix it.", "staging"))

    def test_a_pull_request_that_could_not_open_reports_why(self):
        session, _ = self._edited()
        failing = {"error": "GitHub API error (403)", "failing_tests": ["t"]}
        with patch.object(srv, "_tool_open_pull_request", return_value=failing):
            _status, result = session.execute(self._command(2, "finalize", "open_pull_request", args={}))
        self.assertEqual((result["status"], result["pr_error"], result["failing_tests"]), ("tests_failed", "GitHub API error (403)", ["t"]))
        self.assertNotIn("pr_url", result)

    def test_a_push_that_fails_is_an_error_result_not_a_silent_loss(self):
        session, _ = self._edited()
        shutil.rmtree(self.origin)
        status, result = session.execute(self._command(2, "run_tests", "run_tests", args={}))
        self.assertEqual(status, "error")
        self.assertIn("git push", result["error"])
        self.assertTrue(session.unpushed)


class TestTheLoop(SessionCase):
    def test_commands_are_answered_in_order_with_their_sequence_numbers_and_close_ends_it(self):
        read = self._command(1, "tool", "read_file", args={"path": "one_bpmn/a.py"})
        edit = self._command(2, "tool", "edit_file", args={"path": "one_bpmn/a.py", "old_string": "one", "new_string": "1"})
        session, client = self._session([read, edit, self._command(3, "close", "close")])
        self.assertEqual(session.run(), "closed")
        results = client.results()
        self.assertEqual([r["seq"] for r in results], [1, 2, 3])
        self.assertEqual([r["status"] for r in results], ["ok", "ok", "ok"])
        self.assertEqual(self._pushed("one_bpmn/a.py"), "1 two")

    def test_with_no_commands_the_agent_polls_then_ends_itself_when_idle(self):
        session, client = self._session()
        session.idle_timeout = 0
        with patch.object(agent.time, "sleep"):
            self.assertEqual(session.run(), "idle")
        self.assertTrue(all(endpoint == "next_command" for endpoint, _ in client.sent))

    def test_the_agent_waits_past_the_idle_timeout_so_processa_closes_the_session_first(self):
        session, _client = self._session(idle_timeout_seconds=600)
        self.assertEqual(session.idle_timeout, 600 + agent.IDLE_GRACE_SECONDS)

    def test_the_poll_delay_grows_to_a_limit_and_resets_after_work(self):
        session, client = self._session([None, None, None, self._command(1, "close", "close")])
        client.commands = [None] * 3 + [self._command(1, "close", "close")]
        delays = []

        def call(endpoint, body):
            if endpoint == "next_command":
                return {"accepted": True, "command": client.commands.pop(0) if client.commands else None}
            return {"accepted": True}

        client.call = call
        with patch.object(agent.time, "sleep", side_effect=delays.append):
            session.run()
        self.assertEqual(delays[0], agent.POLL_MIN_SECONDS)
        self.assertGreater(delays[1], delays[0])
        self.assertLessEqual(max(delays), agent.POLL_MAX_SECONDS)

    def test_an_idle_session_with_unpushed_work_checkpoints_on_the_timer(self):
        session, _client = self._session()
        session.execute(self._command(1, "tool", "write_file", args={"path": "one_bpmn/n.py", "content": "x\n"}))
        session.last_push -= agent.CHECKPOINT_SECONDS + 1
        calls = iter([{"accepted": True, "command": None}, {"accepted": True, "command": {"seq": 2, "kind": "close", "action": "close", "payload": {}}}])
        session.client.call = lambda endpoint, body: next(calls) if endpoint == "next_command" else {"accepted": True}
        with patch.object(agent.time, "sleep"):
            session.run()
        self.assertEqual(self._pushed("one_bpmn/n.py"), "x")

    def test_a_refused_poll_stops_the_agent(self):
        session, client = self._session()
        client.call = lambda endpoint, body: {"accepted": False}
        with self.assertRaises(agent.SessionRefused):
            session.run()


PASSWORD = "Tp-s3cret-pw!"


class TestingAgentCase(SessionCase):
    """prepare_site / checkout_pr_head with real git (a local origin) and faked bench, mysqldump, services and HTTP."""

    def setUp(self):
        super().setUp()
        self.snapshots = os.path.join(self.root, "snapshots")
        for target, value in (("SNAPSHOT_DIR", self.snapshots), ("READY_TIMEOUT_SECONDS", 4)):
            patcher = patch.object(srv, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        sites = os.path.join(self.bench, "sites")
        os.makedirs(os.path.join(sites, "sandbox"))
        for path, conf in (("common_site_config.json", {"db_host": "127.0.0.1"}), ("sandbox/site_config.json", {"db_name": "_db1", "db_password": "dbsecret"})):
            with open(os.path.join(sites, path), "w") as fh:
                json.dump(conf, fh)
        self.calls, self.broken, self.dump_bytes = [], set(), "-- dump\n"
        self.pinged, self.stopped, self.popen = [False, True], [], []
        self.session, self.client = self._session()
        self.base_sha = git(self.origin, "rev-parse", "staging")
        # The pull request: one commit on top of staging, published only as refs/pull/5/head (no branch).
        pr = os.path.join(self.root, "pr")
        git(self.root, "clone", "-q", self.origin, pr)
        self.pr = pr
        self._repoint_pr("one_bpmn/b.py", "backend\n")
        for target, side_effect in (("_exec", self._fake_exec), ("_ping", lambda *a: self.pinged.pop(0) if self.pinged else True),
                                    ("_pid_alive", lambda pid: True), ("_stop_services", lambda pids: self.stopped.append(list(pids)))):
            patcher = patch.object(srv, target, side_effect=side_effect)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(srv.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(srv, "_spawn", side_effect=self._fake_popen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _repoint_pr(self, path, text):
        """Make the PR head a new commit on top of base touching only `path`, and publish it as refs/pull/5/head."""
        git(self.pr, "fetch", "-q", "origin", "staging")
        git(self.pr, "checkout", "-q", "-f", "-B", "work", "FETCH_HEAD")
        full = os.path.join(self.pr, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write(text)
        git(self.pr, "add", "-A")
        git(self.pr, "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q", "-m", "pr")
        git(self.pr, "push", "-q", "-f", "origin", "HEAD:refs/pull/5/head")
        self.pr_sha = git(self.pr, "rev-parse", "HEAD")

    def _fake_exec(self, args, env=None, cwd=None, timeout=None):
        self.calls.append((list(args), dict(env or {})))
        done = lambda out="", rc=0, err="": subprocess.CompletedProcess(args, rc, out, err)
        if args[0] == "git":
            return subprocess.run(args, cwd=cwd, capture_output=True, text=True)
        if args[0].endswith("python"):
            key = "users"
        elif args[0] == "mysqldump":
            key = "mysqldump"
        elif args[1] == "build":
            key = "build"
        else:
            key = args[3]  # bench --site sandbox <migrate|set-config>
        if key in self.broken:
            return done("out-line", 1, f"{key} blew up, db_password=dbsecret {PASSWORD}")
        if key == "users":
            plan = json.loads(env["SANDBOX_TEST_USERS"])
            return done("noise\nUSERS_JSON=" + json.dumps([{**u, "created": True} for u in plan]) + "\n")
        if key == "mysqldump":
            with open(next(a.split("=", 1)[1] for a in args if a.startswith("--result-file=")), "w") as fh:
                fh.write(self.dump_bytes)
        return done()

    def _fake_popen(self, args, **kwargs):
        self.popen.append((list(args), kwargs))
        return MagicMock(pid=4000 + len(self.popen))

    def prepare(self, **over):
        payload = {"base_branch": "staging", "pr_number": 5, "run_id": "run-1", "roles": ["Sales User", "HR Manager"], "password": PASSWORD, **over}
        return self.session.execute(self._command(1, "prepare_site", **payload))

    def checkout(self, **over):
        return self.session.execute(self._command(2, "checkout_pr_head", **{"pr_number": 5, "run_id": "run-1", **over}))

    def argv(self, *prefix):
        return [a for a, _ in self.calls if a[: len(prefix)] == list(prefix)]


class TestPrepareSite(TestingAgentCase):
    def test_the_payload_is_validated_before_anything_runs(self):
        bad = ({"run_id": "../x"}, {"run_id": "a/b"}, {"pr_number": "5; rm"}, {"pr_number": 0}, {"pr_number": True}, {"base_branch": "a..b"},
               {"base_branch": "-x"}, {"roles": []}, {"roles": "Sales"}, {"roles": ["ok", "bad;role"]}, {"password": "short"},
               {"password": " padded-password "}, {"roles": ["A B", "A-B"]})
        for over in bad:
            status, result = self.prepare(**over)
            self.assertEqual((status, result["status"], result["step"]), ("ok", "failed", "validate"), over)
        self.assertEqual(self.calls, [])
        _status, result = self.session.execute(self._command(3, "prepare_site", base_branch="staging"))
        self.assertEqual(result["step"], "validate")

    def test_it_checks_out_base_migrates_then_makes_one_user_per_role_plus_one_with_none(self):
        status, result = self.prepare(roles=["Sales User", "HR Manager", "Sales User"])
        self.assertEqual((status, result["status"]), ("ok", "prepared"))
        self.assertEqual(self.argv("bench", "--site", "sandbox", "migrate"), [["bench", "--site", "sandbox", "migrate"]])
        self.assertEqual(result["base_sha"], self.base_sha)
        self.assertEqual(git(self.app, "rev-parse", "HEAD"), self.base_sha)
        self.assertEqual([u["role"] for u in result["users"]], ["Sales User", "HR Manager", None])
        self.assertEqual(len({u["email"] for u in result["users"]}), 3)
        order = [a[0] for a, _ in self.calls if a[0] != "git"]
        self.assertEqual(order, ["bench", f"{self.bench}/env/bin/python", "bench", "mysqldump"])

    def test_one_password_from_the_payload_is_used_for_every_user_and_never_leaks(self):
        _status, result = self.prepare()
        users_env = next(env for args, env in self.calls if args[0].endswith("python"))
        self.assertEqual(users_env["SANDBOX_TEST_PASSWORD"], PASSWORD)
        self.assertEqual(len(json.loads(users_env["SANDBOX_TEST_USERS"])), 3)
        self.assertNotIn(PASSWORD, "".join(a for args, _ in self.calls for a in args))
        self.assertNotIn(PASSWORD, json.dumps(result))
        self.assertNotIn(PASSWORD, json.dumps(self.client.sent))

    def test_the_user_script_creates_each_user_once_with_that_role_and_updates_on_a_repeat(self):
        db = {}

        class Doc:
            def update(self, values):
                self.__dict__.update(values)

            def set(self, key, value):
                setattr(self, key, value)

            def append(self, key, row):
                getattr(self, key).append(row)

            def save(self, ignore_permissions=False):
                db[self.email] = self

        fake = MagicMock()
        fake.db.exists.side_effect = lambda doctype, name: (doctype == "Role" and name != "Ghost") or (doctype == "User" and name in db)
        fake.new_doc.side_effect = lambda doctype: Doc()
        fake.get_doc.side_effect = lambda doctype, name: db[name]
        env = {"SANDBOX_TEST_USERS": json.dumps(srv._user_plan("run-1", ["Sales User", "HR Manager"])), "SANDBOX_TEST_PASSWORD": PASSWORD, "SANDBOX_SITE": "sandbox"}
        for _ in range(2):
            with patch.dict("sys.modules", {"frappe": fake}), patch.dict(os.environ, env), patch("builtins.print") as out:
                exec(compile(srv._ENSURE_USERS_SCRIPT, "script", "exec"), {})
        self.assertEqual(len(db), 3)
        self.assertEqual(sorted(len(u.roles) for u in db.values()), [0, 1, 1])
        self.assertEqual({u.new_password for u in db.values()}, {PASSWORD})
        self.assertFalse(json.loads(out.call_args.args[0].split("=", 1)[1])[0]["created"])
        ghost = {**env, "SANDBOX_TEST_USERS": json.dumps([{"email": "g@x.test", "role": "Ghost", "first_name": "G"}])}
        with patch.dict("sys.modules", {"frappe": fake}), patch.dict(os.environ, ghost), self.assertRaisesRegex(Exception, "role does not exist"):
            exec(compile(srv._ENSURE_USERS_SCRIPT, "script", "exec"), {})

    def test_mute_emails_is_set_before_the_snapshot(self):
        self.prepare()
        self.assertEqual(len(self.argv("bench", "--site", "sandbox", "set-config", "mute_emails", "1")), 1)
        names = [" ".join(a) for a, _ in self.calls if a[0] != "git"]
        self.assertLess(next(i for i, n in enumerate(names) if "mute_emails" in n), next(i for i, n in enumerate(names) if n.startswith("mysqldump")))

    def test_the_snapshot_is_a_non_empty_dump_taken_before_pr_checkout_with_no_db_password_in_args(self):
        _status, result = self.prepare()
        path = os.path.join(self.snapshots, "run-1.sql")
        self.assertEqual((result["snapshot_path"], result["snapshot_bytes"]), (path, len("-- dump\n")))
        self.assertFalse(os.path.exists(path + ".part"))
        args, env = next((a, e) for a, e in self.calls if a[0] == "mysqldump")
        self.assertNotIn("dbsecret", " ".join(args))
        self.assertEqual(env["MYSQL_PWD"], "dbsecret")
        self.assertEqual(git(self.app, "rev-parse", "HEAD"), self.base_sha)  # still on base; PR not fetched
        self.assertEqual(self.argv("git", "fetch", "origin", "refs/pull/5/head"), [])

    def test_an_empty_or_failed_dump_fails_loudly_and_leaves_no_file(self):
        self.dump_bytes = ""
        _status, result = self.prepare()
        self.assertEqual((result["status"], result["step"]), ("failed", "snapshot"))
        self.assertEqual(os.listdir(self.snapshots), [])
        self.dump_bytes = "-- dump\n"
        self.broken.add("mysqldump")
        _status, result = self.prepare()
        self.assertEqual(result["step"], "snapshot")
        self.assertNotIn("dbsecret", json.dumps(result))
        self.assertNotIn(PASSWORD, json.dumps(result))
        self.assertEqual(os.listdir(self.snapshots), [])

    def test_a_repeat_run_does_not_duplicate_users_and_each_run_keeps_its_own_snapshot(self):
        self.prepare()
        first = open(os.path.join(self.snapshots, "run-1.sql")).read()
        self.dump_bytes = "-- other\n"
        self.prepare(run_id="run-2")
        self.assertEqual(open(os.path.join(self.snapshots, "run-1.sql")).read(), first)
        self.assertEqual(sorted(os.listdir(self.snapshots)), ["run-1.json", "run-1.sql", "run-2.json", "run-2.sql"])
        self.prepare()  # same run again: the same emails, so the script updates rather than inserts
        plans = [json.loads(e["SANDBOX_TEST_USERS"]) for a, e in self.calls if a[0].endswith("python")]
        self.assertEqual([plans[0], plans[2]], [srv._user_plan("run-1", ["Sales User", "HR Manager"])] * 2)
        self.assertNotEqual(plans[0], plans[1])

    def test_each_step_failing_is_reported_with_its_step_and_stops_the_rest(self):
        for key, step in (("migrate", "migrate"), ("users", "create_users"), ("set-config", "mute_emails")):
            self.calls.clear()
            self.broken = {key}
            _status, result = self.prepare()
            self.assertEqual((result["status"], result["step"]), ("failed", step), key)
            self.assertNotIn(PASSWORD, json.dumps(result))
            self.assertEqual(self.argv("mysqldump"), [])
        self.broken = set()
        with patch.object(srv, "_checkout_target_branch", return_value=(False, "git fetch failed")):
            _status, result = self.prepare()
        self.assertEqual((result["step"], result["error"]), ("checkout_base", "git fetch failed"))

    def test_a_missing_base_branch_fails_at_checkout(self):
        _status, result = self.prepare(base_branch="nope")
        self.assertEqual((result["status"], result["step"]), ("failed", "checkout_base"))


class TestCheckoutPrHead(TestingAgentCase):
    def setUp(self):
        super().setUp()
        self.prepare()
        self.calls.clear()

    def test_it_refuses_to_run_unless_the_site_was_prepared_for_that_run_and_pr(self):
        for over in ({"run_id": "never"}, {"pr_number": 6}):
            _status, result = self.checkout(**over)
            self.assertEqual((result["status"], result["step"]), ("failed", "verify_base"), over)
        for over in ({"pr_number": "x"}, {"run_id": "../../etc"}, {"pr_number": -1}):
            self.assertEqual(self.checkout(**over)[1]["step"], "validate")
        open(os.path.join(self.snapshots, "run-1.sql"), "w").close()
        self.assertEqual(self.checkout()[1]["step"], "verify_base")
        self.assertEqual(self.calls, [])

    def test_the_pull_ref_is_fetched_and_that_exact_commit_is_checked_out(self):
        _status, result = self.checkout()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(self.argv("git", "fetch", "origin", "refs/pull/5/head")), 1)
        self.assertEqual(result["pr_head_sha"], self.pr_sha)
        self.assertEqual(git(self.app, "rev-parse", "HEAD"), self.pr_sha)
        # a later push to the PR does not move what was checked out: it is a commit, not a branch name
        self._repoint_pr("one_bpmn/b.py", "moved\n")
        self.assertNotEqual(git(self.app, "rev-parse", "HEAD"), self.pr_sha)

    def test_frontend_or_public_changes_trigger_one_build_and_backend_only_does_not(self):
        self.assertFalse(self.checkout()[1]["built_assets"])
        self.assertEqual(self.argv("bench", "build"), [])
        for path in ("spiff/src/App.vue", "one_bpmn/public/js/x.js"):
            self.calls.clear()
            self.pinged[:] = [False, True]
            self._repoint_pr(path, "x\n")
            _status, result = self.checkout()
            self.assertEqual(self.argv("bench", "build"), [["bench", "build", "--app", "one_bpmn"]], path)
            self.assertTrue(result["built_assets"])
        for path in ("one_bpmn/api/x.py", "docs/public.md", "one_bpmn/publicity/x.js", "spiffy/x.js"):
            self.calls.clear()
            self.pinged[:] = [False, True]
            self._repoint_pr(path, "x\n")
            self.assertFalse(self.checkout()[1]["built_assets"], path)
            self.assertEqual(self.argv("bench", "build"), [], path)

    def test_a_failing_build_stops_before_migrate(self):
        self._repoint_pr("spiff/src/App.vue", "x\n")
        self.broken = {"build"}
        _status, result = self.checkout()
        self.assertEqual((result["status"], result["step"]), ("failed", "build"))
        self.assertEqual(self.popen, [])
        self.assertEqual(self.argv("bench", "--site", "sandbox", "migrate"), [])

    def test_a_failed_migration_returns_the_log_tail_and_starts_nothing(self):
        self.broken = {"migrate"}
        _status, result = self.checkout()
        self.assertEqual((result["status"], result["step"]), ("failed", "migrate"))
        self.assertIn("migrate blew up", result["migration_tail"])
        self.assertNotIn("ping", result)
        self.assertEqual(self.popen, [])

    def test_services_start_after_migrate_one_serve_on_8000_and_exactly_one_worker(self):
        _status, result = self.checkout()
        self.assertEqual([a for a, _ in self.popen], [["bench", "--site", "sandbox", "serve", "--port", "8000"], ["bench", "worker", "--queue", "short,default,long"]])
        self.assertTrue(all(k["start_new_session"] for _, k in self.popen))
        flat = " ".join(" ".join(a) for a, _ in self.popen)
        for banned in ("bench start", "redis", "watch", "socketio"):
            self.assertNotIn(banned, flat)
        self.assertEqual((result["port"], result["serve_pid"], result["worker_pid"], result["ping"]), (8000, 4001, 4002, "pong"))
        with open(os.path.join(self.bench, "logs", "sandbox_services.json")) as fh:
            self.assertEqual(json.load(fh), {"pids": [4001, 4002]})

    def test_a_second_call_replaces_the_first_services_instead_of_adding_more(self):
        self.checkout()
        self.pinged[:] = [False, True]
        self.checkout()
        self.assertEqual(self.stopped[0], [])
        self.assertEqual(self.stopped[1], [4001, 4002])
        self.assertEqual(len(self.popen), 4)
        with open(os.path.join(self.bench, "logs", "sandbox_services.json")) as fh:
            self.assertEqual(json.load(fh), {"pids": [4003, 4004]})

    def test_success_waits_for_a_real_ping_answer(self):
        self.pinged[:] = [False, False, False, True]  # occupancy check, then two misses, then pong
        self.assertEqual(self.checkout()[1]["status"], "ready")
        self.assertEqual(self.pinged, [])

    def test_a_site_that_never_answers_stops_the_services_and_reports_failure(self):
        ticks = iter(range(0, 1000, 2))
        with patch.object(srv, "_ping", return_value=False), patch.object(srv.time, "monotonic", side_effect=lambda: next(ticks)):
            _status, result = self.checkout()
        self.assertEqual((result["status"], result["step"]), ("failed", "start_services"))
        self.assertIn("ping", result["error"])
        self.assertEqual(self.stopped[-1], [4001, 4002])

    def test_a_service_that_cannot_start_fails_and_an_occupied_port_is_refused(self):
        with patch.object(srv, "_spawn", side_effect=FileNotFoundError("bench")):
            self.assertEqual(self.checkout()[1]["step"], "start_services")
        self.pinged[:] = [True]
        _status, result = self.checkout()
        self.assertIn("already answering", result["error"])
        self.assertEqual(self.popen, [])


class TestPing(unittest.TestCase):
    def test_ping_accepts_only_a_pong_answer_from_port_8000(self):
        for body, status, expected in ((b'{"message": "pong"}', 200, True), (b'{"message": "nope"}', 200, False), (b"<html>", 200, False), (b"{}", 502, False)):
            response = MagicMock(status=status)
            response.read.return_value = body
            response.__enter__ = lambda s: s
            response.__exit__ = lambda *a: False
            with patch("urllib.request.urlopen", return_value=response) as opened:
                self.assertIs(srv._ping(), expected)
                self.assertEqual(opened.call_args.args[0], "http://127.0.0.1:8000/api/method/ping")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            self.assertFalse(srv._ping())


class TestNoWritesToGit(TestingAgentCase):
    def test_neither_command_ever_commits_pushes_or_merges(self):
        self.prepare()
        self._repoint_pr("spiff/src/App.vue", "x\n")
        self.checkout()
        verbs = {a[1] for a, _ in self.calls if a[0] == "git"}
        self.assertFalse(verbs & {"commit", "push", "merge", "reset", "rebase", "tag"}, verbs)
        self.assertEqual(git(self.origin, "rev-parse", "staging"), self.base_sha)

    def test_the_new_kinds_are_dispatched_and_the_existing_ones_still_are(self):
        with patch.object(srv, "prepare_site", return_value={"status": "prepared"}) as prep, patch.object(srv, "checkout_pr_head", return_value={"status": "ready"}) as head:
            self.assertEqual(self.session.execute(self._command(1, "prepare_site", run_id="r"))[1]["status"], "prepared")
            self.assertEqual(self.session.execute(self._command(2, "checkout_pr_head", run_id="r"))[1]["status"], "ready")
        self.assertEqual((prep.call_args.args[0], head.call_args.args[0]), ("one_bpmn", "one_bpmn"))
        with patch.object(srv, "_tests_for_tree", return_value=(True, "", "", [])):
            self.assertEqual(self.session.execute(self._command(3, "run_tests", "run_tests", args={}))[1]["status"], "tests_passed")

    def test_an_exception_inside_a_new_command_is_still_an_error_result(self):
        with patch.object(srv, "prepare_site", side_effect=RuntimeError("boom")):
            status, result = self.session.execute(self._command(1, "prepare_site"))
        self.assertEqual((status, result["error"]), ("error", "RuntimeError: boom"))


class TestTheClient(unittest.TestCase):
    def _response(self, payload):
        response = MagicMock()
        response.read.return_value = json.dumps(payload).encode()
        response.__enter__ = lambda s: s
        response.__exit__ = lambda *a: False
        return response

    def test_a_call_is_signed_with_the_session_key_and_unwraps_the_message(self):
        client = agent.Processa("https://processa.example/", "SESS-1")
        client.key = "secret"
        with patch.object(agent.urllib.request, "urlopen", return_value=self._response({"message": {"accepted": True, "command": None}})) as urlopen:
            reply = client.call("next_command", {"x": 1})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://processa.example/api/method/one_bpmn.api.sandbox_session.next_command")
        body = json.loads(request.data)
        self.assertEqual((body["session"], body["x"]), ("SESS-1", 1))
        self.assertTrue(body["ts"])
        self.assertEqual(request.get_header("X-signature"), hmac.new(b"secret", request.data, hashlib.sha256).hexdigest())
        self.assertEqual(reply, {"accepted": True, "command": None})

    def test_registration_is_not_signed_because_there_is_no_key_yet(self):
        client = agent.Processa("https://p.example", "S")
        with patch.object(agent.urllib.request, "urlopen", return_value=self._response({"accepted": True})) as urlopen:
            client.call("register", {"token": "t"})
        self.assertIsNone(urlopen.call_args.args[0].get_header("X-signature"))

    def test_a_server_error_is_retried_and_a_client_error_is_not(self):
        client = agent.Processa("https://p.example", "S")
        boom = urllib.error.HTTPError("u", 502, "bad", {}, io.BytesIO())
        with patch.object(agent.time, "sleep"), patch.object(
            agent.urllib.request, "urlopen", side_effect=[boom, self._response({"ok": 1})]
        ) as urlopen:
            self.assertEqual(client.call("next_command", {}), {"ok": 1})
        self.assertEqual(urlopen.call_count, 2)
        refused = urllib.error.HTTPError("u", 403, "no", {}, io.BytesIO())
        with patch.object(agent.urllib.request, "urlopen", side_effect=refused) as urlopen, self.assertRaises(urllib.error.HTTPError):
            client.call("next_command", {})
        self.assertEqual(urlopen.call_count, 1)

    def test_a_connection_that_drops_for_a_while_is_waited_out(self):
        client = agent.Processa("https://p.example", "S")
        down = urllib.error.URLError("down")
        with patch.object(agent.time, "sleep") as sleep, patch.object(
            agent.urllib.request, "urlopen", side_effect=[down] * 6 + [self._response({"ok": 1})]
        ):
            self.assertEqual(client.call("next_command", {}), {"ok": 1})
        self.assertGreater(sum(call.args[0] for call in sleep.call_args_list), 30)

    def test_a_tunnel_that_answers_404_while_offline_is_waited_out(self):
        client = agent.Processa("https://p.example", "S")
        offline = urllib.error.HTTPError("u", 404, "not found", {}, io.BytesIO())
        with patch.object(agent.time, "sleep"), patch.object(
            agent.urllib.request, "urlopen", side_effect=[offline] * 3 + [self._response({"ok": 1})]
        ) as urlopen:
            self.assertEqual(client.call("next_command", {}), {"ok": 1})
        self.assertEqual(urlopen.call_count, 4)

    def test_a_network_failure_that_never_clears_raises_after_the_tries(self):
        client = agent.Processa("https://p.example", "S")
        with patch.object(agent.time, "sleep"), patch.object(
            agent.urllib.request, "urlopen", side_effect=urllib.error.URLError("down")
        ) as urlopen, self.assertRaises(urllib.error.URLError):
            client.call("next_command", {})
        self.assertEqual(urlopen.call_count, agent.CALL_TRIES)

    def test_the_address_must_be_https_outside_a_local_run_and_on_the_pinned_host(self):
        with patch.object(srv, "SANDBOX_ENV", "production"):
            with self.assertRaises(ValueError):
                agent.check_processa_url("http://p.example")
            agent.check_processa_url("https://p.example")
            with patch.dict(os.environ, {"ALLOWED_PROCESSA_HOST": "ba.example"}):
                with self.assertRaises(ValueError):
                    agent.check_processa_url("https://other.example")
                agent.check_processa_url("https://ba.example/x")
        with patch.object(srv, "SANDBOX_ENV", "local"):
            agent.check_processa_url("http://localhost:8000")

    def test_several_pinned_hosts_each_pass_and_any_other_is_refused(self):
        with patch.object(srv, "SANDBOX_ENV", "production"), patch.dict(
            os.environ, {"ALLOWED_PROCESSA_HOST": "staging.example, ba.example,prod.example"}
        ):
            for host in ("staging.example", "ba.example", "prod.example"):
                agent.check_processa_url(f"https://{host}/x")
            with self.assertRaises(ValueError):
                agent.check_processa_url("https://other.example")


class TestMain(unittest.TestCase):
    def test_missing_settings_end_it_with_a_usage_error(self):
        with patch.dict(os.environ, {"SESSION_ID": "", "SESSION_TOKEN": "", "PROCESSA_URL": ""}):
            self.assertEqual(agent.main(), 2)

    def test_a_refused_registration_ends_it_without_touching_any_checkout(self):
        env = {"SESSION_ID": "S", "SESSION_TOKEN": "t", "PROCESSA_URL": "http://localhost:8000"}
        with patch.dict(os.environ, env), patch.object(agent.Processa, "call", return_value={"accepted": False}), patch.object(
            agent.Session, "prepare"
        ) as prepare:
            self.assertEqual(agent.main(), 1)
        prepare.assert_not_called()

    def test_a_registered_session_is_served_to_the_end(self):
        env = {"SESSION_ID": "S", "SESSION_TOKEN": "t", "PROCESSA_URL": "http://localhost:8000"}
        with patch.dict(os.environ, env), patch.object(agent.Processa, "call", return_value=SETTINGS), patch.object(
            agent.Session, "prepare"
        ), patch.object(agent.Session, "run", return_value="closed"):
            self.assertEqual(agent.main(), 0)

    def test_a_session_processa_stops_accepting_ends_with_a_failure_code(self):
        env = {"SESSION_ID": "S", "SESSION_TOKEN": "t", "PROCESSA_URL": "http://localhost:8000"}
        with patch.dict(os.environ, env), patch.object(agent.Processa, "call", return_value=SETTINGS), patch.object(
            agent.Session, "prepare"
        ), patch.object(agent.Session, "run", side_effect=agent.SessionRefused("lost")):
            self.assertEqual(agent.main(), 1)


if __name__ == "__main__":
    unittest.main()
