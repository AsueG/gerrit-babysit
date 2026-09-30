"""Run from this directory: python3 -m unittest"""
import os
import pathlib
import tempfile
import unittest
from unittest import mock

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
os.environ["GERRIT_BABYSIT_CONFIG"] = str(FIXTURES / "config.json")
os.environ.setdefault("GERRIT_BABYSIT_CACHE", tempfile.mkdtemp(prefix="gerrit-babysit-test-"))

import i18n  # noqa: E402


class I18nTest(unittest.TestCase):
    def test_every_language_has_every_string(self):
        # Given
        english = i18n.MESSAGES["en"].keys()
        # When
        gaps = {language: english ^ messages.keys() for language, messages in i18n.MESSAGES.items()}
        # Then
        self.assertEqual({language: set() for language in i18n.MESSAGES}, gaps)

    def test_macos_language_is_read_from_apple_languages(self):
        # Given
        output = '(\n    "fr-FR",\n    "en-FR"\n)\n'
        # When
        with mock.patch.object(i18n.subprocess, "run", return_value=mock.Mock(stdout=output)):
            language = i18n.system_language()
        # Then
        self.assertEqual("fr", language)

    def test_unknown_languages_fall_back_to_english(self):
        # Given
        languages = ["de", "fr"]
        # When
        found = []
        for language in languages:
            with mock.patch.object(i18n, "_language", language):
                found.append(i18n.t("conflict", n=1))
        # Then
        self.assertEqual(["1 has conflicts", "1 en conflit"], found)


if __name__ == "__main__":
    unittest.main()
