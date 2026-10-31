import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evals.run_evals import DEFAULT_NOW, build_record, cost_line, evaluate, load_cases, main, run_case
from src.config import ROOT
from src.eventlog import read_events
from src.llm import Turn, Usage


class _BoomBackend:
    def call(self, *args, **kwargs):
        raise AssertionError("backend should not be called")


def text(t):
    return Turn(t, [], Usage(), [{"type": "text", "text": t}])


class FakeBackend:
    label = "fake"

    def __init__(self, *turns):
        self.turns = list(turns)

    def call(self, system, messages, tools):
        return self.turns.pop(0)


class EvaluateTest(unittest.TestCase):
    def test_pass(self):
        case = {"id": "x", "messages": ["a"], "must_call": ["get_product"], "must_contain": ["2.199,00", ["29/12", "29 de dezembro"]],
                "must_not_contain": ["GF-3D"], "must_not_call": ["get_order"], "must_call_any": [["quote", "get_product"]]}
        self.assertEqual(evaluate(case, ["Custa R$ 2.199,00", "até 29 de Dezembro"], ["get_product"]), [])

    def test_failures(self):
        case = {"id": "x", "messages": ["a"], "must_call": ["quote"], "must_contain": ["2.199,00"], "must_not_contain": ["gf-3d"],
                "must_not_call": ["get_order"], "must_call_any": [["quote", "get_product"]]}
        self.assertEqual(evaluate(case, ["temos o GF-3D"], ["get_order"]), [
            "não chamou quote", "não chamou nenhuma de quote, get_product", "chamou get_order",
            "faltou '2.199,00'", "não devia conter 'gf-3d'"])

    def test_case_files_are_valid(self):
        for name in ["cases.jsonl", "edge_cases.jsonl"]:
            cases = load_cases(ROOT / "evals" / name)
            self.assertGreaterEqual(len(cases), 12, name)
            self.assertEqual(len({c["id"] for c in cases}), len(cases))
            for c in cases:
                self.assertTrue(c["messages"])


class MainOnlyMissingIdTest(unittest.TestCase):
    def test_only_missing_id_is_an_error(self):
        self.assertNotEqual(main(["--only", "nao-existe"], backend=_BoomBackend()), 0)


class RunCaseEventsTest(unittest.TestCase):
    def test_writes_eval_session_log_with_header_and_labels(self):
        case = {"id": "caso-1", "messages": ["oi"]}
        events_dir = Path(tempfile.mkdtemp())
        run_case(case, FakeBackend(text("Oi! Como posso ajudar?")), events_dir=events_dir)

        path = events_dir / "caso-1.jsonl"
        self.assertTrue(path.exists())
        events = read_events(path)
        self.assertEqual(events[0]["kind"], "header")
        self.assertEqual(events[0]["source"], "eval")
        self.assertEqual(events[-1]["kind"], "labels")
        self.assertIsNone(events[-1]["judge"])

    def test_header_now_override_is_the_effective_clock(self):
        events_dir = Path(tempfile.mkdtemp())
        run_case({"id": "a", "messages": ["oi"]}, FakeBackend(text("Oi!")), events_dir=events_dir)
        run_case({"id": "b", "messages": ["oi"], "now": "2025-12-26T10:00"}, FakeBackend(text("Oi!")), events_dir=events_dir)
        self.assertEqual(read_events(events_dir / "a.jsonl")[0]["now_override"], DEFAULT_NOW)
        self.assertEqual(read_events(events_dir / "b.jsonl")[0]["now_override"], "2025-12-26T10:00")

    def test_log_switched_off_mid_case_gets_no_end_or_labels(self):
        events_dir = Path(tempfile.mkdtemp())
        with mock.patch("evals.run_evals.EventLog.write", side_effect=[None, OSError("disk full")]) as write, \
                mock.patch("evals.run_evals.label_session") as label, \
                contextlib.redirect_stdout(io.StringIO()):
            replies, _, _ = run_case({"id": "c", "messages": ["oi"]}, FakeBackend(text("Oi!")), events_dir=events_dir)
        self.assertEqual(replies, ["Oi!"])
        self.assertEqual(write.call_count, 2)  # header, then the failed customer event; no `end`
        label.assert_not_called()

    def test_without_events_dir_writes_nothing(self):
        case = {"id": "caso-2", "messages": ["oi"]}
        events_dir = Path(tempfile.mkdtemp())
        run_case(case, FakeBackend(text("Oi! Como posso ajudar?")))
        self.assertEqual(list(events_dir.iterdir()), [])

    def test_label_session_failure_does_not_break_the_case(self):
        case = {"id": "caso-3", "messages": ["oi"]}
        events_dir = Path(tempfile.mkdtemp())
        with mock.patch("evals.run_evals.label_session", side_effect=RuntimeError("boom")):
            replies, called, session = run_case(
                case, FakeBackend(text("Oi! Como posso ajudar?")), events_dir=events_dir)
        self.assertEqual(replies, ["Oi! Como posso ajudar?"])
        self.assertEqual(called, [])


class CostTest(unittest.TestCase):
    def test_record_carries_the_case_spend(self):
        _, _, session = run_case({"id": "x", "messages": ["oi"]}, FakeBackend(priced(text("Oi!"), 0.004, Usage(5, 6, 7, 8))))
        record = build_record({"id": "x", "messages": ["oi"]}, "cases", [], ["Oi!"], [], session)
        self.assertEqual(record["cost_usd"], 0.004)
        self.assertEqual(record["tokens"], {"input": 5, "output": 6, "cache_read": 7, "cache_write": 8})
        self.assertEqual((record["ok"], record["failures"], record["replies"]), (True, [], ["Oi!"]))

    def test_unpriced_case_has_null_cost(self):
        _, _, session = run_case({"id": "x", "messages": ["oi"]}, FakeBackend(text("Oi!")))
        self.assertIsNone(build_record({"id": "x", "messages": ["oi"]}, "cases", [], ["Oi!"], [], session)["cost_usd"])

    def test_cost_line(self):
        records = [{"cost_usd": 0.01, "messages": 2}, {"cost_usd": 0.02, "messages": 1}]
        self.assertEqual(cost_line(records), "custo: US$ 0.0300 em 3 mensagens (US$ 0.0100 por mensagem)")

    def test_cost_line_with_an_unpriced_case(self):
        records = [{"cost_usd": 0.01, "messages": 2}, {"cost_usd": None, "messages": 1}]
        self.assertEqual(cost_line(records), "custo: desconhecido (1 caso sem preço)")


def priced(turn, cost, usage=None):
    turn.cost_usd = cost
    if usage is not None:
        turn.usage = usage
    return turn


if __name__ == "__main__":
    unittest.main()
