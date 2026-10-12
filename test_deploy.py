"""deploy.py builds the right gcloud commands for the service, a no-traffic revision and the session Job."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import deploy


class TestDeploy(unittest.TestCase):
    def _commands(self, **kwargs):
        commands = []
        with patch.object(deploy, "run", side_effect=lambda command, description: commands.append(command)), patch.object(
            deploy.subprocess, "run"
        ) as sub:
            sub.return_value.returncode = 0
            deploy.deploy("beta", "proj", use_baked=True, **kwargs)
        return commands

    def _last(self, **kwargs):
        return self._commands(**kwargs)[-1]

    def test_a_plain_deploy_still_sends_traffic_to_the_new_revision(self):
        command = self._last()
        self.assertIn("gcloud run deploy dev-agent-sandbox-beta ", command)
        self.assertNotIn("--no-traffic", command)

    def test_a_no_traffic_deploy_names_the_tag(self):
        command = self._last(no_traffic=True, tag="candidate")
        self.assertIn("--no-traffic --tag=candidate", command)

    def test_no_traffic_without_a_tag_is_refused_before_anything_runs(self):
        with patch.object(deploy, "run") as run, self.assertRaises(SystemExit):
            deploy.deploy("beta", "proj", use_baked=True, no_traffic=True)
        run.assert_not_called()

    def test_a_job_deploy_creates_the_job_with_no_automatic_retry_and_no_secrets(self):
        command = self._last(job=True, processa_host="ba.example.com")
        self.assertIn("gcloud run jobs deploy dev-agent-sandbox-job-beta ", command)
        self.assertIn("--max-retries=0", command)
        self.assertIn("--tasks=1", command)
        self.assertIn("ALLOWED_PROCESSA_HOST=ba.example.com", command)
        self.assertIn("SKIP_SETUP=1", command)
        self.assertNotIn("--set-secrets", command)
        self.assertNotIn("gcloud run deploy", command)

    def test_a_job_without_a_pinned_host_still_deploys_but_warns(self):
        with patch("builtins.print") as printed:
            command = self._last(job=True)
        self.assertNotIn("ALLOWED_PROCESSA_HOST", command)
        self.assertTrue(any("--processa-host not set" in str(call.args[0]) for call in printed.call_args_list))

    def test_several_callback_hosts_are_pinned_with_a_delimiter_that_keeps_their_commas(self):
        command = self._last(callback_host="one-fm.com, staging.onefm.com,businessanalyst.onefm.com")
        self.assertIn("--set-env-vars=\"^@^PYTHONUNBUFFERED=1@", command)
        self.assertIn("@ALLOWED_CALLBACK_HOST=one-fm.com,staging.onefm.com,businessanalyst.onefm.com", command)

    def test_one_callback_host_is_pinned_the_same_way(self):
        self.assertIn("@ALLOWED_CALLBACK_HOST=one-fm.com", self._last(callback_host="one-fm.com"))

    def test_without_a_callback_host_no_pin_is_set_but_the_other_variables_still_are(self):
        command = self._last()
        self.assertNotIn("ALLOWED_CALLBACK_HOST", command)
        self.assertIn("@SANDBOX_ENV=beta", command)

    def test_each_environment_has_its_own_job_name(self):
        self.assertNotEqual(deploy._ENVIRONMENTS["production"]["job"], deploy._ENVIRONMENTS["beta"]["job"])


if __name__ == "__main__":
    unittest.main()
