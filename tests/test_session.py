import json
import re
import tempfile
import unittest
from pathlib import Path

from src.clock import parse_now
from src.session import (MAX_SUMMARY, TicketLimitError, append_ticket, clean_text, mask, mask_email, mask_phone,
                         new_session, save_transcript, transcript_markdown)

NOW = parse_now("2026-11-02T10:00")


class MaskTest(unittest.TestCase):
    def test_email_and_phone(self):
        self.assertEqual(mask_email("lucas.mendes@jmail.com"), "l***@jmail.com")
        self.assertEqual(mask_phone("(67) 99812-3456"), "(67) 9****-3456")

    def test_mask_text_keeps_public_numbers(self):
        text = "email lucas.mendes@jmail.com, fone (67) 99812-3456, loja (67) 3341-4444"
        self.assertEqual(mask(text, keep_phones=frozenset({"33414444"})),
                         "email l***@jmail.com, fone (67) 9****-3456, loja (67) 3341-4444")

    def test_wide_phone_forms_are_masked(self):
        cases = {"67 99123-4567": "67 9****-4567", "67991234567": "679****4567", "(67)999998888": "(67)9****8888",
                 "(67) 99123 4567": "(67) 9**** 4567", "99812-3456": "998**-3456"}
        for raw, masked in cases.items():
            self.assertEqual(mask(f"fone {raw}."), f"fone {masked}.", raw)
        self.assertEqual(mask("CEP 79202-333, R$ 2.199,00, 29/12/2025"), "CEP 79202-333, R$ 2.199,00, 29/12/2025")
        self.assertEqual(mask("fone +5567999998888."), "fone +556******8888.")
        self.assertEqual(mask("CEP 79202333, ano 2024-2025"), "CEP 79202333, ano 2024-2025")

    def test_clean_text(self):
        self.assertEqual(clean_text("a\x00b\x1b[31mc\n d", 500), "ab[31mc\n d")
        self.assertEqual(len(clean_text("x" * 600, MAX_SUMMARY)), 500)


class SessionTest(unittest.TestCase):
    def test_new_session_id(self):
        self.assertRegex(new_session(NOW).id, r"^20261102-100000-[0-9a-f]{4}$")

    def test_tickets_capped_at_three(self):
        path = Path(tempfile.mkdtemp()) / "tickets.jsonl"
        s = new_session(NOW)
        rec = append_ticket(s, "complaint", "violão\x00 rachado", NOW, path)
        self.assertRegex(rec["protocolo"], r"^EM-20261102-\d{4}$")
        self.assertEqual((rec["tipo"], rec["resumo"], rec["sessao"]), ("complaint", "violão rachado", s.id))
        append_ticket(s, "human", "b", NOW, path)
        append_ticket(s, "human", "c", NOW, path)
        with self.assertRaises(TicketLimitError):
            append_ticket(s, "human", "d", NOW, path)
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 3)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8").splitlines()[0])["protocolo"], rec["protocolo"])

    def test_transcript_is_masked(self):
        s = new_session(NOW)
        s.note("cliente", "pedido 4, lucas.mendes@jmail.com")
        s.note("ferramenta", "get_order {\"order_id\": 4} → entregue")
        s.note("duda", "Achei, Lucas!")
        md = transcript_markdown(s, "Devolução")
        self.assertTrue(md.startswith("# Devolução"))
        self.assertIn("**Cliente:** pedido 4, l***@jmail.com", md)
        self.assertIn("> ferramenta: get_order", md)
        self.assertIn("**Duda:** Achei, Lucas!", md)
        self.assertNotIn("lucas.mendes", md)
        out = save_transcript(s, "Devolução", Path(tempfile.mkdtemp()) / "x" / "t.md")
        self.assertEqual(out.read_text(encoding="utf-8"), md)
