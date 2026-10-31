import unittest

from src.guard import check, money_cents


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
