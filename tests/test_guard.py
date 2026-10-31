import unittest

from src.guard import check, money_cents
from src.session import PHONE_RE


class GuardTest(unittest.TestCase):
    def test_money_parsing(self):
        self.assertEqual(money_cents("R$ 2.199,00 e R$35 e R$ 1.000 e R$1000"), {219900, 3500, 100000})

    def test_money(self):
        self.assertTrue(check("Custa R$ 2.199,00.", '{"preco": "R$ 2.199,00"}').ok)
        v = check("Custa R$ 1.999,00.", '{"preco": "R$ 2.199,00"}')
        self.assertEqual((v.ok, v.problems), (False, ["o valor R$ 1.999,00 não veio de nenhuma consulta"]))
        self.assertTrue(check("Frete grátis acima de R$ 500", "acima de R$ 500,00").ok)

    def test_phone(self):
        self.assertTrue(check("Liga no (67) 3341-4444", "Telefone (67) 3341-4444").ok)
        self.assertTrue(check("Liga 3341-4444", "(67) 3341-4444").ok)
        self.assertEqual(check("Liga (67) 3333-0000", "").problems, ["o telefone (67) 3333-0000 não veio de nenhuma consulta"])

    def test_email_and_link(self):
        self.assertTrue(check("Escreve para Contato@EmporioDaMusica.com.br", "contato@emporiodamusica.com.br").ok)
        self.assertEqual(check("veja https://evil.example/x.", "").problems, ["o link https://evil.example/x não veio de nenhuma consulta"])

    def test_email_problem_text(self):
        v = check("Escreve para fake@golpe.com", "")
        self.assertEqual(v.problems, ["o e-mail fake@golpe.com não veio de nenhuma consulta"])

    def test_dates_codes_and_plain_text_pass(self):
        self.assertTrue(check("Previsão 29/12/2025, rastreio BRJL5544332BR, CEP 79202-333. Valeu!", "").ok)

    def test_problems_once_in_order(self):
        v = check("R$ 10,00 e R$ 20,00 e de novo R$ 10,00", "")
        self.assertEqual(v.problems, ["o valor R$ 10,00 não veio de nenhuma consulta", "o valor R$ 20,00 não veio de nenhuma consulta"])


class WideGuardTest(unittest.TestCase):
    def test_money_any_spacing_and_reais(self):
        self.assertEqual(money_cents("R$  2.199,00, R$\t35, 1.500 reais e 99,90 reais"), {219900, 3500, 150000, 9990})
        self.assertEqual(check("Sai por 1.999 reais.", '{"preco": "R$ 2.199,00"}').problems,
                         ["o valor R$ 1.999,00 não veio de nenhuma consulta"])
        self.assertTrue(check("Sai por 2.199 reais.", '{"preco": "R$ 2.199,00"}').ok)

    def test_phone_forms_are_found(self):
        for form in ["(67) 3341-4444", "3341-4444", "67 99999 8888", "(67)999998888", "67991234567", "(67) 99123 4567",
                     "67 3341.4444", "99812-3456"]:
            self.assertEqual([m.group() for m in PHONE_RE.finditer(f"liga {form} agora")], [form], form)

    def test_non_phones_are_ignored(self):
        text = ("Previsão 29/12/2025, total R$ 2.199,00, CEP 79202-333, CNPJ 12.345.678/0001-90, "
                "rastreio BRTP3456789BR, pedido 4, pedido 20, R$ 1.000, protocolo EM-20261102-0042, 2025-12-22.")
        self.assertEqual([m.group() for m in PHONE_RE.finditer(text)], [])
        self.assertTrue(check(text, "R$ 2.199,00 R$ 1.000,00").ok)

    def test_phone_with_ddd_must_match_ddd(self):
        allowed = "Telefone (67) 3341-4444"
        self.assertTrue(check("Liga 67 3341 4444", allowed).ok)
        self.assertTrue(check("Liga 3341-4444", allowed).ok)
        self.assertEqual(check("Liga (11) 3341-4444", allowed).problems,
                         ["o telefone (11) 3341-4444 não veio de nenhuma consulta"])
        self.assertFalse(check("Liga 67991234567", allowed).ok)

    def test_url_must_equal_an_allowed_url(self):
        allowed = "veja https://emporiodamusica.com.br/promo"
        self.assertTrue(check("Acesse https://emporiodamusica.com.br/promo.", allowed).ok)
        self.assertTrue(check("Acesse www.emporiodamusica.com.br/promo", allowed).ok)
        self.assertEqual(check("Acesse https://emporiodamusica.com.br/promo-falsa", allowed).problems,
                         ["o link https://emporiodamusica.com.br/promo-falsa não veio de nenhuma consulta"])
        self.assertFalse(check("Acesse https://emporiodamusica.com", allowed).ok)

    def test_bare_domains(self):
        self.assertEqual(check("Compre em emporio-promo.com.br!", "").problems,
                         ["o link emporio-promo.com.br não veio de nenhuma consulta"])
        allowed = "e-mail contato@emporiodamusica.com.br"
        self.assertTrue(check("Nosso site é emporiodamusica.com.br.", allowed).ok)
        self.assertTrue(check("Escreva para contato@emporiodamusica.com.br.", allowed).ok)
        self.assertEqual(check("Escreva para golpe@fake.com.br", allowed).problems,
                         ["o e-mail golpe@fake.com.br não veio de nenhuma consulta"])
