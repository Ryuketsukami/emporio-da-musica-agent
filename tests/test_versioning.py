import subprocess
import tempfile
import unittest
from pathlib import Path

from src.versioning import app_version, build_header, data_sha, sha12


def fake_run(outputs):
    def run(argv, **kw):
        out, code = outputs[argv[1]]
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")
    return run


class VersioningTest(unittest.TestCase):
    def test_app_version_clean_dirty_and_failure(self):
        sha = "a" * 40
        self.assertEqual(app_version(fake_run({"rev-parse": (sha + "\n", 0), "status": ("", 0)})), {"commit": sha, "dirty": False})
        self.assertEqual(app_version(fake_run({"rev-parse": (sha + "\n", 0), "status": (" M src/x.py\n", 0)}))["dirty"], True)
        self.assertEqual(app_version(fake_run({"rev-parse": ("", 128), "status": ("", 128)})), {"commit": "unknown", "dirty": None})

        def boom(argv, **kw):
            raise FileNotFoundError("git")
        self.assertEqual(app_version(boom), {"commit": "unknown", "dirty": None})

    def test_hashes_are_stable_and_sensitive(self):
        self.assertEqual(sha12("abc"), sha12("abc"))
        self.assertNotEqual(sha12("abc"), sha12("abd"))
        self.assertEqual(len(sha12("abc")), 12)
        d = Path(tempfile.mkdtemp())
        (d / "a.csv").write_text("1", encoding="utf-8")
        first = data_sha(d)
        (d / "a.csv").write_text("2", encoding="utf-8")
        self.assertNotEqual(first, data_sha(d))

    def test_header(self):
        run = fake_run({"rev-parse": ("b" * 40 + "\n", 0), "status": ("", 0)})
        h = build_header("20261102-100000-abcd", "chat", "sdk:claude-sonnet-5", "SYS", [{"name": "t"}], None, run=run)
        self.assertEqual((h["schema"], h["session"], h["source"], h["backend"], h["model"]),
                         (1, "20261102-100000-abcd", "chat", "sdk", "claude-sonnet-5"))
        self.assertEqual(h["app"], {"commit": "b" * 40, "dirty": False})
        self.assertEqual(h["prompt_sha"], sha12("SYS"))
        self.assertEqual(set(h), {"schema", "session", "source", "app", "backend", "model", "prompt_sha", "tools_sha", "data_sha", "now_override"})
        with self.assertRaises(ValueError):
            build_header("x", "prod", "sdk:m", "S", [], None, run=run)
