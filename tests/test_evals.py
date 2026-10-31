import tempfile
import unittest
from pathlib import Path

from evals.run_evals import evaluate, load_cases, main, run_case
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

    def test_without_events_dir_writes_nothing(self):
        case = {"id": "caso-2", "messages": ["oi"]}
        events_dir = Path(tempfile.mkdtemp())
        run_case(case, FakeBackend(text("Oi! Como posso ajudar?")))
        self.assertEqual(list(events_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
