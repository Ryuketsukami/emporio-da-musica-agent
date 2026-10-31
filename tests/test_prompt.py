import unittest

from src import clock
from src.data import load_store
from src.policies import SERVED_SECTIONS, load_policies
from src.prompt import load_prompt_flags, system_prompt, user_turn

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
        self.assertIn("A linha [agora: ...] é a data e a hora oficiais desta conversa", sp)

    def test_rules_added_in_the_fix_wave(self):
        sp = system_prompt(POLICIES, STORE)
        self.assertIn("há mais opções", sp)
        self.assertIn("Não escreva texto junto com chamadas de ferramenta", sp)
        self.assertIn("previsão de entrega", sp)

    def test_is_stable(self):
        self.assertEqual(system_prompt(POLICIES, STORE), system_prompt(POLICIES, STORE))  # cacheable

    def test_user_turn(self):
        t = clock.parse_now("2026-11-02T10:00")
        self.assertEqual(user_turn("oi", t), clock.time_line(t) + "\noi")


class PromptFlagsTest(unittest.TestCase):
    NAMES = ("call_tools_directly", "quote_does_all_math", "quick_human_handoff", "register_complaints_directly")

    def test_flag_file(self):
        flags = load_prompt_flags()
        self.assertEqual(tuple(flags), self.NAMES)
        for name, flag in flags.items():
            self.assertTrue(flag["text"].strip(), name)
            self.assertTrue(flag["why"].strip(), name)

    def test_no_flags_means_no_extra_section(self):
        base = system_prompt(POLICIES, STORE)
        self.assertEqual(base, system_prompt(POLICIES, STORE, ()))
        self.assertNotIn("# Regras adicionais", base)

    def test_flags_append_in_file_order(self):
        flags = load_prompt_flags()
        sp = system_prompt(POLICIES, STORE, ("quote_does_all_math", "call_tools_directly"))
        self.assertTrue(sp.startswith(system_prompt(POLICIES, STORE)))
        self.assertIn("# Regras adicionais", sp)
        first = sp.index(flags["call_tools_directly"]["text"])
        second = sp.index(flags["quote_does_all_math"]["text"])
        self.assertLess(first, second)
        self.assertNotIn(flags["quick_human_handoff"]["text"], sp)
        self.assertEqual(sp, system_prompt(POLICIES, STORE, ("call_tools_directly", "quote_does_all_math")))

    def test_unknown_flag_lists_valid_names(self):
        with self.assertRaises(ValueError) as ctx:
            system_prompt(POLICIES, STORE, ("tool_hasty",))
        for name in self.NAMES:
            self.assertIn(name, str(ctx.exception))

