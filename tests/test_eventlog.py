import json
import tempfile
import unittest
from pathlib import Path

from src.data import load_store
from src.eventlog import EventLog, Pseudonymizer, append_event, public_contacts, read_events
from src.policies import load_policies
from tests.test_session import AFTER_PHONE, LOOSE_PHONES, NOT_PHONES


class PseudonymizerTest(unittest.TestCase):
    def test_same_value_same_tag_different_value_new_tag(self):
        p = Pseudonymizer()
        self.assertEqual(p.scrub("a@x.com e b@x.com e A@X.com"), "<EMAIL_1> e <EMAIL_2> e <EMAIL_1>")
        self.assertEqual(p.scrub("fone (67) 99812-3456 ou 99812-3456"), "fone <PHONE_1> ou <PHONE_1>")
        self.assertEqual(p.scrub("outro (67) 99654-7821"), "outro <PHONE_2>")

    def test_public_contacts_stay(self):
        p = Pseudonymizer(keep_phones=frozenset({"33414444"}), keep_emails=frozenset({"contato@emporiodamusica.com.br"}))
        self.assertEqual(p.scrub("Liga (67) 3341-4444 ou contato@emporiodamusica.com.br"),
                         "Liga (67) 3341-4444 ou contato@emporiodamusica.com.br")

    def test_scrub_obj_recurses(self):
        p = Pseudonymizer()
        obj = {"input": {"email_or_phone": "lucas.mendes@jmail.com"}, "list": ["x@y.com", 3, None], "n": 4}
        self.assertEqual(p.scrub_obj(obj), {"input": {"email_or_phone": "<EMAIL_1>"}, "list": ["<EMAIL_2>", 3, None], "n": 4})

    def test_loose_phone_formats_get_one_tag(self):
        for raw in ["(67) 9 9812-3456", "67 9 9812-3456", "9 9812-3456", "998123456", "99812 3456", "067998123456"]:
            p = Pseudonymizer()
            self.assertEqual(p.scrub(f"meu fone: {raw}."), "meu fone: <PHONE_1>.", raw)
        p = Pseudonymizer()
        self.assertEqual(p.scrub("(67) 99812-3456 e (67) 9 9812-3456"), "<PHONE_1> e <PHONE_1>")

    def test_loose_phones_are_tagged_before_any_punctuation(self):
        for raw in LOOSE_PHONES:
            for after in AFTER_PHONE:
                self.assertEqual(Pseudonymizer().scrub(f"meu número é {raw}{after}"), f"meu número é <PHONE_1>{after}",
                                 raw + after)
        p = Pseudonymizer()
        self.assertEqual(p.scrub("+55 67 9 9812-3456, ou (67) 99812-3456"), "<PHONE_1>, ou <PHONE_1>")

    def test_phone_look_alikes_are_not_tagged(self):
        for text in NOT_PHONES:
            self.assertEqual(Pseudonymizer().scrub(text), text)

    def test_loose_phone_keeps_public_numbers_and_non_phones(self):
        p = Pseudonymizer(keep_phones=frozenset({"33414444"}))
        for text in ["Liga 67 3341 4444", "Protocolo EM-20261031-0001", "sessão 20251226-100000-81be",
                     "R$ 1.234.567,89", "entrega 22/12/2025", "agora 2025-12-26T10:00", "pedido 4, R$ 799,90",
                     "código BRQM7890123BR"]:
            self.assertEqual(p.scrub(text), text)

    def test_email_or_phone_value_is_always_tagged(self):
        p = Pseudonymizer(keep_phones=frozenset({"98123456"}))
        obj = {"email_or_phone": "9 98123456"}
        self.assertEqual(p.scrub_obj(obj), {"email_or_phone": "<PHONE_1>"})
        self.assertEqual(p.scrub_obj({"email_or_phone": " Lucas.Mendes@jmail.com"}), {"email_or_phone": "<EMAIL_1>"})
        self.assertEqual(p.scrub("lucas.mendes@jmail.com"), "<EMAIL_1>")

    def test_contact_free_value_stays(self):
        p = Pseudonymizer()
        self.assertEqual(p.scrub_obj({"email_or_phone": ""}), {"email_or_phone": ""})
        self.assertEqual(p.scrub_obj({"email_or_phone": "não informado"}), {"email_or_phone": "não informado"})
        self.assertEqual(p.scrub_obj({"email_or_phone": "(67) 9 9812-3456"}), {"email_or_phone": "<PHONE_1>"})

    def test_scrub_obj_scrubs_keys(self):
        self.assertEqual(Pseudonymizer().scrub_obj({"lucas.mendes@jmail.com": 1}), {"<EMAIL_1>": 1})

    def test_public_contacts_from_data(self):
        phones, emails = public_contacts(load_policies(), load_store())
        self.assertTrue({"33414444", "33214500", "33251180"} <= phones)
        self.assertIn("contato@emporiodamusica.com.br", emails)


class EventLogTest(unittest.TestCase):
    def test_writes_scrubbed_lines_immediately(self):
        path = Path(tempfile.mkdtemp()) / "s" / "x.jsonl"
        log = EventLog(path, Pseudonymizer())
        log.write("customer", text="meu e-mail é lucas.mendes@jmail.com")
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)  # flushed before the next write
        log.write("tool", name="get_order", input={"order_id": 4, "email_or_phone": "lucas.mendes@jmail.com"}, result={"pedido": 4})
        events = read_events(path)
        self.assertEqual([e["kind"] for e in events], ["customer", "tool"])
        self.assertEqual(events[0]["text"], "meu e-mail é <EMAIL_1>")
        self.assertEqual(events[1]["input"]["email_or_phone"], "<EMAIL_1>")
        self.assertRegex(events[0]["t"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        self.assertNotIn("lucas", path.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8").splitlines()[1])["kind"], "tool")


class ReadEventsTest(unittest.TestCase):
    def test_skips_lines_that_are_not_json(self):
        path = Path(tempfile.mkdtemp()) / "x.jsonl"
        path.write_text('{"kind": "header"}\n{"kind": "cust\n\n{"kind": "end"}\n', encoding="utf-8")
        self.assertEqual([e["kind"] for e in read_events(path)], ["header", "end"])


class AppendEventTest(unittest.TestCase):
    def test_writes_line_with_t_and_kind(self):
        path = Path(tempfile.mkdtemp()) / "s" / "x.jsonl"
        append_event(path, "labels", {"rubric_version": "1"})
        events = read_events(path)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "labels")
        self.assertEqual(events[0]["rubric_version"], "1")
        self.assertRegex(events[0]["t"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
