import unittest

from evals.run_evals import evaluate, load_cases, main
from src.config import ROOT


class _BoomBackend:
    def call(self, *args, **kwargs):
        raise AssertionError("backend should not be called")


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


if __name__ == "__main__":
    unittest.main()
