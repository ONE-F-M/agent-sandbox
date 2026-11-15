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
import urllib.parse
from unittest.mock import MagicMock, patch

import dev_agent_server as srv
import session_agent as agent

SETTINGS = {
    "accepted": True, "session_key": "k" * 32, "work_item": "WI-1", "attempt": 1, "target_app": "processa",
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
        for name, text in (("processa/a.py", "one two\n"), ("processa/b.py", "three\n")):
            path = os.path.join(seed, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(text)
        git(seed, "add", "-A")
        git(seed, "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q", "-m", "seed")
        git(seed, "push", "-q", origin, "staging")
        self.bench = os.path.join(self.root, "bench")
        self.app = os.path.join(self.bench, "apps", "processa")
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
        session.execute(self._command(1, "tool", "edit_file", args={"path": "processa/a.py", "old_string": "one", "new_string": "1"}))
        session.checkpoint()
        git(self.app, "checkout", "-q", "staging")
        resumed, _ = self._session()
        with open(os.path.join(self.app, "processa/a.py")) as fh:
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
        status, result = session.execute(self._command(1, "tool", "read_file", args={"path": "processa/a.py"}))
        self.assertEqual((status, result["content"]), ("ok", "one two\n"))
        self.assertIn("tool", result["timings_ms"])
        self.assertFalse(session.unpushed)

    def test_an_edit_is_committed_locally_and_not_pushed_until_a_checkpoint(self):
        session, _ = self._session()
        status, result = session.execute(self._command(1, "tool", "edit_file", args={"path": "processa/a.py", "old_string": "two", "new_string": "2"}))
        self.assertEqual((status, result["edited"]), ("ok", True))
        self.assertIn("edit_file via Dev Agent", git(self.app, "log", "-1", "--format=%s"))
        self.assertTrue(session.unpushed)
        self.assertEqual(self._pushed("processa/a.py"), "one two")
        session.checkpoint()
        self.assertEqual(self._pushed("processa/a.py"), "one 2")
        self.assertFalse(session.unpushed)

    def test_several_edits_make_one_commit(self):
        session, _ = self._session()
        before = git(self.app, "rev-list", "--count", "HEAD")
        edits = [{"path": "processa/a.py", "old_string": "one", "new_string": "1"}, {"path": "processa/b.py", "old_string": "three", "new_string": "3"}]
        session.execute(self._command(1, "tool", "apply_edits", args={"edits": edits}))
        self.assertEqual(int(git(self.app, "rev-list", "--count", "HEAD")) - int(before), 1)
        self.assertEqual(sorted(git(self.app, "show", "--name-only", "--format=", "HEAD").split()), ["processa/a.py", "processa/b.py"])

    def test_a_write_and_a_delete_are_committed(self):
        session, _ = self._session()
        session.execute(self._command(1, "tool", "write_file", args={"path": "processa/new.py", "content": "x\n"}))
        session.execute(self._command(2, "tool", "delete_file", args={"path": "processa/b.py"}))
        session.checkpoint()
        self.assertEqual(self._pushed("processa/new.py"), "x")
        self.assertNotIn("processa/b.py", git(self.origin, "ls-tree", "-r", "--name-only", "WI-1"))

    def test_a_failed_edit_is_an_error_result_with_no_commit(self):
        session, _ = self._session()
        status, result = session.execute(self._command(1, "tool", "edit_file", args={"path": "processa/a.py", "old_string": "absent", "new_string": "x"}))
        self.assertEqual(status, "error")
        self.assertIn("not found", result["error"])
        self.assertFalse(session.unpushed)

    def test_a_search_and_a_listing_work_on_the_checkout(self):
        session, _ = self._session()
        _, found = session.execute(self._command(1, "tool", "grep_files", args={"pattern": "three", "literal": True}))
        self.assertEqual([m["path"] for m in found["matches"]], ["processa/b.py"])
        _, listed = session.execute(self._command(2, "tool", "list_files", args={"path_prefix": "processa"}))
        self.assertEqual(sorted(listed["files"]), ["processa/a.py", "processa/b.py"])

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
        session.execute(self._command(1, "tool", "edit_file", args={"path": "processa/a.py", "old_string": "two", "new_string": "2"}))
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
        tests.assert_called_once_with("processa", "staging")
        self.assertEqual(result["commit"], git(self.app, "rev-parse", "HEAD"))
        self.assertEqual(self._pushed("processa/a.py"), "one 2")
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
        session.execute(self._command(3, "tool", "write_file", args={"path": "processa/processa/doctype/x/x.json", "content": "{}\n"}))
        with patch.object(srv, "_tests_for_tree", return_value=(True, "", "", [])), patch.object(srv, "_migrate_site", return_value=(True, "", "")) as migrate:
            session.execute(self._command(4, "run_tests", "run_tests", args={}))
        migrate.assert_called_once()

    def test_a_failed_migration_is_reported_and_no_tests_run(self):
        session, _ = self._edited()
        session.execute(self._command(3, "tool", "write_file", args={"path": "processa/patches/p.py", "content": "x\n"}))
        with patch.object(srv, "_migrate_site", return_value=(False, "out", "boom")), patch.object(srv, "_tests_for_tree") as tests:
            _status, result = session.execute(self._command(4, "run_tests", "run_tests", args={}))
        self.assertEqual((result["status"], result["error"]), ("failed", "boom"))
        tests.assert_not_called()

    def test_finalize_pushes_then_opens_the_pull_request_and_returns_its_url(self):
        session, _ = self._edited()
        with patch.object(srv, "_tool_open_pull_request", return_value={"pr_url": "https://github.com/o/r/pull/9"}) as open_pr:
            status, result = session.execute(self._command(2, "finalize", "open_pull_request", args={"summary": "Did it."}, work_item_description="Fix it."))
        self.assertEqual((status, result["status"], result["pr_url"]), ("ok", "tests_passed", "https://github.com/o/r/pull/9"))
        self.assertEqual(self._pushed("processa/a.py"), "one 2")
        target_app, args, run_ctx = open_pr.call_args.args
        self.assertEqual((target_app, args), ("processa", {"summary": "Did it."}))
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
        read = self._command(1, "tool", "read_file", args={"path": "processa/a.py"})
        edit = self._command(2, "tool", "edit_file", args={"path": "processa/a.py", "old_string": "one", "new_string": "1"})
        session, client = self._session([read, edit, self._command(3, "close", "close")])
        self.assertEqual(session.run(), "closed")
        results = client.results()
        self.assertEqual([r["seq"] for r in results], [1, 2, 3])
        self.assertEqual([r["status"] for r in results], ["ok", "ok", "ok"])
        self.assertEqual(self._pushed("processa/a.py"), "1 two")

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
        session.execute(self._command(1, "tool", "write_file", args={"path": "processa/n.py", "content": "x\n"}))
        session.last_push -= agent.CHECKPOINT_SECONDS + 1
        calls = iter([{"accepted": True, "command": None}, {"accepted": True, "command": {"seq": 2, "kind": "close", "action": "close", "payload": {}}}])
        session.client.call = lambda endpoint, body: next(calls) if endpoint == "next_command" else {"accepted": True}
        with patch.object(agent.time, "sleep"):
            session.run()
        self.assertEqual(self._pushed("processa/n.py"), "x")

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
        for target, value in (("SNAPSHOT_DIR", self.snapshots), ("SPECS_DIR", os.path.join(self.root, "specs")), ("READY_TIMEOUT_SECONDS", 4)):
            patcher = patch.object(srv, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        sites = os.path.join(self.bench, "sites")
        os.makedirs(os.path.join(sites, "sandbox"))
        for path, conf in (("common_site_config.json", {"db_host": "127.0.0.1"}), ("sandbox/site_config.json", {"db_name": "_db1", "db_password": "dbsecret"})):
            with open(os.path.join(sites, path), "w") as fh:
                json.dump(conf, fh)
        self.calls, self.broken, self.dump_bytes = [], set(), "-- dump\n"
        self.timeouts, self.report, self.npx_rc, self.restored, self.scheduled = {}, None, 0, None, {"processa.tasks.daily"}
        self.gh_rc, self.gh_out = 0, "https://github.com/ONE-F-M/one_bpmn/pull/5#issuecomment-99\n"
        self.yarn_check_rc, self.yarn_install_rc, self.vite_rc, self.npx_out = 1, 0, 0, ""
        self.dist_text = "const api = 'http://127.0.0.1:8000'; const route = '/home';\n"
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

    def _fake_exec(self, args, env=None, cwd=None, timeout=None, stdin=None):
        self.calls.append((list(args), dict(env or {})))
        self.timeouts[args[0]] = timeout
        done = lambda out="", rc=0, err="": subprocess.CompletedProcess(args, rc, out, err)
        if args[0] == "git":
            return subprocess.run(args, cwd=cwd, capture_output=True, text=True)
        if args[0] in self.broken:
            return done("out-line", 1, f"{args[0]} blew up {env and env.get('TEST_PASSWORD')}")
        if args[0] == "gh":
            return done(self.gh_out, self.gh_rc, "gh refused" if self.gh_rc else "")
        if args[0] == "yarn":
            if args[1] == "check":
                return done("", self.yarn_check_rc)
            if args[1] == "install":
                with open(os.path.join(cwd, "yarn.lock"), "a") as fh:
                    fh.write("changed by yarn install\n")
                return done("", self.yarn_install_rc, "install failed" if self.yarn_install_rc else "")
            if args[1:3] == ["vite", "build"]:
                if self.vite_rc:
                    return done("", self.vite_rc, "build failed")
                os.makedirs(os.path.join(cwd, "dist", "assets"), exist_ok=True)
                with open(os.path.join(cwd, "dist", "assets", "app.js"), "w") as fh:
                    fh.write(self.dist_text)
                return done("built")
        if args[0] == "npx":
            out = env["SPEC_OUTPUT_DIR"]
            os.makedirs(out, exist_ok=True)
            if self.report is not None:
                with open(os.path.join(out, "report.json"), "w") as fh:
                    json.dump(self.report, fh)
            return done(self.npx_out, self.npx_rc)
        if args[0] == "mysql":
            self.restored = stdin.read()
            return done()
        if args[0] == "node":
            return done("noise\nSNAPSHOT_JSON=" + json.dumps({"snapshot": "- heading 'Home'", "screenshot_base64": "abc"}))
        if args[0].endswith("python") and "JOB_METHOD" in (env or {}):
            ok = env["JOB_METHOD"] in self.scheduled
            return done("JOB_JSON=" + json.dumps({"ran": True} if ok else {"refused": True}))
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
               {"base_branch": "-x"}, {"roles": []}, {"roles": 5}, {"roles": ["ok", "bad;role"]}, {"password": "short"},
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
        self.assertEqual(sorted(os.listdir(self.snapshots)), ["run-1.json", "run-1.pw", "run-1.sql", "run-2.json", "run-2.pw", "run-2.sql"])
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
        for path in ("frontend/src/App.vue", "processa/public/js/x.js"):
            self.calls.clear()
            self.pinged[:] = [False, True]
            self._repoint_pr(path, "x\n")
            _status, result = self.checkout()
            self.assertEqual(self.argv("bench", "build"), [["bench", "build", "--app", "processa"]], path)
            self.assertTrue(result["built_assets"])
        for path in ("processa/api/x.py", "docs/public.md", "processa/publicity/x.js", "frontend-old/x.js"):
            self.calls.clear()
            self.pinged[:] = [False, True]
            self._repoint_pr(path, "x\n")
            self.assertFalse(self.checkout()[1]["built_assets"], path)
            self.assertEqual(self.argv("bench", "build"), [], path)

    def test_a_failing_build_stops_before_migrate(self):
        self._repoint_pr("frontend/src/App.vue", "x\n")
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
        self._repoint_pr("frontend/src/App.vue", "x\n")
        self.checkout()
        verbs = {a[1] for a, _ in self.calls if a[0] == "git"}
        self.assertFalse(verbs & {"commit", "push", "merge", "reset", "rebase", "tag"}, verbs)
        self.assertEqual(git(self.origin, "rev-parse", "staging"), self.base_sha)

    def test_the_new_kinds_are_dispatched_and_the_existing_ones_still_are(self):
        with patch.object(srv, "prepare_site", return_value={"status": "prepared"}) as prep, patch.object(srv, "checkout_pr_head", return_value={"status": "ready"}) as head:
            self.assertEqual(self.session.execute(self._command(1, "prepare_site", run_id="r"))[1]["status"], "prepared")
            self.assertEqual(self.session.execute(self._command(2, "checkout_pr_head", run_id="r"))[1]["status"], "ready")
        self.assertEqual((prep.call_args.args[0], head.call_args.args[0]), ("processa", "processa"))
        with patch.object(srv, "_tests_for_tree", return_value=(True, "", "", [])):
            self.assertEqual(self.session.execute(self._command(3, "run_tests", "run_tests", args={}))[1]["status"], "tests_passed")

    def test_the_arguments_are_read_from_payload_args_as_processa_sends_them(self):
        sent = {"args": {"run_id": "r", "pr_number": "5"}, "work_item_description": "Test it."}
        with patch.object(srv, "checkout_pr_head", return_value={"status": "ready"}) as head:
            self.session.execute({"seq": 1, "kind": "checkout_pr_head", "action": "checkout_pr_head", "payload": sent})
        self.assertEqual(head.call_args.args[1], {"run_id": "r", "pr_number": "5"})

    def test_roles_from_a_text_field_are_split_into_one_user_each(self):
        _status, result = self.prepare(roles="Sales User\nHR Manager, Sales User\n")
        self.assertEqual(result["status"], "prepared")
        self.assertEqual([u["role"] for u in result["users"]], ["Sales User", "HR Manager", None])
        self.assertEqual(self.prepare(roles=" , \n")[1]["step"], "validate")

    def test_an_exception_inside_a_new_command_is_still_an_error_result(self):
        with patch.object(srv, "prepare_site", side_effect=RuntimeError("boom")):
            status, result = self.session.execute(self._command(1, "prepare_site"))
        self.assertEqual((status, result["error"]), ("error", "RuntimeError: boom"))


def pw_report(*specs):
    """A Playwright JSON report. Each spec is (file, title, project, status, [(result status, error, video)])."""
    suites = []
    for file, title, project, status, results in specs:
        runs = [{"status": st, "duration": 500, "error": {"message": f"\x1b[31m{err}\x1b[0m\nmore"} if err else None,
                 "attachments": [{"name": "video", "path": video}] if video else []} for st, err, video in results]
        runs = [{k: v for k, v in r.items() if v} | {"duration": 500} for r in runs]
        suites.append({"file": file, "suites": [], "specs": [{"title": title, "file": file, "tests": [
            {"projectName": project, "status": status, "results": runs}]}]})
    return {"suites": suites}


class TestSpecs(TestingAgentCase):
    def setUp(self):
        super().setUp()
        self.prepare()
        self.checkout()
        self.calls.clear()
        self.run_dir = os.path.join(self.root, "specs", "run-1")

    def write(self, name="ac1.spec.js", run_id="run-1", content="test('x', async () => {});\n"):
        return srv.write_spec("processa", {"run_id": run_id, "file": name, "content": content})

    def test_write_spec_writes_under_the_run_folder_and_never_touches_the_checkout_or_git(self):
        head, remote = git(self.app, "rev-parse", "HEAD"), git(self.origin, "rev-parse", "staging")
        status, result = self.session.execute(self._command(5, "tool", "write_spec", args={"run_id": "run-1", "file": "ac1.spec.js", "content": "// spec\n"}))
        self.assertEqual((status, result["written"]), ("ok", "run-1/ac1.spec.js"))
        self.assertEqual(open(os.path.join(self.run_dir, "ac1.spec.js")).read(), "// spec\n")
        self.assertFalse(os.path.exists(os.path.join(self.app, "run-1")))
        self.assertEqual((git(self.app, "rev-parse", "HEAD"), git(self.origin, "rev-parse", "staging")), (head, remote))
        self.assertFalse(self.session.unpushed)
        self.assertEqual([a for a, _ in self.calls if a[0] == "git"], [])

    def test_write_spec_refuses_a_bad_run_a_path_a_non_spec_and_oversize_content(self):
        for over in ({"run_id": "../x"}, {"run_id": "a/b"}, {"name": "../ac1.spec.js"}, {"name": "sub/ac1.spec.js"}, {"name": "ac1.js"},
                     {"name": "playwright.config.js"}, {"name": ".spec.js"}, {"content": ""}, {"content": "x" * 200_000}):
            args = {"name": "ac1.spec.js", "run_id": "run-1", "content": "t\n", **over}
            result = self.write(name=args["name"], run_id=args["run_id"], content=args["content"])
            self.assertIn("error", result, over)
        self.assertFalse(os.path.exists(os.path.join(self.root, "specs", "x")))
        self.assertEqual(sorted(os.listdir(self.run_dir)) if os.path.isdir(self.run_dir) else [], [])

    def test_run_specs_needs_a_prepared_head_site_and_specs_and_never_returns_an_empty_pass(self):
        for run_id, ping in (("never-prepared", True), ("run-1", False)):
            self.pinged[:] = [ping] if not ping else []
            with patch.object(srv, "_ping", return_value=ping):
                result = srv.run_specs("processa", {"run_id": run_id})
            self.assertEqual((result["status"], result["step"]), ("failed", "not_prepared"))
            self.assertIn("prepare the site first", result["error"])
        self.pinged.clear()
        result = srv.run_specs("processa", {"run_id": "run-1"})
        self.assertEqual(result["step"], "no_specs")
        self.assertEqual(self.argv("npx"), [])

    def test_specs_run_in_chromium_with_the_fixed_config_and_report_json_per_spec(self):
        self.write("ac1.spec.js"), self.write("ac2.spec.js")
        self.report = pw_report(
            ("ac1.spec.js", "AC1 creates a user @ui", "desktop", "expected", [("passed", None, "/o/v1.webm")]),
            ("ac1.spec.js", "AC1 creates a user @ui", "mobile", "expected", [("passed", None, "/o/v1m.webm")]),
            ("ac2.spec.js", "AC2 starts the site", "desktop", "unexpected", [("failed", "expect(received).toBe(1)", "/o/v2.webm"), ("failed", "expect(received).toBe(1)", None)]),
        )
        status, result = self.session.execute(self._command(6, "run_specs", "run_specs", args={"run_id": "run-1"}))
        self.assertEqual((status, result["status"], result["code"]), ("ok", "specs_run", "head"))
        by = {(r["file"], r["viewport"]): r for r in result["results"]}
        self.assertEqual(set(by), {("ac1.spec.js", "desktop"), ("ac1.spec.js", "mobile"), ("ac2.spec.js", "desktop")})
        failed = by[("ac2.spec.js", "desktop")]
        self.assertEqual((failed["status"], failed["criterion"], failed["video"], failed["duration_ms"]), ("failed", "AC2", "/o/v2.webm", 1000))
        self.assertEqual(failed["failing_assertion"], "expect(received).toBe(1)\nmore")  # colour codes stripped
        self.assertEqual(by[("ac1.spec.js", "mobile")]["criterion"], "AC1")
        self.assertEqual(result["summary"], {"passed": 2, "failed": 1})
        config = open(os.path.join(self.run_dir, "playwright.config.js")).read()
        for needle in ("width: 1280, height: 720", "width: 390, height: 844", "grep: /@ui/", "retries: 1", "mode: 'on'", "trace: 'off'"):
            self.assertIn(needle, config)
        self.assertNotIn("trace: 'on'", config)

    def test_a_spec_that_fails_once_and_passes_on_the_rerun_is_flaky(self):
        self.write()
        self.report = pw_report(("ac1.spec.js", "AC1 flaky", "desktop", "flaky", [("failed", "timed out", "/o/a.webm"), ("passed", None, "/o/b.webm")]))
        result = srv.run_specs("processa", {"run_id": "run-1"})
        self.assertEqual(result["results"][0]["status"], "flaky")
        self.assertEqual(result["results"][0]["video"], "/o/b.webm")
        self.assertEqual(result["summary"], {"flaky": 1})

    def test_the_password_reaches_playwright_only_through_the_environment(self):
        self.write()
        self.report = pw_report(("ac1.spec.js", "AC1", "desktop", "expected", [("passed", None, None)]))
        srv.run_specs("processa", {"run_id": "run-1"})
        args, env = self.argv("npx")[0], next(e for a, e in self.calls if a[0] == "npx")
        self.assertEqual(env["TEST_PASSWORD"], PASSWORD)
        self.assertNotIn(PASSWORD, " ".join(args))
        self.assertNotIn(PASSWORD, json.dumps(self.client.sent))
        self.assertEqual(oct(os.stat(os.path.join(self.snapshots, "run-1.pw")).st_mode & 0o777), "0o600")

    def test_a_crashed_run_is_reported_as_failed_with_its_output_and_without_the_password(self):
        self.write()
        self.report, self.npx_rc = None, 2
        result = srv.run_specs("processa", {"run_id": "run-1"})
        self.assertEqual((result["status"], result["step"]), ("failed", "run"))
        self.assertNotIn(PASSWORD, json.dumps(result))

    def test_run_specs_on_base_restores_the_snapshot_checks_out_base_restarts_and_runs_only_changed_behavior(self):
        self.write()
        self.report = pw_report(("ac1.spec.js", "AC1 @changed-behavior", "desktop", "unexpected", [("failed", "no user", None)]))
        self.stopped.clear()
        self.popen.clear()
        self.pinged[:] = [False, True]  # the port is free after the stop, then the restarted site answers
        result = srv.run_specs_on_base("processa", {"run_id": "run-1"})
        self.assertEqual((result["status"], result["code"], result["base_sha"]), ("specs_run", "base", self.base_sha))
        self.assertEqual(self.restored, "-- dump\n")
        self.assertEqual(git(self.app, "rev-parse", "HEAD"), self.base_sha)
        self.assertEqual(self.stopped[-1], [4001, 4002])  # the head's server and worker, stopped before the restore
        self.assertEqual([a for a, _ in self.popen][0], ["bench", "--site", "sandbox", "serve", "--port", "8000"])
        npx = self.argv("npx")[0]
        self.assertEqual(npx[-2:], ["--grep", "@changed-behavior"])
        self.assertEqual(self.argv("bench", "build"), [])  # the head built no assets, so the base needs none
        order = [a[0] for a, _ in self.calls if a[0] in ("mysql", "git", "npx")]
        self.assertLess(order.index("mysql"), order.index("npx"))
        _, env = next((a, e) for a, e in self.calls if a[0] == "mysql")
        self.assertNotIn("dbsecret", " ".join(next(a for a, _ in self.calls if a[0] == "mysql")))
        self.assertEqual(env["MYSQL_PWD"], "dbsecret")

    def test_the_base_is_rebuilt_when_the_head_built_assets_and_a_failed_restore_stops_everything(self):
        self.write()
        marker = os.path.join(self.snapshots, "run-1.json")
        data = json.load(open(marker))
        json.dump({**data, "built_assets": True}, open(marker, "w"))
        self.report = pw_report(("ac1.spec.js", "AC1", "desktop", "expected", [("passed", None, None)]))
        self.pinged[:] = [False, True]
        srv.run_specs_on_base("processa", {"run_id": "run-1"})
        self.assertEqual(self.argv("bench", "build"), [["bench", "build", "--app", "processa"]])
        self.calls.clear()
        self.broken.add("mysql")
        self.pinged[:] = [False, True]
        result = srv.run_specs_on_base("processa", {"run_id": "run-1"})
        self.assertEqual((result["status"], result["step"]), ("failed", "restore"))
        self.assertEqual([a for a, _ in self.calls if a[0] in ("npx",) or a[:2] == ["git", "checkout"]], [])
        self.assertNotIn(PASSWORD, json.dumps(result))

    def test_run_specs_on_base_before_the_head_was_checked_out_says_prepare_first(self):
        marker = os.path.join(self.snapshots, "run-1.json")
        data = json.load(open(marker))
        data.pop("head_sha")
        json.dump(data, open(marker, "w"))
        self.assertEqual(srv.run_specs_on_base("processa", {"run_id": "run-1"})["step"], "not_prepared")
        self.assertEqual(srv.run_specs_on_base("processa", {"run_id": "nope"})["step"], "not_prepared")

    def test_browser_snapshot_returns_the_snapshot_and_a_screenshot_for_a_role_user_within_60_seconds(self):
        result = srv.browser_snapshot("processa", {"run_id": "run-1", "url": "/app/home", "role": "Sales User"})
        self.assertEqual((result["snapshot"], result["screenshot_base64"]), ("- heading 'Home'", "abc"))
        args, env = self.argv("node")[0], next(e for a, e in self.calls if a[0] == "node")
        self.assertEqual((env["SNAP_USER"], env["SNAP_URL"], env["SNAP_PASSWORD"]), ("sales-user.run-1@sandbox.test", "/app/home", PASSWORD))
        self.assertNotIn(PASSWORD, " ".join(args))
        self.assertEqual(self.timeouts["node"], 60)

    def test_browser_snapshot_refuses_other_hosts_unknown_roles_and_an_unprepared_run(self):
        for url in ("https://evil.example/x", "//evil.example/x", "http://127.0.0.1:8001/x", "javascript:alert(1)", "app/home", None):
            self.assertIn("error", srv.browser_snapshot("processa", {"run_id": "run-1", "url": url, "role": "Sales User"}), url)
        self.assertIn("http://127.0.0.1:8000/app/x", srv.browser_snapshot("processa", {"run_id": "run-1", "url": "http://127.0.0.1:8000/app/x", "role": "HR Manager"}).get("url", ""))
        self.assertIn("no test user", srv.browser_snapshot("processa", {"run_id": "run-1", "url": "/app", "role": "Ghost"})["error"])
        self.assertIn("prepare the site first", srv.browser_snapshot("processa", {"run_id": "never", "url": "/app", "role": "Sales User"})["error"])

    def test_trigger_job_runs_a_scheduled_method_and_refuses_any_other(self):
        self.assertEqual(srv.trigger_job("processa", {"method": "processa.tasks.daily"}), {"ran": "processa.tasks.daily"})
        refused = srv.trigger_job("processa", {"method": "frappe.client.delete"})
        self.assertIn("scheduler_events", refused["error"])
        self.calls.clear()
        for bad in ("rm -rf /", "a", "a..b", "", None, "x.y; import os"):
            self.assertIn("error", srv.trigger_job("processa", {"method": bad}), bad)
        self.assertEqual(self.calls, [])  # a malformed method never reaches bench

    def test_the_scheduler_check_reads_every_kind_of_scheduler_event_of_the_target_app(self):
        fake = MagicMock()
        fake.get_hooks.return_value = {"daily": ["pkg.mod.daily"], "cron": {"* * * * *": ["pkg.mod.minutely"]}, "all": ("pkg.mod.all",)}
        ran = []
        fake.get_attr.side_effect = lambda m: (lambda: ran.append(m))
        out = []
        for method in ("pkg.mod.daily", "pkg.mod.minutely", "pkg.mod.all", "pkg.mod.other"):
            env = {"SANDBOX_SITE": "sandbox", "JOB_APP": "pkg", "JOB_METHOD": method}
            with patch.dict("sys.modules", {"frappe": fake}), patch.dict(os.environ, env), patch("builtins.print") as printed:
                exec(compile(srv._TRIGGER_SCRIPT, "script", "exec"), {})
            out.append(json.loads(printed.call_args.args[0].split("=", 1)[1]))
        self.assertEqual(out, [{"ran": True}] * 3 + [{"refused": True}])
        self.assertEqual(ran, ["pkg.mod.daily", "pkg.mod.minutely", "pkg.mod.all"])
        fake.get_hooks.assert_called_with("scheduler_events", app_name="pkg")

    def test_prepare_site_keeps_the_users_and_a_private_password_file_and_checkout_records_the_head(self):
        marker = json.load(open(os.path.join(self.snapshots, "run-1.json")))
        self.assertEqual([u["role"] for u in marker["users"]], ["Sales User", "HR Manager", None])
        self.assertNotIn(PASSWORD, json.dumps(marker))
        self.assertEqual((marker["head_sha"], marker["built_assets"]), (self.pr_sha, False))
        self.assertEqual(open(os.path.join(self.snapshots, "run-1.pw")).read(), PASSWORD)

    def test_running_the_base_does_not_delete_the_heads_output_or_results(self):
        self.write()
        self.report = pw_report(("ac1.spec.js", "AC1 @changed-behavior", "desktop", "expected", [("passed", None, None)]))
        srv.run_specs("processa", {"run_id": "run-1"})
        head_report = os.path.join(self.run_dir, "out-head", "report.json")
        self.assertTrue(os.path.isfile(head_report))
        self.assertEqual(json.load(open(os.path.join(self.run_dir, "results-head.json")))[0]["criterion"], "AC1")
        self.pinged[:] = [False, True]
        srv.run_specs_on_base("processa", {"run_id": "run-1"})
        self.assertTrue(os.path.isfile(head_report))
        self.assertTrue(os.path.isfile(os.path.join(self.run_dir, "out-base", "report.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.run_dir, "results-base.json")))

    def _node(self, script, env=None):
        import shutil as _sh
        if not _sh.which("node"):
            self.skipTest("node is not installed here")
        os.makedirs(self.run_dir, exist_ok=True)
        with open(os.path.join(self.run_dir, "frappe-helpers.js"), "w") as fh:
            fh.write(srv._FRAPPE_HELPERS)
        return subprocess.run(["node", "-e", script], cwd=self.run_dir, capture_output=True, text=True,
                              env={**os.environ, "SPEC_BASE_URL": "http://127.0.0.1:8000", "TEST_PASSWORD": PASSWORD, **(env or {})})

    STUB_PAGE = """
        const h = require('./frappe-helpers'); const calls = [];
        const loc = (sel) => ({ first() { return this; }, filter(o) { calls.push(['filter', o.hasText]); return this; },
          async fill(v) { calls.push(['fill', sel, v]); }, async blur() { calls.push(['blur']); }, async click() { calls.push(['click', sel]); },
          async pressSequentially(t) { calls.push(['type', t]); }, async waitFor() { calls.push(['waitFor', sel]); } });
        const status = Number(process.env.LOGIN_STATUS || 200);
        const page = { request: { post: async (url, o) => { calls.push(['post', url, o.form]); return { ok: () => status === 200, status: () => status }; } },
          goto: async (u) => calls.push(['goto', u]), waitForFunction: async () => calls.push(['idle']), locator: (sel) => loc(sel),
          evaluate: async () => calls.push(['blur-active']) };
    """

    def test_the_frappe_helper_file_is_written_into_every_run_folder_and_cannot_be_replaced_by_a_spec(self):
        self.write()
        self.report = pw_report(("ac1.spec.js", "AC1", "desktop", "expected", [("passed", None, None)]))
        srv.run_specs("processa", {"run_id": "run-1"})
        helper = os.path.join(self.run_dir, "frappe-helpers.js")
        self.assertEqual(open(helper).read(), srv._FRAPPE_HELPERS)
        with open(helper, "w") as fh:
            fh.write("tampered")
        self.pinged[:] = [False, True]
        srv.run_specs_on_base("processa", {"run_id": "run-1"})
        self.assertEqual(open(helper).read(), srv._FRAPPE_HELPERS)  # rewritten for the base run too
        self.assertIn("error", self.write(name="frappe-helpers.js"))  # not a .spec file, so write_spec refuses it

    def test_the_helper_is_valid_javascript_that_loads_without_playwright_and_exports_the_documented_functions(self):
        run = self._node("const h = require('./frappe-helpers'); console.log(Object.keys(h).sort().join(','))")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "field,fillField,fillLink,loginAs,openForm,openList,saveForm,waitForDialog,waitForFrappeIdle")

    def test_login_posts_to_the_api_with_the_password_from_the_environment_and_a_refusal_never_shows_it(self):
        run = self._node(self.STUB_PAGE + "(async () => { await h.loginAs(page, 'a@b.test'); console.log(JSON.stringify(calls)); })();")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)[0], ["post", "http://127.0.0.1:8000/api/method/login", {"usr": "a@b.test", "pwd": PASSWORD}])
        refused = self._node(self.STUB_PAGE + "h.loginAs(page, 'a@b.test').catch((e) => { console.error(e.message); process.exit(3); });", {"LOGIN_STATUS": "401"})
        self.assertEqual(refused.returncode, 3)
        self.assertIn("HTTP 401", refused.stderr)
        self.assertNotIn(PASSWORD, refused.stderr + refused.stdout)

    def test_the_helpers_wait_blur_and_open_dropdowns_in_the_order_frappe_needs(self):
        script = self.STUB_PAGE + """(async () => {
            await h.openForm(page, 'Work Item'); await h.openForm(page, 'Work Item', 'WI 1'); await h.openList(page, 'Work Item');
            calls.length = 0;
            await h.fillField(page, 'title', 'x'); const afterField = calls.map((c) => c[0]).join(' ');
            calls.length = 0; await h.fillLink(page, 'epic', 'WI-1'); const afterLink = calls.map((c) => c[0]).join(' ');
            calls.length = 0; await h.saveForm(page); const afterSave = calls.map((c) => c.join(':')).join(' ');
            console.log(JSON.stringify({ afterField, afterLink, afterSave }));
        })();"""
        run = self._node(script)
        self.assertEqual(run.returncode, 0, run.stderr)
        seen = json.loads(run.stdout)
        self.assertEqual(seen["afterField"], "fill blur idle")  # fill, blur the field, then wait for Frappe
        self.assertEqual(seen["afterLink"], "click type filter waitFor click idle")  # type, wait for the dropdown, pick, settle
        self.assertTrue(seen["afterSave"].startswith("blur-active idle click:.page-actions .primary-action:visible"), seen["afterSave"])
        urls = self._node(self.STUB_PAGE + """(async () => { await h.openForm(page, 'Work Item'); await h.openForm(page, 'Work Item', 'WI 1');
            await h.openList(page, 'Work Item'); console.log(JSON.stringify(calls.filter((c) => c[0] === 'goto').map((c) => c[1]))); })();""")
        self.assertEqual(json.loads(urls.stdout), ["/app/work-item/new", "/app/work-item/WI%201", "/app/work-item"])

    def test_the_new_kinds_are_dispatched_and_nothing_is_ever_committed(self):
        with patch.object(srv, "run_specs", return_value={"status": "specs_run"}) as head, patch.object(srv, "run_specs_on_base", return_value={"status": "specs_run"}) as base:
            self.assertEqual(self.session.execute(self._command(1, "run_specs", "run_specs", args={"run_id": "r"}))[1]["status"], "specs_run")
            self.assertEqual(self.session.execute(self._command(2, "run_specs_on_base", "run_specs_on_base", args={"run_id": "r"}))[1]["status"], "specs_run")
        self.assertEqual((head.call_args.args[1], base.call_args.args[1]), ({"run_id": "r"}, {"run_id": "r"}))
        self.write(), srv.run_specs("processa", {"run_id": "run-1"})
        verbs = {a[1] for a, _ in self.calls if a[0] == "git"}
        self.assertFalse(verbs & {"commit", "push", "merge", "reset", "rebase", "tag"}, verbs)



TOKEN = "ghp_" + "x" * 36  # built at run time: not a real credential, and no credential-shaped literal in the file


class TestUploadVideos(TestingAgentCase):
    """upload_videos with a fake GitHub: the repository lookup and upload calls are recorded, the files are real."""

    def setUp(self):
        super().setUp()
        self.run_dir = os.path.join(self.root, "specs", "run-1")
        self.http = []
        self.replies = {}
        patcher = patch.object(srv, "_http", side_effect=self._fake_http)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake_http(self, method, url, token, data=None, headers=None, timeout=300):
        body = data.read() if hasattr(data, "read") else data
        self.http.append({"method": method, "url": url, "token": token, "body": body, "headers": headers or {}})
        for needle, reply in self.replies.items():
            if needle in url:
                return reply(len(self.http)) if callable(reply) else reply
        if url.endswith("/repos/ONE-F-M/one_bpmn"):
            return 200, {"id": 424242}
        return 200, {"url": f"https://github.com/user-attachments/assets/{len(self.http)}"}

    def make_videos(self, specs):
        """specs: (criterion, viewport, bytes or None). Writes the video files and results-head.json."""
        results = []
        for i, (criterion, viewport, size) in enumerate(specs):
            video = None
            if size is not None:
                folder = os.path.join(self.run_dir, "out-head", "artifacts", f"{criterion}-{viewport}-{i}")
                os.makedirs(folder, exist_ok=True)
                video = os.path.join(folder, "video.webm")
                with open(video, "wb") as fh:
                    fh.truncate(size) if size > 10_000 else fh.write(b"w" * size)
            results.append({"criterion": criterion, "file": f"{criterion.lower()}.spec.js", "viewport": viewport, "status": "passed", "video": video})
        os.makedirs(self.run_dir, exist_ok=True)
        with open(os.path.join(self.run_dir, "results-head.json"), "w") as fh:
            json.dump(results, fh)
        return results

    def upload(self, **over):
        args = {"run_id": "run-1", "repository": "ONE-F-M/one_bpmn", "pr_number": 5, "token": TOKEN, **over}
        return srv.upload_videos("processa", args)

    def left(self):
        return [os.path.join(r, f) for r, _d, fs in os.walk(self.run_dir) for f in fs if f.endswith(".webm")] if os.path.isdir(self.run_dir) else []

    def test_one_video_per_criterion_is_uploaded_to_the_repository_and_the_urls_come_back(self):
        self.make_videos([("AC1", "desktop", 500), ("AC1", "mobile", 600), ("AC2", "desktop", 700)])
        result = self.upload()
        self.assertEqual((result["status"], result["path"]), ("videos_uploaded", "upload_endpoint"))
        self.assertEqual([u["criterion"] for u in result["uploads"]], ["AC1", "AC2"])
        self.assertTrue(all(u["url"].startswith("https://github.com/user-attachments/assets/") for u in result["uploads"]))
        lookup, first, second = self.http
        self.assertEqual((lookup["method"], lookup["url"]), ("GET", "https://api.github.com/repos/ONE-F-M/one_bpmn"))
        self.assertTrue(first["url"].startswith("https://uploads.github.com/user-attachments/assets?"))
        for part in ("name=AC1.webm", "content_type=video%2Fwebm", "repository_id=424242"):
            self.assertIn(part, first["url"])
        self.assertEqual((first["body"], second["body"]), (b"w" * 500, b"w" * 700))  # the desktop video, not the mobile one
        self.assertEqual(first["headers"]["Content-Type"], "application/octet-stream")

    def test_the_local_videos_are_deleted_only_after_every_upload_worked(self):
        self.make_videos([("AC1", "desktop", 500), ("AC2", "desktop", 700), ("AC2", "mobile", 800)])
        self.assertEqual(len(self.left()), 3)
        result = self.upload()
        self.assertEqual((result["status"], result["deleted"]), ("videos_uploaded", 3))
        self.assertEqual(self.left(), [])

    def test_a_video_over_100_mb_or_a_failed_upload_is_reported_for_its_criterion_and_the_rest_continue(self):
        self.make_videos([("AC1", "desktop", 101 * 1024 * 1024), ("AC2", "desktop", 500), ("AC3", "desktop", 600)])
        self.replies["name=AC3"] = (500, {"message": f"boom {TOKEN}"})
        result = self.upload()
        self.assertEqual((result["status"], result["step"]), ("failed", "upload"))
        self.assertEqual([u["criterion"] for u in result["uploads"]], ["AC2"])
        errors = {e["criterion"]: e["error"] for e in result["errors"]}
        self.assertIn("over the 100 MB limit", errors["AC1"])
        self.assertIn("upload failed (500)", errors["AC3"])
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertEqual(len(self.left()), 3)  # nothing deleted: a retry still has every file
        self.assertEqual(len([h for h in self.http if h["method"] == "POST"]), 2)  # the 101 MB one was never sent

    def test_when_the_endpoint_refuses_the_token_the_machine_user_posts_the_whole_comment_with_gh(self):
        self.make_videos([("AC1", "desktop", 500), ("AC2", "desktop", 700)])
        self.replies["user-attachments"] = (404, {"message": "Not Found"})
        result = self.upload(comment_body="Acceptance test: 1 of 2 criteria passed")
        self.assertEqual((result["status"], result["path"]), ("videos_uploaded", "gh_pr_comment"))
        self.assertEqual(result["comment_url"], "https://github.com/ONE-F-M/one_bpmn/pull/5#issuecomment-99")
        args, env = next((a, e) for a, e in self.calls if a[0] == "gh")
        self.assertEqual(args[:7], ["gh", "pr", "comment", "5", "--repo", "ONE-F-M/one_bpmn", "--body"])
        self.assertEqual(args[7], "Acceptance test: 1 of 2 criteria passed")
        self.assertEqual(args.count("--attach"), 2)
        self.assertEqual(env["GH_TOKEN"], TOKEN)
        self.assertNotIn(TOKEN, " ".join(args))
        self.assertEqual(self.left(), [])

    def test_a_refused_token_on_the_repository_lookup_also_takes_the_gh_path_and_a_gh_failure_is_a_failure(self):
        self.make_videos([("AC1", "desktop", 500)])
        self.replies["/repos/"] = (401, {"message": "Bad credentials"})
        self.gh_rc = 1
        result = self.upload()
        self.assertEqual((result["status"], result["step"], result["path"]), ("failed", "fallback", "gh_pr_comment"))
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertEqual(len(self.left()), 1)  # nothing posted, nothing deleted
        self.gh_rc, self.calls[:] = 0, []
        self.assertEqual(self.upload()["path"], "gh_pr_comment")
        self.assertEqual([h for h in self.http if h["method"] == "POST"], [])  # no upload is attempted once the lookup was refused

    def test_a_lookup_failure_that_is_not_a_refusal_is_reported_and_nothing_is_posted(self):
        self.make_videos([("AC1", "desktop", 500)])
        self.replies["/repos/"] = (500, {"message": "oops"})
        result = self.upload()
        self.assertEqual((result["status"], result["step"]), ("failed", "repository"))
        self.assertEqual([a for a, _ in self.calls if a[0] == "gh"], [])

    def test_the_payload_is_validated_and_a_run_without_videos_is_refused(self):
        self.make_videos([("AC1", "desktop", 500)])
        for over in ({"run_id": "../x"}, {"repository": "evil/repo"}, {"repository": "ONE-F-M/a/b"}, {"repository": "ONE-F-M"}, {"pr_number": "x"},
                     {"token": ""}, {"token": " spaced "}, {"token": None}):
            self.assertEqual(self.upload(**over)["step"], "validate", over)
        self.assertEqual(self.http, [])
        self.assertEqual(srv.upload_videos("processa", {"run_id": "other", "repository": "ONE-F-M/one_bpmn", "pr_number": 5, "token": TOKEN})["step"], "no_videos")
        self.make_videos([("AC1", "desktop", None)])
        self.assertEqual(self.upload()["step"], "no_videos")

    def test_the_token_is_never_in_an_argument_a_result_or_a_file(self):
        self.make_videos([("AC1", "desktop", 500)])
        result = self.upload()
        self.assertNotIn(TOKEN, json.dumps(result))
        for root, _d, files in os.walk(self.root):
            for name in files:
                if name.endswith((".json", ".js", ".sql", ".pw")):
                    with open(os.path.join(root, name), errors="ignore") as fh:
                        self.assertNotIn(TOKEN, fh.read(), name)

    def test_the_session_dispatches_the_command_and_it_commits_nothing(self):
        self.make_videos([("AC1", "desktop", 500)])
        status, result = self.session.execute(self._command(9, "upload_videos", "upload_videos", args={"run_id": "run-1", "repository": "ONE-F-M/one_bpmn", "pr_number": 5, "token": TOKEN}))
        self.assertEqual((status, result["status"]), ("ok", "videos_uploaded"))
        self.assertNotIn(TOKEN, json.dumps(result) + json.dumps(self.client.sent))
        verbs = {a[1] for a, _ in self.calls if a[0] == "git"}
        self.assertFalse(verbs & {"commit", "push", "merge", "reset", "rebase", "tag"}, verbs)


class TestMobileWebBuild(TestingAgentCase):
    """mobile_app_ionic: built with vite against the sandbox site, served with vite preview, no bench build or migrate."""

    STAGING_ENV = ("VITE_BASE_API_URL=https://staging.one-fm.com\nVITE_API_PREFIX=/api/method/one_fm.api.\nVITE_BASE_URL=/\n\n"
                   "VITE_FIREBASE_PROJECT_ID=one-fm-70641\nVITE_FIREBASE_AUTH_DOMAIN=one-fm-70641.firebaseapp.com\n")

    def setUp(self):
        super().setUp()
        origin, seed = os.path.join(self.root, "mobile_origin.git"), os.path.join(self.root, "mobile_seed")
        os.makedirs(seed)
        git(self.root, "init", "-q", "--bare", origin)
        git(seed, "init", "-q", "-b", "staging")
        for name, text in ((".env.staging", self.STAGING_ENV), ("yarn.lock", "committed lock\n"), ("package.json", "{}\n"),
                           ("src/composable/useDataUseConsent.js", 'export const DATA_USE_POLICY_VERSION = "2026-10-22";\n'), ("src/App.vue", "a\n")):
            path = os.path.join(seed, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(text)
        git(seed, "add", "-A")
        git(seed, "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q", "-m", "seed")
        git(seed, "push", "-q", origin, "staging")
        self.mobile_dir = os.path.join(self.bench, "apps", "mobile_app_ionic")
        git(self.root, "clone", "-q", origin, self.mobile_dir)
        with open(os.path.join(seed, "src/App.vue"), "w") as fh:
            fh.write("changed\n")
        git(seed, "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q", "-am", "pr")
        git(seed, "push", "-q", origin, "HEAD:refs/pull/5/head")
        self.m_pr_sha = git(seed, "rev-parse", "HEAD")
        self.http_ok = True
        patcher = patch.object(srv, "_http_ok", side_effect=lambda url, timeout=5: self.http_ok)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.run_dir = os.path.join(self.root, "specs", "run-m")

    def prepare_mobile(self):
        payload = {"base_branch": "staging", "pr_number": 5, "run_id": "run-m", "roles": ["Sales User"], "password": PASSWORD}
        return srv.prepare_site("mobile_app_ionic", payload)

    def head(self):
        self.pinged[:] = [False, True]
        return srv.checkout_pr_head("mobile_app_ionic", {"run_id": "run-m", "pr_number": 5})

    def ready(self):
        self.assertEqual(self.prepare_mobile()["status"], "prepared")
        self.calls.clear()
        self.popen.clear()
        return self.head()

    def lock(self):
        return open(os.path.join(self.mobile_dir, "yarn.lock")).read()

    def test_it_builds_with_the_sandbox_env_from_staging_and_serves_with_vite_preview_and_runs_no_bench_build_or_migrate(self):
        result = self.ready()
        self.assertEqual((result["status"], result["mobile_url"], result["pr_head_sha"]), ("ready", "http://127.0.0.1:8100", self.m_pr_sha))
        env = dict(l.split("=", 1) for l in open(os.path.join(self.mobile_dir, ".env.sandbox")).read().splitlines() if "=" in l)
        self.assertEqual(env["VITE_BASE_API_URL"], "http://127.0.0.1:8000")
        self.assertEqual(env["VITE_API_PREFIX"], "/api/method/one_fm.api.")  # with the trailing dot, as .env.staging has it
        self.assertEqual(env["VITE_BASE_URL"], "/")  # the router's base path: a site address here would break every route
        staging = dict(l.split("=", 1) for l in self.STAGING_ENV.splitlines() if "=" in l)
        for key in ("VITE_FIREBASE_PROJECT_ID", "VITE_FIREBASE_AUTH_DOMAIN"):  # everything else in .env.staging is carried over unchanged
            self.assertEqual(env[key], staging[key])
        self.assertNotIn("staging.one-fm.com", open(os.path.join(self.mobile_dir, ".env.sandbox")).read())
        self.assertEqual(self.argv("yarn", "vite", "build"), [["yarn", "vite", "build", "--mode", "sandbox"]])
        preview = [a for a, _ in self.popen if a[:3] == ["yarn", "vite", "preview"]]
        self.assertEqual(preview, [["yarn", "vite", "preview", "--host", "127.0.0.1", "--port", "8100", "--strictPort"]])
        self.assertEqual((self.argv("bench", "build"), self.argv("bench", "--site", "sandbox", "migrate")), ([], []))
        self.assertEqual(git(self.mobile_dir, "rev-parse", "HEAD"), self.m_pr_sha)

    def test_the_preview_is_tracked_with_the_sites_processes_and_the_marker_records_the_app_and_its_consent_version(self):
        result = self.ready()
        self.assertEqual(result["preview_pid"], 4003)
        with open(os.path.join(self.bench, "logs", "sandbox_services.json")) as fh:
            self.assertEqual(json.load(fh), {"pids": [4001, 4002, 4003]})
        marker = json.load(open(os.path.join(self.snapshots, "run-m.json")))
        self.assertEqual(marker["mobile"], {"url": "http://127.0.0.1:8100", "consent_version": "2026-10-22"})

    def test_the_preview_origin_is_allowed_by_the_site_before_the_preview_starts(self):
        self.ready()
        self.assertEqual(self.argv("bench", "--site", "sandbox", "set-config", "allow_cors"), [["bench", "--site", "sandbox", "set-config", "allow_cors", "http://127.0.0.1:8100"]])
        self.prepare_mobile()
        self.calls.clear()
        self.popen.clear()
        self.broken.add("set-config")
        result = self.head()
        self.assertEqual((result["status"], result["step"]), ("failed", "cors"))
        self.assertEqual([a for a, _ in self.popen if a[:3] == ["yarn", "vite", "preview"]], [])

    def test_a_build_that_mentions_production_fails_and_nothing_is_served(self):
        for text in ("fetch('https://one-fm.com/api')", "const u = 'https://staging.one-fm.com'", "x = 'HTTPS://ONE-FM.COM'"):
            self.dist_text = text
            self.assertEqual(self.prepare_mobile()["status"], "prepared")
            self.popen.clear()
            result = self.head()
            self.assertEqual((result["status"], result["step"]), ("failed", "production_url"), text)
            self.assertIn("assets/app.js", json.dumps(result))
            self.assertEqual(self.popen, [])  # not even the site was started

    def test_the_privacy_policy_link_the_app_opens_is_allowed_but_any_other_mention_of_the_host_is_not(self):
        self.dist_text = "const PRIVACY_POLICY_URL = 'https://app.one-fm.com/privacy_policy/';"
        self.assertEqual(self.ready()["status"], "ready")  # DataUsePage.vue hard-codes this, so every real build contains it
        for text in ("fetch('https://app.one-fm.com/api/method/x')", "const m = 'support@one-fm.com'",
                     "const a = 'https://app.one-fm.com/privacy_policy/'; const b = 'https://one-fm.com'"):
            self.dist_text = text
            self.assertEqual(self.prepare_mobile()["status"], "prepared")
            self.popen.clear()
            result = self.head()
            self.assertEqual((result["status"], result["step"]), ("failed", "production_url"), text)
            self.assertEqual(self.popen, [])

    def test_a_firebase_domain_is_not_mistaken_for_production(self):
        self.dist_text = "const d = 'one-fm-70641.firebaseapp.com'; const s = 'mobile.one-fm-70641.example'"
        self.assertEqual(self.ready()["status"], "ready")

    def test_yarn_lock_is_put_back_whether_the_build_works_or_not(self):
        self.prepare_mobile()
        self.assertEqual(self.head()["status"], "ready")
        self.assertEqual(self.lock(), "committed lock\n")  # yarn install appended to it; it was restored
        self.assertEqual(git(self.mobile_dir, "status", "--short", "--", "yarn.lock"), "")
        for attr, value, step in (("vite_rc", 1, "build"), ("yarn_install_rc", 1, "install")):
            self.prepare_mobile()
            setattr(self, attr, value)
            result = self.head()
            self.assertEqual((result["status"], result["step"]), ("failed", step))
            self.assertEqual(self.lock(), "committed lock\n")
            self.vite_rc = self.yarn_install_rc = 0

    def test_a_preview_that_never_answers_is_stopped_and_reported(self):
        self.prepare_mobile()
        self.http_ok = False
        result = self.head()
        self.assertEqual((result["status"], result["step"]), ("failed", "start_preview"))
        self.assertIn(4003, self.stopped[-1])

    def test_a_missing_staging_env_is_reported_rather_than_building_with_the_wrong_one(self):
        empty = os.path.join(self.root, "no_env_app")
        os.makedirs(empty)
        self.assertIn(".env.staging is missing", srv._write_sandbox_env(empty))
        self.assertFalse(os.path.exists(os.path.join(empty, ".env.sandbox")))  # nothing is written to build with
        self.prepare_mobile()  # checks the base out, which is where .env.staging comes from
        self.assertIsNone(srv._write_sandbox_env(self.mobile_dir))

    def _specs(self):
        self.ready()
        srv.write_spec("mobile_app_ionic", {"run_id": "run-m", "file": "ac1.spec.js", "content": "test('AC1 shows the home screen', async () => {});\n"})
        self.report = pw_report(("ac1.spec.js", "AC1 shows the home screen", "desktop", "expected", [("passed", None, None)]))

    def test_mobile_helpers_and_the_smoke_spec_exist_only_for_a_mobile_run_and_the_specs_get_the_apps_address_and_consent_version(self):
        self._specs()
        srv.run_specs("mobile_app_ionic", {"run_id": "run-m"})
        for name in ("mobile-helpers.js", "mobile-smoke.spec.js", "frappe-helpers.js"):
            self.assertTrue(os.path.isfile(os.path.join(self.run_dir, name)), name)
        env = next(e for a, e in self.calls if a[0] == "npx")
        self.assertEqual((env["SPEC_MOBILE_URL"], env["SPEC_MOBILE_CONSENT_VERSION"], env["SPEC_BASE_URL"]), ("http://127.0.0.1:8100", "2026-10-22", "http://127.0.0.1:8000"))
        self.assertNotIn(PASSWORD, " ".join(next(a for a, _ in self.calls if a[0] == "npx")))

    def test_a_frappe_apps_run_gets_no_mobile_files_or_variables(self):
        self.prepare()
        self.checkout()
        self.write(name="ac1.spec.js") if hasattr(self, "write") else srv.write_spec("processa", {"run_id": "run-1", "file": "ac1.spec.js", "content": "test('AC1', async () => {});\n"})
        self.report = pw_report(("ac1.spec.js", "AC1", "desktop", "expected", [("passed", None, None)]))
        srv.run_specs("processa", {"run_id": "run-1"})
        run_dir = os.path.join(self.root, "specs", "run-1")
        self.assertFalse(os.path.exists(os.path.join(run_dir, "mobile-helpers.js")) or os.path.exists(os.path.join(run_dir, "mobile-smoke.spec.js")))
        self.assertNotIn("SPEC_MOBILE_URL", next(e for a, e in self.calls if a[0] == "npx"))

    def _node(self, script, env=None):
        import shutil as _sh
        if not _sh.which("node"):
            self.skipTest("node is not installed here")
        os.makedirs(self.run_dir, exist_ok=True)
        with open(os.path.join(self.run_dir, "mobile-helpers.js"), "w") as fh:
            fh.write(srv._MOBILE_HELPERS)
        return subprocess.run(["node", "-e", script], cwd=self.run_dir, capture_output=True, text=True,
                              env={**os.environ, "SPEC_MOBILE_URL": "http://127.0.0.1:8100", "SPEC_BASE_URL": "http://127.0.0.1:8000",
                                   "SPEC_MOBILE_CONSENT_VERSION": "2026-10-22", "MOBILE_TEST_PW": PASSWORD, **(env or {})})

    STUB = """
        const m = require('./mobile-helpers'); const seen = { init: [], fetch: [] };
        globalThis.fetch = async (url, o) => { seen.fetch.push([url, String(o.body), o.method]);
          const ok = process.env.LOGIN_OK !== '0';
          return { ok, status: ok ? 200 : 401, json: async () => ({ data: { token: 'Bearer abc', refresh_token: 'r1', employee_id: 'E1', enrolled: 1 } }) }; };
        const page = { addInitScript: async (fn, arg) => { const store = {}; globalThis.localStorage = { setItem: (k, v) => { store[k] = v; } }; fn(arg); seen.init.push(store); },
          goto: async (u) => seen.goto = u, waitForLoadState: async () => {} };
    """

    def test_a_seeded_session_has_the_user_the_token_and_the_consent_the_app_looks_for(self):
        run = self._node(self.STUB + "(async () => { await m.seedSession(page, { employeeId: 'E1', password: process.env.MOBILE_TEST_PW }); console.log(JSON.stringify(seen)); })();")
        self.assertEqual(run.returncode, 0, run.stderr)
        seen = json.loads(run.stdout)
        self.assertEqual(seen["fetch"][0][0], "http://127.0.0.1:8000/api/method/one_fm.api.v1.authentication.user_login")
        self.assertEqual(seen["fetch"][0][1:], [urllib.parse.urlencode({"employee_id": "E1", "password": PASSWORD}), "POST"])
        stores = {k: v for store in seen["init"] for k, v in store.items()}
        user, auth = json.loads(stores["user"]), json.loads(stores["auth"])
        self.assertEqual((user["token"], user["refreshToken"], user["user"]["employee_id"]), ("Bearer abc", "r1", "E1"))
        self.assertTrue(user["user"] and user["token"])  # the app's guard: authenticated means a user and a token
        self.assertEqual((auth["employeeId"], auth["isRegistered"]), ("E1", True))
        self.assertEqual(stores["CapacitorStorage.data_use_consent"], "2026-10-22")  # the current policy version, not a made-up value

    def test_a_token_you_already_have_skips_the_login_call_and_a_refused_login_never_shows_the_password(self):
        run = self._node(self.STUB + "(async () => { await m.seedSession(page, { user: { employee_id: 'E9' }, token: 'token k:s' }); console.log(JSON.stringify(seen)); })();")
        self.assertEqual(run.returncode, 0, run.stderr)
        seen = json.loads(run.stdout)
        self.assertEqual(seen["fetch"], [])
        self.assertEqual(json.loads(next(s["user"] for s in seen["init"] if "user" in s))["token"], "token k:s")
        refused = self._node(self.STUB + "m.seedSession(page, { employeeId: 'E1', password: process.env.MOBILE_TEST_PW }).catch((e) => { console.error(e.message); process.exit(3); });", {"LOGIN_OK": "0"})
        self.assertEqual(refused.returncode, 3)
        self.assertIn("HTTP 401", refused.stderr)
        self.assertNotIn(PASSWORD, refused.stderr + refused.stdout)

    def test_the_helper_and_the_smoke_spec_are_valid_javascript_and_the_smoke_spec_skips_without_an_employee(self):
        import shutil as _sh
        if not _sh.which("node"):
            self.skipTest("node is not installed here")
        os.makedirs(self.run_dir, exist_ok=True)
        for name, text in (("mobile-helpers.js", srv._MOBILE_HELPERS), ("mobile-smoke.spec.js", srv._MOBILE_SMOKE)):
            with open(os.path.join(self.run_dir, name), "w") as fh:
                fh.write(text)
            self.assertEqual(subprocess.run(["node", "--check", name], cwd=self.run_dir, capture_output=True, text=True).returncode, 0, name)
        self.assertIn("test.skip(!id || !password", srv._MOBILE_SMOKE)
        self.assertIn("@ui", srv._MOBILE_SMOKE)  # it also runs at the phone size

    def test_a_criterion_that_needs_a_native_feature_is_could_not_test_with_the_reason_native_device_and_is_never_run(self):
        self.prepare()
        self.checkout()
        srv.write_spec("processa", {"run_id": "run-1", "file": "ac1.spec.js", "content": "test('AC1 shows the list', async () => {});\n"})
        srv.write_spec("processa", {"run_id": "run-1", "file": "ac2.spec.js", "content": "test.fixme('AC2 enrols a face with the camera @native-device', async () => {});\n"})
        self.report = pw_report(("ac1.spec.js", "AC1 shows the list", "desktop", "expected", [("passed", None, None)]))
        result = srv.run_specs("processa", {"run_id": "run-1"})
        npx = next(a for a, _ in self.calls if a[0] == "npx")
        self.assertEqual(npx[npx.index("--grep-invert") + 1], "@native-device")
        native = [r for r in result["results"] if r["status"] == "could_not_test"]
        self.assertEqual(len(native), 1)
        self.assertEqual((native[0]["criterion"], native[0]["reason"], native[0]["file"], native[0]["video"]), ("AC2", "native_device", "ac2.spec.js", None))
        self.assertEqual(result["summary"], {"passed": 1, "could_not_test": 1})
        self.assertEqual(json.load(open(os.path.join(self.root, "specs", "run-1", "results-head.json")))[-1]["reason"], "native_device")

    def test_a_run_of_only_native_specs_reports_them_instead_of_failing_on_no_tests_found(self):
        self.prepare()
        self.checkout()
        srv.write_spec("processa", {"run_id": "run-1", "file": "ac1.spec.js", "content": "test('AC1 sends a push @native-device', async () => {});\n"})
        self.report, self.npx_rc, self.npx_out = None, 1, "Error: No tests found"
        result = srv.run_specs("processa", {"run_id": "run-1"})
        self.assertEqual((result["status"], result["summary"]), ("specs_run", {"could_not_test": 1}))
        self.assertEqual(result["results"][0]["reason"], "native_device")



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
        self.assertEqual(request.full_url, "https://processa.example/api/method/processa.api.sandbox_session.next_command")
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
