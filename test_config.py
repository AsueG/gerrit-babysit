"""Run from this directory: python3 -m unittest"""
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
os.environ["GERRIT_BABYSIT_CONFIG"] = str(FIXTURES / "config.json")

import config  # noqa: E402
import watch  # noqa: E402


class ConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)

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

    def test_macos_language_is_read_from_apple_languages(self):
        # Given
        output = '(\n    "fr-FR",\n    "en-FR"\n)\n'
        # When
        with mock.patch.object(config.subprocess, "run", return_value=mock.Mock(stdout=output)):
            language = config.system_language()
        # Then
        self.assertEqual("fr", language)

    def test_unknown_languages_fall_back_to_english(self):
        # Given
        languages = ["de", "fr"]
        # When
        found = []
        for language in languages:
            with mock.patch.object(config, "LANGUAGE", language):
                found.append(config.t("conflict", n=1))
        # Then
        self.assertEqual(["1 has conflicts", "1 en conflit"], found)

    def test_http_credentials_come_from_the_gerrit_mcp_config(self):
        # Given
        mcp = self.dir / "gerrit_config.json"
        mcp.write_text(json.dumps({"gerrit_hosts": [
            {"external_url": "https://other.example/", "authentication": {"username": "x", "auth_token": "y"}},
            {"external_url": f"https://{watch.HOST}/", "authentication": {"username": "u", "auth_token": "p"}},
        ]}))
        # When
        with mock.patch.dict(watch.CONFIG, {"gerrit_mcp_config": str(mcp)}):
            credentials = watch.http_credentials()
        # Then
        self.assertEqual(("u", "p"), credentials)

    def test_http_credentials_refuse_another_hosts_token(self):
        # Given
        mcp = self.dir / "gerrit_config.json"
        mcp.write_text(json.dumps({"gerrit_hosts": [
            {"external_url": "https://other.example/", "authentication": {"username": "x", "auth_token": "y"}}]}))
        # When / Then
        with mock.patch.dict(watch.CONFIG, {"gerrit_mcp_config": str(mcp)}), self.assertRaises(KeyError):
            watch.http_credentials()

    def test_http_credentials_fall_back_to_netrc(self):
        # Given
        (self.dir / ".netrc").write_text(f"machine {watch.HOST} login u password p\n")
        (self.dir / ".netrc").chmod(0o600)
        # When
        with mock.patch.dict(os.environ, {"HOME": str(self.dir)}), \
                mock.patch.dict(watch.CONFIG, {"gerrit_mcp_config": None}):
            credentials = watch.http_credentials()
        # Then
        self.assertEqual(("u", "p"), credentials)


if __name__ == "__main__":
    unittest.main()
