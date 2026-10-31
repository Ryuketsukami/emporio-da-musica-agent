import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.report import load_sessions, main, summarize
from src.eventlog import read_events
from src.labels import RUBRIC_VERSION
from src.llm import Turn, Usage


def _signals(fallbacks=0, guard_hits=0, tech_errors=0, human=0):
    return {"turns": 1, "tickets": {"complaint": 0, "human": human}, "guard_hits": guard_hits,
            "fallbacks": fallbacks, "tool_cap_hit": False, "order_misses": 0, "order_locked": False,
            "tech_errors": tech_errors, "repeated_question": False}


def _judge(resolution, frustration):
    return {"resolution": resolution, "resolution_evidence": "", "frustration": frustration,
            "frustration_evidence": "", "resolution_evidence_found": True, "frustration_evidence_found": True}


def _header(session_id, source, commit="abc1234567", model="claude-sonnet-5", prompt_sha="p1", data_sha="d"):
    return {"schema": 1, "session": session_id, "source": source, "app": {"commit": commit, "dirty": False},
            "backend": "sdk", "model": model, "prompt_sha": prompt_sha, "tools_sha": "t", "data_sha": data_sha,
            "now_override": None}


def _write(path: Path, header: dict, labels: dict | None = None, ended: bool = True, events: list[dict] = ()) -> None:
    lines = [json.dumps({"t": "2026-11-02T10:00:00+00:00", "kind": "header", **header}, ensure_ascii=False)]
    lines += [json.dumps({"t": "2026-11-02T10:01:00+00:00", **e}, ensure_ascii=False) for e in events]
    if ended:
        lines.append(json.dumps({"t": "2026-11-02T10:04:00+00:00", "kind": "end"}))
    if labels is not None:
        lines.append(json.dumps({"t": "2026-11-02T10:05:00+00:00", "kind": "labels", **labels}, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class FakeJudge:
    def __init__(self, reply):
        self.reply = reply

    def call(self, system, messages, tools):
        return Turn(self.reply, [], Usage(), [{"type": "text", "text": self.reply}])


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

        # Group 1: two chat sessions, same version, both judged.
        _write(self.dir / "s1.jsonl", _header("s1", "chat"), {
            "rubric_version": RUBRIC_VERSION, "signals": _signals(fallbacks=1),
            "judge": _judge("resolved", 0), "judge_model": "m",
        })
        _write(self.dir / "s2.jsonl", _header("s2", "chat"), {
            "rubric_version": RUBRIC_VERSION, "signals": _signals(),
            "judge": _judge("handed_off", 2), "judge_model": "m",
        })

        # Group 2: a different-commit chat session, unlabelled.
        _write(self.dir / "s3.jsonl", _header("s3", "chat", commit="zzz9999999"))

        # An eval session, excluded by default (same version as group 1, code signals only).
        _write(self.dir / "s4.jsonl", _header("s4", "eval"), {
            "rubric_version": RUBRIC_VERSION, "signals": _signals(), "judge": None, "judge_model": None,
        })

    def test_load_sessions_skips_files_without_a_header(self):
        (self.dir / "nohdr.jsonl").write_text(json.dumps({"t": "x", "kind": "end"}) + "\n", encoding="utf-8")
        self.assertEqual(len(load_sessions([self.dir])), 4)

    def test_default_filter_excludes_eval(self):
        summary = summarize(load_sessions([self.dir]))
        self.assertEqual(summary["filter"], "chat")
        self.assertEqual(len(summary["groups"]), 2)

    def test_group_rates(self):
        summary = summarize(load_sessions([self.dir]))
        groups = {tuple(g["version"].values()): g for g in summary["groups"]}

        g1 = groups[("abc1234", "p1", "t", "d", "claude-sonnet-5")]
        self.assertEqual(g1["sessions"], 2)
        self.assertEqual(g1["labelled"], 2)
        self.assertEqual(g1["judged"], 2)
        self.assertEqual(g1["resolution"]["resolved"], 0.5)
        self.assertEqual(g1["resolution"]["handed_off"], 0.5)
        self.assertEqual(g1["frustrated_rate"], 0.5)
        self.assertEqual(g1["fallback_rate"], 0.5)

        g2 = groups[("zzz9999", "p1", "t", "d", "claude-sonnet-5")]
        self.assertEqual(g2["sessions"], 1)
        self.assertEqual(g2["labelled"], 0)
        self.assertEqual(g2["judged"], 0)
        self.assertIsNone(g2["resolution"]["resolved"])
        self.assertIsNone(g2["frustrated_rate"])
        self.assertIsNone(g2["fallback_rate"])
        self.assertIsNone(g2["guard_hit_rate"])
        self.assertIsNone(g2["tech_error_rate"])

    def test_cost_per_session_and_per_message(self):
        _write(self.dir / "s1.jsonl", _header("s1", "chat"), events=[
            {"kind": "customer", "text": "a"}, {"kind": "reply", "text": "b", "cost_usd": 0.01},
            {"kind": "customer", "text": "c"}, {"kind": "fallback", "text": "d", "cost_usd": 0.02}])
        _write(self.dir / "s2.jsonl", _header("s2", "chat"), events=[
            {"kind": "customer", "text": "a"}, {"kind": "error", "error": "LLMError", "cost_usd": 0.0}])
        summary = summarize(load_sessions([self.dir]))
        g1 = next(g for g in summary["groups"] if g["version"]["commit"] == "abc1234")
        self.assertEqual(g1["priced_sessions"], 2)
        self.assertEqual(g1["cost_per_session"], 0.015)
        self.assertEqual(g1["cost_per_message"], 0.01)

    def test_sessions_without_a_known_cost_are_left_out_of_cost(self):
        _write(self.dir / "s1.jsonl", _header("s1", "chat"), events=[
            {"kind": "customer", "text": "a"}, {"kind": "reply", "text": "b", "cost_usd": 0.01},
            {"kind": "customer", "text": "c"}, {"kind": "reply", "text": "d", "cost_usd": None}])
        _write(self.dir / "s2.jsonl", _header("s2", "chat"), events=[  # logged before cost tracking
            {"kind": "customer", "text": "a"}, {"kind": "reply", "text": "b"}])
        summary = summarize(load_sessions([self.dir]))
        g1 = next(g for g in summary["groups"] if g["version"]["commit"] == "abc1234")
        self.assertEqual(g1["priced_sessions"], 0)
        self.assertIsNone(g1["cost_per_session"])
        self.assertIsNone(g1["cost_per_message"])

    def test_version_groups_split_on_data_and_tools(self):
        _write(self.dir / "s5.jsonl", _header("s5", "chat", data_sha="d2"))
        summary = summarize(load_sessions([self.dir]))
        self.assertEqual(list(summary["groups"][0]["version"]), ["commit", "prompt_sha", "tools_sha", "data_sha", "model"])
        self.assertEqual(len(summary["groups"]), 3)

    def test_old_rubric_judge_label_is_not_judged(self):
        _write(self.dir / "s1.jsonl", _header("s1", "chat"), {
            "rubric_version": "0", "signals": _signals(), "judge": _judge("resolved", 0), "judge_model": "m"})
        summary = summarize(load_sessions([self.dir]))
        g1 = next(g for g in summary["groups"] if g["version"]["commit"] == "abc1234")
        self.assertEqual((g1["labelled"], g1["judged"]), (2, 1))
        self.assertEqual(g1["resolution"]["handed_off"], 1.0)

    def test_all_includes_eval(self):
        summary = summarize(load_sessions([self.dir]), include_all=True)
        self.assertEqual(summary["filter"], "all")
        total_sessions = sum(g["sessions"] for g in summary["groups"])
        self.assertEqual(total_sessions, 4)

    def test_main_writes_summary_json_atomically(self):
        runtime = Path(tempfile.mkdtemp())
        with mock.patch("scripts.report.RUNTIME_DIR", runtime), contextlib.redirect_stdout(io.StringIO()):
            code = main(["--dir", str(self.dir)])
        self.assertEqual(code, 0)

        written = json.loads((runtime / "summary.json").read_text(encoding="utf-8"))
        self.assertFalse((runtime / "summary.json.tmp").exists())

        expected = summarize(load_sessions([self.dir]))
        written.pop("generated_at")
        expected.pop("generated_at")
        self.assertEqual(written, expected)

    def test_label_only_fills_sessions_without_a_current_judge_label(self):
        reply = ('{"resolution": "resolved", "resolution_evidence": "", "frustration": 0, '
                 '"frustration_evidence": ""}')
        runtime = Path(tempfile.mkdtemp())
        (self.dir / "running.jsonl").write_text(json.dumps({"t": "x", "kind": "header", **_header("r", "chat")}) + "\n",
                                                encoding="utf-8")
        with mock.patch("scripts.report.RUNTIME_DIR", runtime), contextlib.redirect_stdout(io.StringIO()):
            main(["--dir", str(self.dir), "--label"], judge_backend=FakeJudge(reply))

        # A session with no `end` event (still running or crashed) is not labelled.
        self.assertNotIn("labels", [e["kind"] for e in read_events(self.dir / "running.jsonl")])

        # s1 already had a current-rubric judge label: untouched.
        self.assertEqual([e["kind"] for e in read_events(self.dir / "s1.jsonl")].count("labels"), 1)

        # s3 had no labels event at all: it gets one, with the fake judge's resolution.
        s3_events = read_events(self.dir / "s3.jsonl")
        self.assertEqual([e["kind"] for e in s3_events].count("labels"), 1)
        self.assertEqual(s3_events[-1]["judge"]["resolution"], "resolved")


if __name__ == "__main__":
    unittest.main()
