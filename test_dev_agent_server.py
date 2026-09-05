"""Unit tests for dev_agent_server.py's pure logic — payload validation, the
tool implementations that don't need a real cloned working tree end-to-end,
and the trace-scanning helpers run_job relies on. No Docker, no live
Anthropic key, no real git/GitHub calls: subprocess and network boundaries
are mocked. Run with: python3 -m unittest test_dev_agent_server -v

Deliberately NOT covering _run_coding_loop or run_job end-to-end here — that
needs a real (or thoroughly mocked) Anthropic client and checked-out working
tree, which is what run_local_sandbox.sh's live Docker run is for.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import dev_agent_server as srv


def _valid_payload(**overrides):
    payload = {
        "correlation_id": "corr-1",
        "target_app": "one_bpmn",
        "git_branch": "staging",
        "work_item_description": "Fix the thing.",
        "agent_config": {"system_prompt": "You are helpful.", "model": "claude-haiku-4-5", "api_key": "sk-test"},
        "tools": [
            {"name": "read_file", "description": "Read a file.", "input_schema": {"type": "object", "properties": {}}},
        ],
        "github_token": "gh-test-token",
        "callback_url": "https://processa.example.com/api/method/one_bpmn.api.agent_callback.report_result",
    }
    payload.update(overrides)
    return payload


class TestValidatePayloadTools(unittest.TestCase):
    def test_valid_payload_passes(self):
        self.assertIsNone(srv._validate_payload(_valid_payload()))

    def test_missing_tools_field_rejected(self):
        payload = _valid_payload()
        del payload["tools"]
        self.assertIn("Missing required field", srv._validate_payload(payload))

    def test_empty_tools_list_rejected(self):
        error = srv._validate_payload(_valid_payload(tools=[]))
        self.assertIn("non-empty list", error)

    def test_tools_not_a_list_rejected(self):
        error = srv._validate_payload(_valid_payload(tools={"name": "read_file"}))
        self.assertIn("non-empty list", error)

    def test_tool_missing_required_field_rejected(self):
        error = srv._validate_payload(_valid_payload(tools=[{"name": "read_file"}]))
        self.assertIn("missing required field", error)
        self.assertIn("description", error)

    def test_unknown_tool_name_rejected(self):
        error = srv._validate_payload(_valid_payload(tools=[
            {"name": "delete_everything", "description": "x", "input_schema": {}},
        ]))
        self.assertIn("delete_everything", error)
        self.assertIn("no implementation", error)

    def test_all_known_tool_names_accepted(self):
        tools = [
            {"name": name, "description": "x", "input_schema": {"type": "object", "properties": {}}}
            for name in sorted(srv._KNOWN_TOOL_NAMES)
        ]
        self.assertIsNone(srv._validate_payload(_valid_payload(tools=tools)))


class TestEditFile(unittest.TestCase):
    def setUp(self):
        # realpath: on macOS, mkdtemp() returns a path through a symlink
        # (/var/folders/... -> /private/var/folders/...), and _safe_path
        # itself realpath()s before its prefix check — without matching that
        # here too, the two sides disagree and every call looks like an
        # escape attempt. BENCH_DIR in real use is never symlinked, so this
        # is purely a test-fixture concern, not something _safe_path itself
        # needs to guard against differently.
        self.app_dir = os.path.realpath(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.app_dir, ignore_errors=True)

    def _write(self, rel_path, content):
        abs_path = os.path.join(self.app_dir, rel_path)
        os.makedirs(os.path.dirname(abs_path) or self.app_dir, exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as fh:
            fh.write(content)

    def _read(self, rel_path):
        with open(os.path.join(self.app_dir, rel_path), encoding="utf-8") as fh:
            return fh.read()

    def test_replaces_a_unique_match(self):
        self._write("foo.py", "def foo():\n    return 1\n")
        result = srv._tool_edit_file(self.app_dir, {
            "path": "foo.py", "old_string": "return 1", "new_string": "return 2",
        })
        self.assertEqual(result, {"edited": True, "path": "foo.py"})
        self.assertEqual(self._read("foo.py"), "def foo():\n    return 2\n")

    def test_zero_matches_is_an_error_not_a_silent_noop(self):
        self._write("foo.py", "def foo():\n    return 1\n")
        result = srv._tool_edit_file(self.app_dir, {
            "path": "foo.py", "old_string": "return 999", "new_string": "return 2",
        })
        self.assertIn("error", result)
        self.assertEqual(self._read("foo.py"), "def foo():\n    return 1\n")  # untouched

    def test_ambiguous_match_is_rejected(self):
        self._write("foo.py", "x = 1\nx = 1\n")
        result = srv._tool_edit_file(self.app_dir, {
            "path": "foo.py", "old_string": "x = 1", "new_string": "x = 2",
        })
        self.assertIn("error", result)
        self.assertIn("2 times", result["error"])
        self.assertEqual(self._read("foo.py"), "x = 1\nx = 1\n")  # untouched

    def test_missing_file_is_an_error(self):
        result = srv._tool_edit_file(self.app_dir, {
            "path": "nope.py", "old_string": "a", "new_string": "b",
        })
        self.assertIn("error", result)

    def test_path_escape_is_rejected(self):
        result = srv._tool_edit_file(self.app_dir, {
            "path": "../../etc/passwd", "old_string": "a", "new_string": "b",
        })
        self.assertIn("error", result)


class TestDispatchToolRouting(unittest.TestCase):
    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        self.run_ctx = {
            "git_branch": "staging", "work_item_description": "Fix it.",
            "github_token": "gh-token", "correlation_id": "corr-1",
        }

    def tearDown(self):
        shutil.rmtree(self.app_dir, ignore_errors=True)

    def test_unknown_tool_name_returns_a_structured_error_not_a_crash(self):
        result = srv._dispatch_tool(self.app_dir, "one_bpmn", "not_a_real_tool", {}, self.run_ctx)
        self.assertIn("error", result)
        self.assertIn("not_a_real_tool", result["error"])

    def test_open_pull_request_routes_to_the_new_handler(self):
        with patch.object(srv, "_tool_open_pull_request", return_value={"pr_url": "https://x"}) as mock_fn:
            result = srv._dispatch_tool(self.app_dir, "one_bpmn", "open_pull_request", {"summary": "did stuff"}, self.run_ctx)
        mock_fn.assert_called_once_with("one_bpmn", {"summary": "did stuff"}, self.run_ctx)
        self.assertEqual(result, {"pr_url": "https://x"})

    def test_edit_file_routes_to_the_new_handler(self):
        with patch.object(srv, "_tool_edit_file", return_value={"edited": True}) as mock_fn:
            args = {"path": "a.py", "old_string": "x", "new_string": "y"}
            result = srv._dispatch_tool(self.app_dir, "one_bpmn", "edit_file", args, self.run_ctx)
        mock_fn.assert_called_once_with(self.app_dir, args)
        self.assertEqual(result, {"edited": True})


class TestOpenPullRequestTool(unittest.TestCase):
    def setUp(self):
        self.run_ctx = {
            "git_branch": "staging", "work_item_description": "Fix it.",
            "github_token": "gh-token", "correlation_id": "corr-1",
        }

    def test_no_changes_is_an_error_before_anything_else_runs(self):
        with patch.object(srv, "_collect_changed_files", return_value={}), patch.object(
            srv, "_run_tests"
        ) as mock_run_tests, patch.object(srv, "_open_pr") as mock_open_pr:
            result = srv._tool_open_pull_request("one_bpmn", {"summary": "nothing"}, self.run_ctx)
        self.assertIn("error", result)
        mock_run_tests.assert_not_called()
        mock_open_pr.assert_not_called()

    def test_reruns_tests_fresh_rather_than_trusting_a_stale_prior_result(self):
        """The whole point of re-testing inside this tool: the model may have
        edited files after its last run_tests call, so pass/fail here must
        come from a fresh run, not whatever the model last saw."""
        with patch.object(srv, "_collect_changed_files", return_value={"a.py": "content"}), patch.object(
            srv, "_run_tests", return_value=(True, "out", "err")
        ) as mock_run_tests, patch.object(
            srv, "_open_pr", return_value=("https://github.com/x/y/pull/1", None)
        ) as mock_open_pr:
            result = srv._tool_open_pull_request("one_bpmn", {"summary": "did stuff"}, self.run_ctx)
        mock_run_tests.assert_called_once_with("one_bpmn")
        self.assertEqual(result, {"pr_url": "https://github.com/x/y/pull/1", "tests_passed": True})
        args, kwargs = mock_open_pr.call_args
        self.assertEqual(args[0], "one_bpmn")
        self.assertEqual(args[1], "staging")
        self.assertIs(kwargs["tests_passed"], True)
        self.assertEqual(kwargs["stderr_tail"], "err")

    def test_pr_error_surfaces_with_test_status_attached(self):
        with patch.object(srv, "_collect_changed_files", return_value={"a.py": "content"}), patch.object(
            srv, "_run_tests", return_value=(False, "out", "boom")
        ), patch.object(srv, "_open_pr", return_value=(None, "GitHub API error (500)")):
            result = srv._tool_open_pull_request("one_bpmn", {"summary": "did stuff"}, self.run_ctx)
        self.assertEqual(result, {"error": "GitHub API error (500)", "tests_passed": False})


class TestLastOpenPrResult(unittest.TestCase):
    def test_no_trace_entries_returns_none(self):
        self.assertIsNone(srv._last_open_pr_result([]))

    def test_no_open_pull_request_calls_returns_none(self):
        trace = [{"role": "assistant", "content": "", "tool_calls": [
            {"name": "read_file", "arguments": {}, "result": {}, "status": "Success"},
        ]}]
        self.assertIsNone(srv._last_open_pr_result(trace))

    def test_returns_the_last_call_when_called_more_than_once(self):
        trace = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"name": "open_pull_request", "arguments": {}, "result": {"error": "no changes"}, "status": "Error"},
            ]},
            {"role": "assistant", "content": "", "tool_calls": [
                {"name": "write_file", "arguments": {}, "result": {}, "status": "Success"},
            ]},
            {"role": "assistant", "content": "", "tool_calls": [
                {"name": "open_pull_request", "arguments": {}, "result": {"pr_url": "https://x"}, "status": "Success"},
            ]},
        ]
        self.assertEqual(srv._last_open_pr_result(trace), {"pr_url": "https://x"})


class TestHeadBranchFor(unittest.TestCase):
    def test_deterministic_across_calls(self):
        a = srv._head_branch_for("one_bpmn", "staging", "Fix the thing.")
        b = srv._head_branch_for("one_bpmn", "staging", "Fix the thing.")
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("dev-agent/"))

    def test_differs_when_any_input_differs(self):
        base = srv._head_branch_for("one_bpmn", "staging", "Fix the thing.")
        self.assertNotEqual(base, srv._head_branch_for("one_fm", "staging", "Fix the thing."))
        self.assertNotEqual(base, srv._head_branch_for("one_bpmn", "main", "Fix the thing."))
        self.assertNotEqual(base, srv._head_branch_for("one_bpmn", "staging", "Fix another thing."))


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestCheckoutOrCreateHeadBranch(unittest.TestCase):
    def test_head_branch_already_exists_remotely(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if "git remote get-url origin" in cmd:
                return _FakeCompletedProcess(0, stdout="https://github.com/o/r.git\n")
            if f"git fetch origin dev-agent/abc" in cmd:
                return _FakeCompletedProcess(0)
            if "git checkout dev-agent/abc" in cmd:
                return _FakeCompletedProcess(0)
            return _FakeCompletedProcess(0)

        with patch.object(srv, "_run", side_effect=fake_run):
            ok, err = srv._checkout_or_create_head_branch("one_bpmn", "staging", "dev-agent/abc")
        self.assertTrue(ok)
        self.assertIsNone(err)
        # Never fell through to creating/pushing a new branch off the base.
        self.assertFalse(any("git push" in c for c in calls))

    def test_head_branch_missing_creates_and_pushes(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if "git remote get-url origin" in cmd:
                return _FakeCompletedProcess(0, stdout="https://github.com/o/r.git\n")
            if "git fetch origin dev-agent/abc" in cmd:
                return _FakeCompletedProcess(1, stderr="couldn't find remote ref")
            return _FakeCompletedProcess(0)

        with patch.object(srv, "_run", side_effect=fake_run):
            ok, err = srv._checkout_or_create_head_branch("one_bpmn", "staging", "dev-agent/abc")
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertTrue(any("git checkout -B dev-agent/abc" in c for c in calls))
        self.assertTrue(any("git push -u origin dev-agent/abc" in c for c in calls))

    def test_base_branch_fetch_failure_is_reported(self):
        def fake_run(cmd, **kwargs):
            if "git remote get-url origin" in cmd:
                return _FakeCompletedProcess(0, stdout="https://github.com/o/r.git\n")
            if "git fetch origin dev-agent/abc" in cmd:
                return _FakeCompletedProcess(1, stderr="no such ref")
            if "git fetch origin staging" in cmd:
                return _FakeCompletedProcess(1, stderr="network error")
            return _FakeCompletedProcess(0)

        with patch.object(srv, "_run", side_effect=fake_run):
            ok, err = srv._checkout_or_create_head_branch("one_bpmn", "staging", "dev-agent/abc")
        self.assertFalse(ok)
        self.assertIn("network error", err)

    def test_github_token_never_appears_in_a_reported_error(self):
        # Both fetches fail (a total auth failure, not just "branch doesn't
        # exist yet") so the function actually returns an error to check —
        # a fetch_head failure alone falls through to the base-branch path,
        # same as a genuine "branch not created yet" case would.
        with patch.dict(os.environ, {"GITHUB_TOKEN": "super-secret-token"}):
            def fake_run(cmd, **kwargs):
                if "git remote get-url origin" in cmd:
                    return _FakeCompletedProcess(0, stdout="https://github.com/o/r.git\n")
                if "git fetch origin dev-agent/abc" in cmd:
                    return _FakeCompletedProcess(1, stderr="fatal: could not read from super-secret-token@github.com")
                if "git fetch origin staging" in cmd:
                    return _FakeCompletedProcess(1, stderr="fatal: could not read from super-secret-token@github.com")
                return _FakeCompletedProcess(0)

            with patch.object(srv, "_run", side_effect=fake_run):
                ok, err = srv._checkout_or_create_head_branch("one_bpmn", "staging", "dev-agent/abc")
        self.assertFalse(ok)
        self.assertNotIn("super-secret-token", err)
        self.assertIn("***", err)


class TestValidateToolCallPayload(unittest.TestCase):
    def _payload(self, **overrides):
        payload = {
            "action": "read_file",
            "target_app": "one_bpmn",
            "git_branch": "staging",
            "work_item_description": "Fix the thing.",
            "github_token": "gh-token",
            "args": {"path": "a.py"},
        }
        payload.update(overrides)
        return payload

    def test_valid_payload_passes(self):
        self.assertIsNone(srv._validate_tool_call_payload(self._payload()))

    def test_missing_field_rejected(self):
        payload = self._payload()
        del payload["target_app"]
        self.assertIn("target_app", srv._validate_tool_call_payload(payload))

    def test_slow_action_rejected_here(self):
        error = srv._validate_tool_call_payload(self._payload(action="run_tests"))
        self.assertIn("run_tests", error)
        self.assertIn("not valid for /tool_call", error)

    def test_args_must_be_an_object(self):
        error = srv._validate_tool_call_payload(self._payload(args="not-a-dict"))
        self.assertIn("args must be an object", error)


class TestValidatePayloadActionBranch(unittest.TestCase):
    def _action_payload(self, **overrides):
        payload = {
            "correlation_id": "corr-1",
            "action": "run_tests",
            "target_app": "one_bpmn",
            "git_branch": "staging",
            "work_item_description": "Fix the thing.",
            "github_token": "gh-token",
            "callback_url": "https://processa.example.com/api/method/one_bpmn.api.agent_callback.report_result",
        }
        payload.update(overrides)
        return payload

    def test_valid_action_payload_passes(self):
        self.assertIsNone(srv._validate_payload(self._action_payload()))
        self.assertIsNone(srv._validate_payload(self._action_payload(action="open_pull_request")))

    def test_fast_action_rejected_on_run(self):
        error = srv._validate_payload(self._action_payload(action="read_file"))
        self.assertIn("not valid for /run", error)

    def test_missing_field_rejected(self):
        payload = self._action_payload()
        del payload["github_token"]
        self.assertIn("github_token", srv._validate_payload(payload))

    def test_does_not_require_agent_config_or_tools(self):
        payload = self._action_payload()
        self.assertNotIn("agent_config", payload)
        self.assertNotIn("tools", payload)
        self.assertIsNone(srv._validate_payload(payload))


class TestHandleToolCall(unittest.TestCase):
    def setUp(self):
        # A fresh BENCH_DIR per test (not a shared /tmp root) with a real
        # apps/one_bpmn directory — no symlink needed, and no risk of one
        # test's directory colliding with another's.
        self.bench_dir = os.path.realpath(tempfile.mkdtemp())
        self.app_dir = os.path.join(self.bench_dir, "apps", "one_bpmn")
        os.makedirs(self.app_dir)
        self.bench_dir_patch = patch.object(srv, "BENCH_DIR", self.bench_dir)
        self.bench_dir_patch.start()

    def tearDown(self):
        self.bench_dir_patch.stop()
        shutil.rmtree(self.bench_dir, ignore_errors=True)

    def _payload(self, action, args):
        return {
            "action": action, "target_app": "one_bpmn", "git_branch": "staging",
            "work_item_description": "Fix the thing.", "args": args, "github_token": "gh-token",
        }

    def test_checkout_failure_short_circuits_before_touching_files(self):
        with patch.object(srv, "_checkout_or_create_head_branch", return_value=(False, "branch trouble")):
            result = srv._handle_tool_call(self._payload("read_file", {"path": "a.py"}))
        self.assertEqual(result, {"error": "branch trouble"})

    def test_read_file_after_successful_checkout(self):
        with open(os.path.join(self.app_dir, "a.py"), "w") as fh:
            fh.write("hello")
        with patch.object(srv, "_checkout_or_create_head_branch", return_value=(True, None)):
            result = srv._handle_tool_call(self._payload("read_file", {"path": "a.py"}))
        self.assertEqual(result, {"found": True, "content": "hello"})

    def test_write_file_commits_and_pushes_on_success(self):
        with patch.object(srv, "_checkout_or_create_head_branch", return_value=(True, None)), patch.object(
            srv, "_commit_and_push", return_value=(True, None)
        ) as mock_commit:
            result = srv._handle_tool_call(self._payload("write_file", {"path": "a.py", "content": "x"}))
        self.assertEqual(result, {"written": True, "path": "a.py"})
        mock_commit.assert_called_once()

    def test_write_file_surfaces_a_commit_error_without_failing_the_write(self):
        with patch.object(srv, "_checkout_or_create_head_branch", return_value=(True, None)), patch.object(
            srv, "_commit_and_push", return_value=(False, "push rejected")
        ):
            result = srv._handle_tool_call(self._payload("write_file", {"path": "a.py", "content": "x"}))
        self.assertTrue(result["written"])
        self.assertEqual(result["commit_error"], "push rejected")

    def test_unknown_action_is_a_structured_error(self):
        with patch.object(srv, "_checkout_or_create_head_branch", return_value=(True, None)):
            result = srv._handle_tool_call(self._payload("delete_everything", {}))
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()


class TestHungCommandsReadAsFailures(unittest.TestCase):
    def test_run_turns_a_timeout_into_a_failed_result(self):
        boom = srv.subprocess.TimeoutExpired(cmd="yarn build", timeout=900, output=b"partial out", stderr=b"")
        with patch.object(srv.subprocess, "run", side_effect=boom):
            result = srv._run("cd /x && yarn build", timeout=900)
        self.assertEqual(result.returncode, 124)
        self.assertIn("partial out", result.stdout)
        self.assertIn("timed out after 900s", result.stderr)
        self.assertIn("yarn build", result.stderr)

    def test_mobile_tests_report_a_hung_build_instead_of_raising(self):
        hung = srv.subprocess.CompletedProcess("yarn build", 124, "", "timed out after 900s: yarn build")
        with patch.object(srv, "_run", return_value=hung) as run:
            passed, stdout, stderr = srv._run_tests("mobile_app_ionic")
        self.assertFalse(passed)
        self.assertIn("timed out after 900s", stderr)
        run.assert_called_once()  # a hung build never reaches the unit tests

    def test_an_escaping_exception_still_marks_the_job_failed_and_calls_back(self):
        payload = {"correlation_id": "corr-9", "callback_url": "https://processa.example.com/cb"}

        def job(_payload):
            raise RuntimeError("something nobody anticipated")

        with patch.object(srv, "_set_status") as set_status, patch.object(srv, "_post_callback") as post:
            srv._report_unexpected_failure(job, payload)
        set_status.assert_called_once()
        self.assertEqual(set_status.call_args.kwargs["state"], "failed")
        self.assertIn("RuntimeError", set_status.call_args.kwargs["error"])
        post.assert_called_once()
        url, body = post.call_args.args
        self.assertEqual(url, "https://processa.example.com/cb")
        self.assertEqual(body["correlation_id"], "corr-9")
        self.assertEqual(body["status"], "failed")
        self.assertIn("something nobody anticipated", body["error"])

    def test_a_job_that_finishes_normally_reports_nothing_extra(self):
        with patch.object(srv, "_set_status") as set_status, patch.object(srv, "_post_callback") as post:
            srv._report_unexpected_failure(lambda p: None, {"correlation_id": "corr-9"})
        set_status.assert_not_called()
        post.assert_not_called()
