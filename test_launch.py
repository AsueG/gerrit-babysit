import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock

_spec = importlib.util.spec_from_file_location("launch", pathlib.Path(__file__).parent / "macos" / "launch.py")
assert _spec and _spec.loader
launch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launch)


class SkillDirTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name)
        self.cache = self.home / "cache"
        self.installed = self.home / "installed_plugins.json"
        for name, value in (("PLUGIN_CACHE", self.cache), ("INSTALLED", self.installed)):
            patcher = mock.patch.object(launch, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def version(self, number):
        path = self.cache / "market" / "gerrit-babysit" / number
        path.mkdir(parents=True)
        return path

    def install(self, *entries):
        self.installed.write_text(json.dumps({"version": 2, "plugins": {"gerrit-babysit@market": list(entries)}}))

    def test_a_cloned_skill_runs_where_it_is(self):
        # Given
        clone = self.home / "repo" / ".claude" / "skills" / "gerrit-babysit"
        # When
        found = launch.skill_dir(clone)
        # Then
        self.assertEqual(clone, found)

    def test_a_plugin_runs_the_version_installed_last(self):
        # Given
        old, new = self.version("1.4.0"), self.version("1.4.1")
        self.install({"scope": "project", "installPath": str(old), "lastUpdated": "2026-10-02"},
                     {"scope": "user", "installPath": str(new), "lastUpdated": "2026-10-01"})
        # When
        found = launch.skill_dir(old)
        # Then
        self.assertEqual(new, found)

    def test_a_removed_or_unlisted_install_falls_back_to_the_one_install_sh_ran_from(self):
        # Given
        old = self.version("1.4.0")
        # When
        unlisted = launch.skill_dir(old)
        self.install({"scope": "user", "installPath": str(self.cache / "market" / "gerrit-babysit" / "gone")})
        removed = launch.skill_dir(old)
        # Then
        self.assertEqual([old, old], [unlisted, removed])


if __name__ == "__main__":
    unittest.main()
