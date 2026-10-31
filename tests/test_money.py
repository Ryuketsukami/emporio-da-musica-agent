import unittest

from src.money import QuoteLine, apply_discount, brl, build_quote, installment_value, max_installments

GD20 = QuoteLine(95, "Takamine GD20 Dreadnought Natural", 1, 219900, None, None, False)
TAYLOR = QuoteLine(94, "Taylor 110e Dreadnought Natural", 1, 599900, 8, "Desconto Exclusivo", False)
C40 = QuoteLine(81, "Yamaha C40 Nylon Natural", 1, 59990, None, None, False)
MEMPHIS = QuoteLine(83, "Tagima Memphis AC-39 Nylon Natural", 1, 42990, None, None, False)
DRUM = QuoteLine(139, "Bateria Acústica Yamaha Kit 1 Studio", 1, 443900, None, None, True)


def line(base, **kw):
    return QuoteLine(**{**base.__dict__, **kw})


class FormatTest(unittest.TestCase):
    def test_brl(self):
        self.assertEqual([brl(219900), brl(5999), brl(100000000), brl(0)],
                         ["R$ 2.199,00", "R$ 59,99", "R$ 1.000.000,00", "R$ 0,00"])

    def test_apply_discount_half_up(self):
        self.assertEqual((apply_discount(599900, 8), apply_discount(219900, 5), apply_discount(59990, 5)),
                         (551908, 208905, 56991))


class InstallmentTest(unittest.TestCase):
    def test_ladder(self):
        cases = {4999: 1, 10000: 2, 15000: 3, 31999: 3, 32000: 4, 48000: 6, 69999: 6, 70000: 7, 120000: 12, 219900: 12}
        for total, n in cases.items():
            self.assertEqual(max_installments(total), n, total)

    def test_value(self):
        self.assertEqual((installment_value(219900, 12), installment_value(100000, 3)), (18325, 33333))


class QuoteTest(unittest.TestCase):
    def test_single_regular_item(self):
        q = build_quote([GD20], local=None)
        self.assertEqual(q["subtotal"], "R$ 2.199,00")
        self.assertEqual(q["pix"]["total"], "R$ 2.089,05")
        self.assertEqual(q["cartao_credito"], {"max_parcelas": 12, "valor_parcela": "R$ 183,25", "sem_juros": True})
        self.assertTrue(q["pagamento_combinado_permitido"])
        self.assertEqual(q["frete"], {"situacao": "informe a cidade de entrega para calcular o frete"})

    def test_promo_line_shows_original_and_skips_pix(self):
        q = build_quote([TAYLOR], local=None)
        self.assertEqual(q["itens"][0]["promocao"], {"preco_original": "R$ 5.999,00", "desconto": "8%",
                                                     "preco_promocional": "R$ 5.519,08", "campanha": "Desconto Exclusivo"})
        self.assertEqual(q["itens"][0]["total_item"], "R$ 5.519,08")
        self.assertEqual(q["pix"]["total"], "R$ 5.519,08")

    def test_mixed_cart_pix_only_on_regular_items(self):
        q = build_quote([TAYLOR, C40], local=None)
        self.assertEqual(q["subtotal"], "R$ 6.118,98")
        self.assertEqual(q["pix"]["total"], "R$ 6.088,99")

    def test_quantity(self):
        self.assertEqual(build_quote([line(C40, qty=2)], local=None)["itens"][0]["total_item"], "R$ 1.199,80")

    def test_local_shipping(self):
        self.assertEqual(build_quote([C40], local=True)["frete"],
                         {"valor": "grátis", "prazo": "1 a 3 dias úteis", "modalidade": "motoboy próprio"})
        self.assertEqual(build_quote([MEMPHIS], local=True)["frete"]["valor"], "R$ 35,00")
        self.assertEqual(build_quote([line(MEMPHIS, unit_cents=50000)], local=True)["frete"]["valor"], "R$ 35,00")

    def test_other_city(self):
        self.assertEqual(build_quote([GD20], local=False)["frete"], {
            "valor": "calculado pelo CEP no fechamento",
            "opcoes": ["PAC (Correios): 5 a 12 dias úteis", "SEDEX (Correios): 2 a 5 dias úteis",
                       "Jadlog (.package): 3 a 8 dias úteis"],
        })

    def test_special_freight_other_city_only(self):
        self.assertEqual(build_quote([DRUM], local=False)["frete"]["valor"], "frete especial com cotação individual")
        self.assertEqual(build_quote([DRUM], local=True)["frete"]["valor"], "grátis")

    def test_combined_payment_threshold(self):
        self.assertFalse(build_quote([line(C40, unit_cents=200000)], local=None)["pagamento_combinado_permitido"])
        self.assertTrue(build_quote([line(C40, unit_cents=200001)], local=None)["pagamento_combinado_permitido"])
