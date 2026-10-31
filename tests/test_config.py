import tempfile
import unittest
from pathlib import Path

from src.config import load_settings, parse_env

MISSING = Path("/nonexistent/.env")


class ParseEnvTest(unittest.TestCase):
    def test_reads_pairs_and_strips_inline_comments(self):
        text = "LLM_MODEL=claude-sonnet-5\nLLM_DEV=false           # true = cli\n# full comment\n\nLLM_API_KEY=\n"
        self.assertEqual(parse_env(text), {"LLM_MODEL": "claude-sonnet-5", "LLM_DEV": "false", "LLM_API_KEY": ""})

    def test_strips_matching_quotes(self):
        self.assertEqual(parse_env("A=\"x y\"\nB='z'"), {"A": "x y", "B": "z"})

    def test_hash_without_leading_space_is_part_of_value(self):
        self.assertEqual(parse_env("A=abc#def"), {"A": "abc#def"})


class LoadSettingsTest(unittest.TestCase):
    def write(self, text):
        path = Path(tempfile.mkdtemp()) / ".env"
        path.write_text(text, encoding="utf-8")
        return path

    def test_defaults_when_file_missing(self):
        s = load_settings(MISSING, environ={})
        self.assertEqual((s.api_key, s.model, s.dev, s.dev_model), ("", "claude-sonnet-5", False, "sonnet"))

    def test_environment_overrides_file(self):
        s = load_settings(self.write("LLM_MODEL=a\nLLM_DEV=false\n"), environ={"LLM_MODEL": "b", "LLM_DEV": "TRUE"})
        self.assertEqual((s.model, s.dev), ("b", True))

    def test_dev_flag_values(self):
        for raw, expected in [("1", True), ("yes", True), ("true", True), ("0", False), ("no", False), ("", False)]:
            self.assertEqual(load_settings(MISSING, environ={"LLM_DEV": raw}).dev, expected, raw)
