import unittest

from src import clock
from src.data import load_store
from src.policies import SERVED_SECTIONS, load_policies
from src.prompt import system_prompt, user_turn

STORE, POLICIES = load_store(), load_policies()


class PromptTest(unittest.TestCase):
    def test_contents(self):
        sp = system_prompt(POLICIES, STORE)
        self.assertIn("Duda", sp)
        self.assertIn(POLICIES["atendimento"], sp)
        for key in SERVED_SECTIONS:
            self.assertIn(f"- {key}:", sp)
        for c in STORE.categories.values():
            self.assertIn(c.name, sp)
        self.assertIn("português do Brasil", sp)
        self.assertNotIn("{", sp)  # no unfilled template fields

    def test_is_stable(self):
        self.assertEqual(system_prompt(POLICIES, STORE), system_prompt(POLICIES, STORE))  # cacheable

    def test_user_turn(self):
        t = clock.parse_now("2026-11-02T10:00")
        self.assertEqual(user_turn("oi", t), clock.time_line(t) + "\noi")
