"""The image clones each app from the repository that holds it today."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("02c_clone_onefm_apps_2.sh")


def clone_urls(app: str) -> list[str]:
    line = next(l for l in SCRIPT.read_text().splitlines() if l.startswith(f"clone_app {app} "))
    return re.findall(r"github\.com/\$\{GITHUB_ORG\}/([\w.-]+)\.git", line)


PLAIN = Path(__file__).with_name("02d_clone_lms_and_mobile.sh")
PINS = Path(__file__).with_name("lib_pin_apps.sh")


class TestCloneScripts(unittest.TestCase):
    def test_processa_is_cloned_from_the_renamed_repository(self):
        self.assertEqual(clone_urls("processa"), ["processa", "processa"])

    def test_this_repository_is_cloned_as_the_agent_sandbox_app_and_pinned(self):
        line = next(l for l in PLAIN.read_text().splitlines() if l.startswith("clone_plain_repo agent_sandbox "))
        self.assertEqual(re.findall(r"github\.com/\$\{GITHUB_ORG\}/([\w.-]+)\.git", line), ["agent-sandbox", "agent-sandbox"])
        self.assertRegex(PINS.read_text(), r"\[agent_sandbox\]=\"[0-9a-f]{40}\"")


if __name__ == "__main__":
    unittest.main()
