"""The image clones each app from the repository that holds it today."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("02c_clone_onefm_apps_2.sh")


def clone_urls(app: str) -> list[str]:
    line = next(l for l in SCRIPT.read_text().splitlines() if l.startswith(f"clone_app {app} "))
    return re.findall(r"github\.com/\$\{GITHUB_ORG\}/([\w.-]+)\.git", line)


class TestCloneScripts(unittest.TestCase):
    def test_processa_is_cloned_from_the_renamed_repository(self):
        self.assertEqual(clone_urls("processa"), ["processa", "processa"])


if __name__ == "__main__":
    unittest.main()
