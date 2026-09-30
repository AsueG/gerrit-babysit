"""Run from this directory: python3 -m unittest"""
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
os.environ["GERRIT_BABYSIT_CONFIG"] = str(FIXTURES / "config.json")
os.environ.setdefault("GERRIT_BABYSIT_CACHE", tempfile.mkdtemp(prefix="gerrit-babysit-test-"))

import config  # noqa: E402
import gerrit  # noqa: E402


class ConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)

    def test_a_corrupt_state_file_reads_as_missing(self):
        # Given
        (self.dir / "seen.json").write_text('{"1:10:rev')
        # When
        with mock.patch("sys.stderr"):
            found = config.read_json(self.dir / "seen.json")
        # Then
        self.assertEqual({}, found)

    def test_a_failed_write_keeps_the_old_file_and_no_temp_file(self):
        # Given
        config.atomic_write(self.dir / "seen.json", {"a": 1})
        # When
        with self.assertRaises(TypeError):
            config.atomic_write(self.dir / "seen.json", {"a": object()})
        # Then
        self.assertEqual((["seen.json"], {"a": 1}),
                         (sorted(p.name for p in self.dir.iterdir()), config.read_json(self.dir / "seen.json")))

    def test_the_env_variable_wins_over_the_skill_folder(self):
        # Given
        chosen = self.dir / "chosen.json"
        chosen.write_text(json.dumps({"gerrit_host": "chosen"}))
        (self.dir / "config.json").write_text(json.dumps({"gerrit_host": "skill"}))
        # When
        with mock.patch.dict(os.environ, {"GERRIT_BABYSIT_CONFIG": str(chosen)}), \
                mock.patch.object(config, "SKILL_DIR", self.dir):
            loaded = config.load()
        # Then
        self.assertEqual("chosen", loaded["gerrit_host"])

    def test_missing_keys_fall_back_to_defaults(self):
        # Given
        (self.dir / "config.json").write_text(json.dumps({"gerrit_host": "h"}))
        # When
        with mock.patch.dict(os.environ, {"GERRIT_BABYSIT_CONFIG": ""}), \
                mock.patch.object(config, "SKILL_DIR", self.dir):
            loaded = config.load()
        # Then
        self.assertEqual((["Verified"], "zuul"), (loaded["ci_labels"], loaded["ci_user"]))

    def test_a_skill_cloned_into_the_repo_watches_that_repo(self):
        # Given
        skill = self.dir / "repo" / ".claude" / "skills" / "gerrit-babysit"
        # When
        with mock.patch.object(config, "SKILL_DIR", skill):
            repo = config.default_repo()
        # Then
        self.assertEqual(self.dir / "repo", repo)

    def test_a_plugin_watches_the_claude_project(self):
        # Given
        plugin = self.dir / ".claude" / "plugins" / "cache" / "gerrit-babysit" / "gerrit-babysit" / "1.0.0"
        # When
        with mock.patch.object(config, "SKILL_DIR", plugin), \
                mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.dir / "project")}):
            repo = config.default_repo()
        # Then
        self.assertEqual(self.dir / "project", repo)

    def test_http_credentials_come_from_the_gerrit_mcp_config(self):
        # Given
        mcp = self.dir / "gerrit_config.json"
        mcp.write_text(json.dumps({"gerrit_hosts": [
            {"external_url": "https://other.example/", "authentication": {"username": "x", "auth_token": "y"}},
            {"external_url": f"https://{gerrit.HOST}/", "authentication": {"username": "u", "auth_token": "p"}},
        ]}))
        # When
        with mock.patch.dict(gerrit.CONFIG, {"gerrit_mcp_config": str(mcp)}):
            credentials = gerrit.http_credentials()
        # Then
        self.assertEqual(("u", "p"), credentials)

    def test_http_credentials_refuse_another_hosts_token(self):
        # Given
        mcp = self.dir / "gerrit_config.json"
        mcp.write_text(json.dumps({"gerrit_hosts": [
            {"external_url": "https://other.example/", "authentication": {"username": "x", "auth_token": "y"}}]}))
        # When / Then
        with mock.patch.dict(gerrit.CONFIG, {"gerrit_mcp_config": str(mcp)}), self.assertRaises(KeyError):
            gerrit.http_credentials()

    def test_http_credentials_fall_back_to_netrc(self):
        # Given
        (self.dir / ".netrc").write_text(f"machine {gerrit.HOST} login u password p\n")
        (self.dir / ".netrc").chmod(0o600)
        # When
        with mock.patch.dict(os.environ, {"HOME": str(self.dir)}), \
                mock.patch.dict(gerrit.CONFIG, {"gerrit_mcp_config": None}):
            credentials = gerrit.http_credentials()
        # Then
        self.assertEqual(("u", "p"), credentials)


if __name__ == "__main__":
    unittest.main()
