import json
import tempfile
import unittest
from pathlib import Path

from src.data import load_store
from src.eventlog import EventLog, Pseudonymizer, public_contacts, read_events
from src.policies import load_policies


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
