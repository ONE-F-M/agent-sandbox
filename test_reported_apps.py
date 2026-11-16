"""The sandbox tells Processa which apps it holds, and answers to a repository name as well as to an app folder name."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import dev_agent_server as srv
import session_agent

REMOTES = {
    "one_fm": "https://github.com/ONE-F-M/one_fm.git",
    "one_fm_password_management": "https://ghp_exampletoken@github.com/ONE-F-M/password_management.git",
    "agent_sandbox": "git@github.com:ONE-F-M/agent-sandbox.git",
    "no_remote": None,
}


class BenchCase(unittest.TestCase):
    """A bench whose apps/ holds the REMOTES apps as git clones, a folder that is not a clone, and a folder with an unsafe name."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name in (*REMOTES, "not_a_clone", "bad-name"):
            os.makedirs(os.path.join(self.tmp.name, "apps", name, ".git" if name != "not_a_clone" else "src"))
        patcher = patch.object(srv, "BENCH_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        exec_patch = patch.object(srv, "_exec", side_effect=self._git)
        exec_patch.start()
        self.addCleanup(exec_patch.stop)

    @staticmethod
    def _git(args, **kwargs):
        name = os.path.basename(args[2])
        url = REMOTES.get(name)
        return subprocess.CompletedProcess(args, 0 if url else 1, (url or "") + "\n", "" if url else "no such remote")


class TestInstalledApps(BenchCase):
    def test_every_clone_is_reported_with_its_repository_and_nothing_else_is(self):
        self.assertEqual(
            srv.installed_apps(),
            [
                {"app": "agent_sandbox", "repository": "ONE-F-M/agent-sandbox"},
                {"app": "no_remote", "repository": ""},
                {"app": "one_fm", "repository": "ONE-F-M/one_fm"},
                {"app": "one_fm_password_management", "repository": "ONE-F-M/password_management"},
            ],
        )

    def test_a_token_in_a_remote_url_is_never_reported(self):
        self.assertNotIn("ghp_", repr(srv.installed_apps()))

    def test_a_bench_with_no_apps_folder_reports_nothing(self):
        with patch.object(srv, "BENCH_DIR", os.path.join(self.tmp.name, "missing")):
            self.assertEqual(srv.installed_apps(), [])

    def test_the_session_agent_reports_nothing_instead_of_failing_when_the_scan_fails(self):
        with patch.object(srv, "installed_apps", side_effect=RuntimeError("boom")):
            self.assertEqual(session_agent.reported_apps(), [])


class TestAppDirName(BenchCase):
    def test_an_app_folder_name_is_kept(self):
        self.assertEqual(srv.app_dir_name("one_fm"), "one_fm")
        self.assertEqual(srv.app_dir_name("agent_sandbox"), "agent_sandbox")

    def test_a_repository_name_becomes_the_folder_it_was_cloned_into(self):
        self.assertEqual(srv.app_dir_name("password_management"), "one_fm_password_management")
        self.assertEqual(srv.app_dir_name("agent-sandbox"), "agent_sandbox")

    def test_a_name_nothing_matches_comes_back_unchanged_so_errors_still_name_it(self):
        for name in ("press", "../etc", "", None):
            self.assertEqual(srv.app_dir_name(name), "" if name is None else name)

    def test_a_session_for_a_repository_name_works_on_the_folder(self):
        settings = {"target_app": "agent-sandbox", "git_branch": "staging", "work_item": "WI-1"}
        with patch.object(srv, "_head_branch_for", return_value="WI-1-work"):
            session = session_agent.Session(object(), settings)
        self.assertEqual(session.target_app, "agent_sandbox")
        self.assertEqual(session.app_dir, f"{self.tmp.name}/apps/agent_sandbox")


class TestRegistrationCarriesTheApps(BenchCase):
    def test_the_register_call_includes_the_apps(self):
        sent = []

        class Client:
            def __init__(self, base_url, session_id):
                self.session_id = session_id

            def call(self, endpoint, body):
                sent.append((endpoint, body))
                return {"accepted": False}

        env = {"SESSION_ID": "S-1", "SESSION_TOKEN": "t", "PROCESSA_URL": "https://processa.example", "SANDBOX_ENV": "local"}
        with patch.dict(os.environ, env), patch.object(session_agent, "Processa", Client), patch.object(session_agent, "check_processa_url"):
            self.assertEqual(session_agent.main(), 1)
        endpoint, body = sent[0]
        self.assertEqual(endpoint, "register")
        self.assertEqual(body["token"], "t")
        self.assertIn({"app": "agent_sandbox", "repository": "ONE-F-M/agent-sandbox"}, body["apps"])


class TestRunTestsForThisRepository(unittest.TestCase):
    def test_the_sandbox_repo_is_tested_with_unittest_and_never_with_bench(self):
        seen = []

        def run(cmd, **kwargs):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 1, "", "FAIL: test_a (test_b.TestC.test_a)\n")

        with patch.object(srv, "_run", side_effect=run), patch.object(srv, "_exec", side_effect=AssertionError("bench must not run")):
            passed, out, err, failing = srv._run_tests("agent_sandbox")
        self.assertFalse(passed)
        self.assertEqual(len(seen), 1)
        self.assertIn("python3 -m unittest discover", seen[0])
        self.assertIn("/apps/agent_sandbox", seen[0])
        self.assertTrue(failing)

    def test_a_passing_suite_passes(self):
        with patch.object(srv, "_run", return_value=subprocess.CompletedProcess("x", 0, "", "OK\n")):
            self.assertTrue(srv._run_tests("agent_sandbox")[0])


if __name__ == "__main__":
    unittest.main()
