import re
import unittest

from src.config import DATA_DIR
from src.policies import SERVED_SECTIONS, load_policies

try:
    import pypdf  # noqa: F401
except ImportError:
    pypdf = None

P = load_policies()


def squash(text):
    text = re.sub(r"[​•‣●•]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class PoliciesTest(unittest.TestCase):
    def test_sections(self):
        self.assertEqual(set(P), {"sobre", "horario", "pagamento", "trocas_devolucoes", "frete", "promocoes",
                                  "atendimento", "garantia", "privacidade", "disposicoes"})
        self.assertEqual(SERVED_SECTIONS, ("sobre", "horario", "pagamento", "trocas_devolucoes", "frete",
                                           "promocoes", "garantia", "privacidade"))

    def test_key_facts(self):
        self.assertIn("Rua 14 de Maio, 3200", P["sobre"])
        self.assertIn("(67) 3341-4444", P["sobre"])
        self.assertIn("contato@emporiodamusica.com.br", P["sobre"])
        self.assertNotIn("empóriodamusica", "".join(P.values()))
        self.assertIn("09:00 às 13:00", P["horario"])
        self.assertIn("7 (sete) dias corridos", P["trocas_devolucoes"])
        self.assertIn("(67) 3321-4500", P["atendimento"])
        self.assertIn("R$ 500,00", P["frete"])
        self.assertTrue(P["sobre"].startswith("## 1. Sobre a Empório da Música"))

    @unittest.skipIf(pypdf is None, "pypdf not installed")
    def test_every_fragment_is_in_the_pdf(self):
        from scripts.pdf_text import extract_text
        pdf = squash(extract_text(DATA_DIR / "políticas_da_loja.pdf"))
        for line in (DATA_DIR / "policies.md").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or re.fullmatch(r"\|?[\s:|-]+\|?", line):
                continue
            line = re.sub(r"^(#{2,3}|-|\d+\.)\s+", "", line)
            for fragment in (c for c in line.split("|") if c.strip()):
                self.assertIn(squash(fragment.replace("**", "")), pdf, fragment)
