import tempfile
import unittest
from pathlib import Path

from src.llm import Usage
from src.pricing import add_cost, add_usage, call_cost, load_prices

PRICES = {"claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.2}}


class CallCostTests(unittest.TestCase):
    def test_each_token_type_has_its_own_rate(self):
        usage = Usage(input=1_000_000, output=1_000_000, cache_read=1_000_000, cache_write=1_000_000)
        self.assertAlmostEqual(call_cost("claude-sonnet-5", usage, PRICES), 2.0 + 10.0 + 0.2 + 2.5)

    def test_small_call(self):
        usage = Usage(input=1000, output=200, cache_read=5000)
        self.assertAlmostEqual(call_cost("claude-sonnet-5", usage, PRICES), 0.002 + 0.002 + 0.001)

    def test_dated_model_id_uses_its_base_entry(self):
        usage = Usage(input=1_000_000)
        self.assertAlmostEqual(call_cost("claude-sonnet-5-20260101", usage, PRICES), 2.0)

    def test_unknown_or_missing_model_has_no_cost(self):
        usage = Usage(input=10)
        self.assertIsNone(call_cost("claude-sonnet-50", usage, PRICES))
        self.assertIsNone(call_cost("gpt-x", usage, PRICES))
        self.assertIsNone(call_cost(None, usage, PRICES))


class SumTests(unittest.TestCase):
    def test_add_usage(self):
        total = add_usage(Usage(1, 2, 3, 4), Usage(10, 20, 30, 40))
        self.assertEqual(total, Usage(11, 22, 33, 44))

    def test_add_cost_is_unknown_once_any_part_is(self):
        self.assertAlmostEqual(add_cost(0.5, 0.25), 0.75)
        self.assertIsNone(add_cost(0.5, None))
        self.assertIsNone(add_cost(None, 0.5))


class LoadPricesTests(unittest.TestCase):
    def test_shipped_table_has_the_default_and_judge_models(self):
        prices = load_prices()
        self.assertEqual(prices["claude-sonnet-5"],
                         {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.2})
        self.assertIn("claude-haiku-4-5", prices)

    def test_entry_missing_a_rate_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pricing.toml"
            path.write_text("[m]\ninput = 1\noutput = 2\ncache_read = 0.1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_prices(path)


if __name__ == "__main__":
    unittest.main()
