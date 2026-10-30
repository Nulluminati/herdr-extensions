import unittest

import yaml

from support.paths import ROOT

CONFIG = ROOT / ".github/dependabot.yml"


class DependabotTests(unittest.TestCase):
    def setUp(self):
        self.config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        self.updates = self.config["updates"]

    def update(self, ecosystem, directory):
        matches = [
            entry
            for entry in self.updates
            if entry["package-ecosystem"] == ecosystem and entry["directory"] == directory
        ]
        self.assertEqual(len(matches), 1, f"one {ecosystem} entry in {directory}")
        return matches[0]

    def test_uses_configuration_version_two(self):
        self.assertEqual(self.config["version"], 2)

    def test_holds_no_entry_beyond_the_expected_ones(self):
        self.assertEqual(
            sorted((entry["package-ecosystem"], entry["directory"]) for entry in self.updates),
            [("github-actions", "/"), ("uv", "/"), ("uv", "/plugins/herdboard")],
        )

    def test_updates_pinned_github_actions_in_the_root(self):
        entry = self.update("github-actions", "/")
        self.assertEqual(entry["schedule"]["interval"], "weekly")

    def test_updates_both_uv_projects_weekly(self):
        for directory in ("/", "/plugins/herdboard"):
            entry = self.update("uv", directory)
            self.assertEqual(entry["schedule"]["interval"], "weekly")

    def test_each_uv_directory_holds_the_files_the_update_needs(self):
        directories = [entry["directory"] for entry in self.updates if entry["package-ecosystem"] == "uv"]
        self.assertTrue(directories)
        for directory in directories:
            for name in ("pyproject.toml", "uv.lock"):
                self.assertTrue((ROOT / directory.lstrip("/") / name).is_file(), f"{directory} needs {name}")

    def test_each_ecosystem_groups_minor_and_patch_updates(self):
        for entry in self.updates:
            self.assertEqual(len(entry["groups"]), 1, f"{entry['package-ecosystem']} groups its updates")
            group = next(iter(entry["groups"].values()))
            self.assertEqual(group["patterns"], ["*"])
            self.assertEqual(group["update-types"], ["minor", "patch"])

    def test_commit_messages_use_a_conventional_prefix(self):
        for entry in self.updates:
            self.assertEqual(entry["commit-message"], {"prefix": "chore(deps)"})


if __name__ == "__main__":
    unittest.main()
