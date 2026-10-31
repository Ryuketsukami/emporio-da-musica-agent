import json
import tempfile
import unittest
from pathlib import Path

from src.clock import parse_now
from src.data import load_store
from src.policies import load_policies
from src.session import new_session
from src.tools import TOOLS, ToolContext, run_tool

STORE, POLICIES = load_store(), load_policies()


def ctx(now="2026-11-02T10:00"):
    t = parse_now(now)
    return ToolContext(STORE, POLICIES, new_session(t), t, Path(tempfile.mkdtemp()) / "tickets.jsonl")


class SchemaTest(unittest.TestCase):
    def test_names_and_shape(self):
        self.assertEqual([t["name"] for t in TOOLS], ["search_products", "get_product", "quote",
                         "list_active_promotions", "get_order", "read_policy", "find_partner_stores", "register_ticket"])
        for t in TOOLS:
            self.assertTrue(t["description"].strip())
            self.assertEqual((t["input_schema"]["type"], t["input_schema"]["additionalProperties"]), ("object", False))

    def test_validation(self):
        c = ctx()
        self.assertEqual(run_tool("rm_rf", {}, c), {"erro": "Ferramenta desconhecida: rm_rf"})
        self.assertEqual(run_tool("get_product", {"id_or_name": "x", "path": "/etc"}, c), {"erro": "Parâmetro desconhecido: path"})
        self.assertEqual(run_tool("get_product", {}, c), {"erro": "Parâmetro obrigatório ausente: id_or_name"})
        self.assertEqual(run_tool("quote", {"items": [{"product_id": 95, "qty": 11}]}, c), {"erro": "Valor inválido para items"})
        self.assertEqual(run_tool("get_order", {"order_id": True, "email_or_phone": "x"}, c), {"erro": "Valor inválido para order_id"})
        self.assertEqual(run_tool("read_policy", {"section": "../../etc/passwd"}, c), {"erro": "Valor inválido para section"})
        self.assertEqual(run_tool("read_policy", {"section": "atendimento"}, c), {"erro": "Valor inválido para section"})


class CatalogToolsTest(unittest.TestCase):
    def test_search(self):
        r = run_tool("search_products", {"text": "violão", "max_price": 1000}, ctx())
        self.assertEqual((r["total_encontrado"], len(r["resultados"])), (12, 10))
        self.assertEqual(r["resultados"][0], {"id": 83, "nome": "Tagima Memphis AC-39 Nylon Natural", "categoria": "Violões",
                                              "preco": "R$ 429,90", "promocao": None, "situacao": "disponível"})

    def test_search_empty_category(self):
        r = run_tool("search_products", {"category": "Instrumentos de Sopro (Metais)"}, ctx())
        self.assertEqual(r["resultados"], [])
        self.assertEqual(r["observacao"], "No momento não temos modelos dessa categoria.")

    def test_get_product_regular(self):
        r = run_tool("get_product", {"id_or_name": "Takamine GD20"}, ctx())
        self.assertEqual((r["id"], r["preco"], r["pix"], r["parcelamento"], r["situacao"]),
                         (95, "R$ 2.199,00", "R$ 2.089,05", "até 12x de R$ 183,25 sem juros", "disponível"))
        self.assertIn("especificacoes", r)
        self.assertNotIn("alternativas", r)

    def test_get_product_out_of_stock_offers_alternatives(self):
        r = run_tool("get_product", {"id_or_name": "Giannini GF-3D"}, ctx())
        self.assertEqual(r["situacao"], "temporariamente indisponível")
        self.assertEqual([a["id"] for a in r["alternativas"]], [107, 82, 86])
        self.assertNotIn("pix", r)

    def test_get_product_promo_has_no_pix_discount(self):
        r = run_tool("get_product", {"id_or_name": "Taylor 110e"}, ctx())
        self.assertEqual(r["promocao"]["preco_promocional"], "R$ 5.519,08")
        self.assertEqual(r["pix"], "R$ 5.519,08")
        self.assertEqual(r["observacao_pix"], "O desconto de 5% no PIX não vale sobre preço promocional.")
        self.assertEqual(r["parcelamento"], "até 12x de R$ 459,92 sem juros")

    def test_get_product_unknown(self):
        self.assertEqual(run_tool("get_product", {"id_or_name": "stradivarius violino"}, ctx())["erro"], "Produto não encontrado")

    def test_quote(self):
        r = run_tool("quote", {"items": [{"product_id": 95, "qty": 1}], "city": "Dourados"}, ctx())
        self.assertEqual((r["subtotal"], r["frete"]["valor"]), ("R$ 2.199,00", "calculado pelo CEP no fechamento"))
        self.assertEqual(run_tool("quote", {"items": [{"product_id": 130, "qty": 1}]}, ctx()),
                         {"erro": "Kala KA-ABP-CTG Barítono Cedar ainda não está à venda (em breve)."})
        self.assertEqual(run_tool("quote", {"items": [{"product_id": 93, "qty": 3}]}, ctx()),
                         {"erro": "Não temos 3 unidades de Martin D-28 Dreadnought Natural."})

    def test_promotions(self):
        r = run_tool("list_active_promotions", {}, ctx())
        self.assertEqual([p["id"] for p in r["promocoes"]], [90, 94, 121, 127])
        self.assertEqual(r["promocoes"][2], {"id": 121, "produto": "Ohana CK-20 Concert Natural", "preco_original": "R$ 549,00",
                                             "desconto": "20%", "preco_promocional": "R$ 439,20", "campanha": "Desconto Exclusivo"})


class OrderToolTest(unittest.TestCase):
    def test_found_by_phone_without_contact_data(self):
        r = run_tool("get_order", {"order_id": 8, "email_or_phone": "(67) 99654-7821"}, ctx())
        self.assertEqual((r["pedido"], r["cliente"], r["situacao"], r["codigo_rastreio"], r["total"], r["pagamento"]),
                         (8, "Ana", "enviado", "BRJL5544332BR", "R$ 349,90", "cartão de débito"))
        self.assertEqual(r["itens"], [{"produto": "Kala KA-C Concert Mogno", "qtd": 1}])
        dumped = json.dumps(r, ensure_ascii=False)
        self.assertNotIn("@", dumped)
        self.assertNotIn("99654", dumped)
        self.assertNotIn("Carolina", dumped)

    def test_email_case_and_phone_without_ddd(self):
        self.assertEqual(run_tool("get_order", {"order_id": 4, "email_or_phone": "LUCAS.MENDES@JMAIL.COM"}, ctx())["pedido"], 4)
        self.assertEqual(run_tool("get_order", {"order_id": 4, "email_or_phone": "99812-3456"}, ctx())["pedido"], 4)

    def test_three_misses_lock_the_tool(self):
        c = ctx()
        miss = run_tool("get_order", {"order_id": 4, "email_or_phone": "x@y.com"}, c)
        self.assertEqual(miss["tentativas_restantes"], 2)
        self.assertEqual(run_tool("get_order", {"order_id": 999, "email_or_phone": "x@y.com"}, c)["erro"], miss["erro"])
        run_tool("get_order", {"order_id": 4, "email_or_phone": "z@y.com"}, c)
        locked = run_tool("get_order", {"order_id": 4, "email_or_phone": "lucas.mendes@jmail.com"}, c)
        self.assertNotIn("pedido", locked)
        self.assertIn("bloqueada", locked["erro"])

    def test_return_windows(self):
        r = run_tool("get_order", {"order_id": 4, "email_or_phone": "lucas.mendes@jmail.com"}, ctx("2025-12-26T10:00"))
        self.assertEqual(r["opcoes_de_devolucao"], {
            "data_recebimento_considerada": "22/12/2025",
            "observacao": "Usamos a previsão de entrega como data de recebimento.",
            "arrependimento": {"prazo_ate": "29/12/2025", "dentro_do_prazo": True},
            "troca_por_preferencia": {"prazo_ate": "29/12/2025", "dentro_do_prazo": True},
            "troca_por_defeito": {"prazo_ate": "09/01/2026", "dentro_do_prazo": True},
            "garantia_legal": {"prazo_ate": "22/03/2026", "dentro_do_prazo": True},
        })
        late = run_tool("get_order", {"order_id": 4, "email_or_phone": "lucas.mendes@jmail.com"}, ctx("2026-01-10T10:00"))
        o = late["opcoes_de_devolucao"]
        self.assertEqual((o["arrependimento"]["dentro_do_prazo"], o["troca_por_defeito"]["dentro_do_prazo"],
                          o["garantia_legal"]["dentro_do_prazo"]), (False, False, True))

    def test_not_delivered_and_cancelled(self):
        self.assertNotIn("opcoes_de_devolucao", run_tool("get_order", {"order_id": 9, "email_or_phone": "(67) 99234-5678"}, ctx()))
        r = run_tool("get_order", {"order_id": 17, "email_or_phone": "matheus.correia@hayoo.com.br"}, ctx())
        self.assertEqual((r["situacao"], r["observacao"]), ("cancelado", "Pedido cancelado pelo cliente antes do processamento"))


class OtherToolsTest(unittest.TestCase):
    def test_read_policy(self):
        self.assertEqual(run_tool("read_policy", {"section": "horario"}, ctx()), {"secao": "horario", "texto": POLICIES["horario"]})

    def test_partners(self):
        r = run_tool("find_partner_stores", {"accessory": "cordas"}, ctx())
        self.assertEqual(r["lojas"][0], {"nome": "Casa do Músico Acessórios",
                                         "vende": "cordas, palhetas, cabos, correias, afinadores",
                                         "endereco": "Rua Barão do Rio Branco 1520 - Centro", "cidade": "Campo Grande",
                                         "telefone": "(67) 3325-1180"})
        self.assertEqual(run_tool("find_partner_stores", {"accessory": "teremim"}, ctx())["lojas"], [])

    def test_ticket_open_closed_and_cap(self):
        c = ctx()
        r = run_tool("register_ticket", {"type": "complaint", "summary": "Violão chegou rachado"}, c)
        self.assertRegex(r["protocolo"], r"^EM-20261102-\d{4}$")
        self.assertEqual((r["prazo_retorno"], r["loja_aberta"]), ("até 24 horas úteis", True))
        self.assertNotIn("reabre", r)
        closed = run_tool("register_ticket", {"type": "human", "summary": "Quero um atendente"}, ctx("2026-11-01T20:00"))
        self.assertEqual((closed["loja_aberta"], closed["reabre"]), (False, "seg 02/11 às 09:00"))
        run_tool("register_ticket", {"type": "human", "summary": "b"}, c)
        run_tool("register_ticket", {"type": "human", "summary": "c"}, c)
        self.assertEqual(run_tool("register_ticket", {"type": "human", "summary": "d"}, c),
                         {"erro": "Limite de chamados desta conversa atingido."})
