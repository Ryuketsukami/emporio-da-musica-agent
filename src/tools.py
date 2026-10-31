"""The eight agent tools: Anthropic tool schemas, input validation, and dispatch.

`run_tool` is the security boundary between the model and the store: every call is
validated against the same property table used to build the tool's `input_schema`
before any lookup happens, and the function never raises.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from src.clock import fmt_date, is_open, reopening_text
from src.config import TICKETS_PATH
from src.data import (Store, alternatives, availability, effective_price, find_partners, find_product,
                       is_available, is_local_city, needs_special_freight, rank, search)
from src.money import PIX_PCT, QuoteLine, apply_discount, brl, build_quote, installment_value, max_installments
from src.policies import SERVED_SECTIONS
from src.session import MAX_SUMMARY, Session, TicketLimitError, append_ticket

MAX_ORDER_MISSES = 3  # per chat, and per order number across chats within ORDER_LOCK_HOURS
ORDER_LOCK_HOURS = 24
SEARCH_LIMIT = 10

CATEGORY_NAMES = (
    "Guitarras", "Baixos", "Baterias e Percussão", "Teclados e Pianos", "Violões",
    "Instrumentos de Sopro (Madeiras)", "Instrumentos de Sopro (Metais)", "Cordas Orquestrais", "Ukuleles",
)

_STATUS_LABELS = {
    "pending": "aguardando pagamento",
    "confirmed": "confirmado, em separação",
    "shipped": "enviado",
    "delivered": "entregue",
    "cancelled": "cancelado",
}

_PAYMENT_LABELS = {"pix": "PIX", "debit": "cartão de débito", "boleto": "boleto bancário"}

_CREDIT_RE = re.compile(r"^credit_(\d+)x$")

_DIGITS_RE = re.compile(r"\D")


@dataclass
class ToolContext:
    store: Store
    policies: dict[str, str]
    session: Session
    now: datetime
    tickets_path: Path = TICKETS_PATH
    order_misses_path: Path | None = None  # None: no lock across chats (unit tests)


def _card(store: Store, product) -> dict:
    promo = store.active_promo(product.id)
    promocao = None
    if promo is not None:
        promocao = {
            "preco_original": brl(product.price_cents),
            "desconto": f"{promo.pct}%",
            "preco_promocional": brl(apply_discount(product.price_cents, promo.pct)),
            "campanha": promo.campaign,
        }
    return {
        "id": product.id,
        "nome": product.name,
        "categoria": product.category,
        "preco": brl(product.price_cents),
        "promocao": promocao,
        "situacao": availability(product),
    }


def _payment_label(payment: str) -> str:
    if payment in _PAYMENT_LABELS:
        return _PAYMENT_LABELS[payment]
    match = _CREDIT_RE.match(payment)
    if match:
        return f"cartão de crédito em {match.group(1)}x"
    return payment


def _contact_matches(contact: str, customer) -> bool:
    if "@" in contact:
        return contact.strip().lower() == customer.email.lower()
    digits = _DIGITS_RE.sub("", contact)
    if len(digits) < 8:
        return False
    customer_digits = _DIGITS_RE.sub("", customer.phone)
    return digits[-8:] == customer_digits[-8:]


def _return_windows(order, now: datetime) -> dict:
    receipt = order.estimated_delivery

    def window(deadline):
        return {"prazo_ate": fmt_date(deadline), "dentro_do_prazo": now.date() <= deadline}

    return {
        "data_recebimento_considerada": fmt_date(receipt),
        "observacao": "Usamos a previsão de entrega como data de recebimento.",
        "arrependimento": window(receipt + timedelta(days=7)),
        "troca_por_preferencia": window(receipt + timedelta(days=7)),
        "troca_por_defeito": window(order.date + timedelta(days=30)),
        "garantia_legal": window(receipt + timedelta(days=90)),
    }


def _search_products(args: dict, ctx: ToolContext) -> dict:
    text = args.get("text")
    category = args.get("category")
    min_price = args.get("min_price")
    max_price = args.get("max_price")
    include_unavailable = args.get("include_unavailable", False)
    min_cents = round(min_price * 100) if min_price is not None else None
    max_cents = round(max_price * 100) if max_price is not None else None

    products = search(ctx.store, text=text, category=category, min_cents=min_cents, max_cents=max_cents,
                       include_unavailable=include_unavailable)
    result = {"resultados": [_card(ctx.store, p) for p in products[:SEARCH_LIMIT]], "total_encontrado": len(products)}
    if len(products) > SEARCH_LIMIT:
        result["observacao"] = (f"Mostrando os {SEARCH_LIMIT} mais baratos de {len(products)}. "
                                "Diga ao cliente que há mais opções e ofereça filtrar.")
    if not products:
        if category is not None and not any(p.category == category for p in ctx.store.products.values()):
            result["observacao"] = "No momento não temos modelos dessa categoria."
        else:
            result["observacao"] = "Nenhum produto encontrado com esses critérios."
    return result


def _get_product(args: dict, ctx: ToolContext) -> dict:
    product = find_product(ctx.store, args["id_or_name"])
    if product is None:
        suggestions = [p for _, p in rank(ctx.store, args["id_or_name"], include_unavailable=True)[:3]]
        return {"erro": "Produto não encontrado", "sugestoes": [_card(ctx.store, p) for p in suggestions]}

    card = _card(ctx.store, product)
    card["descricao"] = product.description
    card["especificacoes"] = product.specs
    if is_available(product):
        price = effective_price(ctx.store, product)
        if ctx.store.active_promo(product.id) is not None:
            card["pix"] = brl(price)
            card["observacao_pix"] = "O desconto de 5% no PIX não vale sobre preço promocional."
        else:
            card["pix"] = brl(apply_discount(price, PIX_PCT))
        n = max_installments(price)
        card["parcelamento"] = "à vista" if n == 1 else f"até {n}x de {brl(installment_value(price, n))} sem juros"
    else:
        card["alternativas"] = [_card(ctx.store, a) for a in alternatives(ctx.store, product)]
    return card


def _quote(args: dict, ctx: ToolContext) -> dict:
    lines = []
    total_qty: dict[int, int] = {}
    for item in args["items"]:
        total_qty[item["product_id"]] = total_qty.get(item["product_id"], 0) + item["qty"]
    for item in args["items"]:
        product = ctx.store.products.get(item["product_id"])
        if product is None:
            return {"erro": f"Produto {item['product_id']} não encontrado."}
        if product.status == "coming_soon":
            return {"erro": f"{product.name} ainda não está à venda (em breve)."}
        if product.status == "discontinued":
            return {"erro": f"{product.name} está fora de linha."}
        if product.stock == 0:
            return {"erro": f"{product.name} está temporariamente indisponível."}
        qty = item["qty"]
        if total_qty[product.id] > product.stock:
            return {"erro": f"Não temos {total_qty[product.id]} unidades de {product.name}."}
        promo = ctx.store.active_promo(product.id)
        lines.append(QuoteLine(
            product_id=product.id, name=product.name, qty=qty, unit_cents=product.price_cents,
            promo_pct=promo.pct if promo else None, campaign=promo.campaign if promo else None,
            special_freight=needs_special_freight(product),
        ))
    city = args.get("city")
    local = None if city is None else is_local_city(city)
    return build_quote(lines, local)


def _list_active_promotions(args: dict, ctx: ToolContext) -> dict:
    product_ids = sorted({p.product_id for p in ctx.store.promotions if p.active})
    promocoes = []
    for product_id in product_ids:
        product = ctx.store.products[product_id]
        promo = ctx.store.active_promo(product_id)
        promocoes.append({
            "id": product.id,
            "produto": product.name,
            "preco_original": brl(product.price_cents),
            "desconto": f"{promo.pct}%",
            "preco_promocional": brl(apply_discount(product.price_cents, promo.pct)),
            "campanha": promo.campaign,
        })
    return {"promocoes": promocoes}


def record_order_miss(path: Path, order_id: int, now: datetime) -> None:
    """One line per failed lookup: the order number asked for and the time, never the contact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"pedido": order_id, "em": now.isoformat()}) + "\n")


def recent_order_misses(path: Path | None, order_id: int, now: datetime) -> int:
    """Failed lookups of this order number, from any chat, in the last ORDER_LOCK_HOURS."""
    if path is None or not path.is_file():
        return 0
    since = now - timedelta(hours=ORDER_LOCK_HOURS)
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
            if entry["pedido"] == order_id and since < datetime.fromisoformat(entry["em"]) <= now:
                count += 1
        except (ValueError, KeyError, TypeError):
            continue
    return count


def _attempts_left_notice(left: int) -> str:
    if left <= 0:
        return "Não restam tentativas: a consulta foi bloqueada. Ofereça encaminhar para um atendente."
    return "Resta 1 tentativa." if left == 1 else f"Restam {left} tentativas."


def _get_order(args: dict, ctx: ToolContext) -> dict:
    session = ctx.session
    order_id = args["order_id"]
    if session.order_misses >= MAX_ORDER_MISSES:
        return {"erro": "Consulta de pedidos bloqueada nesta conversa por segurança. "
                         "Ofereça encaminhar para um atendente."}
    if recent_order_misses(ctx.order_misses_path, order_id, ctx.now) >= MAX_ORDER_MISSES:
        return {"erro": f"Consulta deste pedido bloqueada por segurança depois de várias tentativas erradas. "
                         f"Ela volta em até {ORDER_LOCK_HOURS} horas. Ofereça encaminhar para um atendente."}

    order = ctx.store.orders.get(order_id)
    customer = ctx.store.customers.get(order.customer_id) if order is not None else None
    matched = order is not None and customer is not None and _contact_matches(args["email_or_phone"], customer)

    if not matched:
        session.order_misses += 1
        order_left = MAX_ORDER_MISSES
        if ctx.order_misses_path is not None:
            record_order_miss(ctx.order_misses_path, order_id, ctx.now)
            order_left -= recent_order_misses(ctx.order_misses_path, order_id, ctx.now)
        left = max(0, min(MAX_ORDER_MISSES - session.order_misses, order_left))
        return {
            "erro": "Não encontrei um pedido com esses dados. "
                    "Confira o número do pedido e o e-mail ou telefone do cadastro.",
            "tentativas_restantes": left,
            "aviso": _attempts_left_notice(left),
        }

    result = {
        "pedido": order.id,
        "cliente": customer.name.split()[0],
        "situacao": _STATUS_LABELS[order.status],
        "data_pedido": fmt_date(order.date),
        "itens": [{"produto": ctx.store.products[item.product_id].name, "qtd": item.qty} for item in order.items],
        "total": brl(order.total_cents),
        "pagamento": _payment_label(order.payment),
        "codigo_rastreio": order.tracking,
        "previsao_entrega": fmt_date(order.estimated_delivery) if order.estimated_delivery else None,
        "observacao": order.notes,
    }
    if order.status == "delivered":
        result["opcoes_de_devolucao"] = _return_windows(order, ctx.now)
    return result


def _read_policy(args: dict, ctx: ToolContext) -> dict:
    section = args["section"]
    return {"secao": section, "texto": ctx.policies[section]}


def _find_partner_stores(args: dict, ctx: ToolContext) -> dict:
    partners = find_partners(ctx.store, args["accessory"], args.get("city"))
    lojas = [{"nome": p.name, "vende": p.sells, "endereco": p.address, "cidade": p.city, "telefone": p.phone}
             for p in partners]
    observacao = ("Lojas parceiras indicadas pela Empório da Música." if lojas
                  else "Não temos loja parceira para esse item.")
    return {"lojas": lojas, "observacao": observacao}


def _register_ticket(args: dict, ctx: ToolContext) -> dict:
    try:
        record = append_ticket(ctx.session, args["type"], args["summary"], ctx.now, ctx.tickets_path)
    except TicketLimitError:
        return {"erro": "Limite de chamados desta conversa atingido."}
    result = {"protocolo": record["protocolo"], "prazo_retorno": "até 24 horas úteis", "loja_aberta": is_open(ctx.now)}
    if not result["loja_aberta"]:
        result["reabre"] = reopening_text(ctx.now)
    return result


def _matches(value, schema: dict) -> bool:
    kind = schema["type"]
    if kind == "string":
        if not isinstance(value, str) or isinstance(value, bool):
            return False
        if len(value) > schema.get("maxLength", len(value)) or len(value) < schema.get("minLength", 0):
            return False
        return "enum" not in schema or value in schema["enum"]
    if kind in ("integer", "number"):
        allowed_types = int if kind == "integer" else (int, float)
        if not isinstance(value, allowed_types) or isinstance(value, bool):
            return False
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            return False
        return True
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "array":
        if not isinstance(value, list):
            return False
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            return False
        item_schema = schema.get("items")
        return item_schema is None or all(_matches(item, item_schema) for item in value)
    if kind == "object":
        if not isinstance(value, dict):
            return False
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if any(key not in properties for key in value) or any(key not in value for key in required):
            return False
        return all(_matches(v, properties[k]) for k, v in value.items())
    return False


def _validate_args(args: dict, properties: dict, required: list[str]) -> str | None:
    # Errors name the accepted keys and enum options, so the model can correct the call itself.
    for key in args:
        if key not in properties:
            return f"Parâmetro desconhecido: {key}. Parâmetros aceitos: {', '.join(properties)}"
    for key in required:
        if key not in args:
            return f"Parâmetro obrigatório ausente: {key}"
    for key, value in args.items():
        if not _matches(value, properties[key]):
            options = properties[key].get("enum")
            return f"Valor inválido para {key}" + (f". Opções: {', '.join(options)}" if options else "")
    return None


_ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "product_id": {"type": "integer", "description": "ID numérico do produto."},
        "qty": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Quantidade desejada (1 a 10)."},
    },
    "required": ["product_id", "qty"],
}

_TOOL_DEFS: list[tuple[str, str, dict, list[str], Callable[[dict, ToolContext], dict]]] = [
    (
        "search_products",
        "Busca produtos no catálogo por texto, categoria ou faixa de preço. Use quando o cliente quiser "
        "encontrar ou comparar produtos sem saber exatamente qual deseja.",
        {
            "text": {"type": "string", "maxLength": 200, "description": "Texto de busca livre."},
            "category": {"type": "string", "enum": list(CATEGORY_NAMES), "description": "Categoria do produto."},
            "min_price": {"type": "number", "minimum": 0, "maximum": 1000000,
                          "description": "Preço mínimo em reais."},
            "max_price": {"type": "number", "minimum": 0, "maximum": 1000000,
                          "description": "Preço máximo em reais."},
            "include_unavailable": {"type": "boolean", "description": "Incluir produtos sem estoque ou em breve."},
        },
        [],
        _search_products,
    ),
    (
        "get_product",
        "Busca os detalhes completos de um produto específico pelo nome ou ID. Use quando o cliente perguntar "
        "sobre um produto específico, incluindo preço, parcelamento e disponibilidade.",
        {"id_or_name": {"type": "string", "maxLength": 200, "description": "Nome ou ID do produto."}},
        ["id_or_name"],
        _get_product,
    ),
    (
        "quote",
        "Monta um orçamento com subtotal, frete e opções de pagamento para uma lista de produtos e quantidades. "
        "Use quando o cliente quiser saber o valor total de uma compra.",
        {
            "items": {
                "type": "array", "minItems": 1, "maxItems": 10, "items": _ITEM_SCHEMA,
                "description": "Lista de itens do orçamento, cada um com product_id e qty.",
            },
            "city": {"type": "string", "maxLength": 200, "description": "Cidade de entrega, para calcular o frete."},
        },
        ["items"],
        _quote,
    ),
    (
        "list_active_promotions",
        "Lista todas as promoções ativas da loja. Use quando o cliente perguntar sobre promoções, descontos ou "
        "ofertas em andamento.",
        {},
        [],
        _list_active_promotions,
    ),
    (
        "get_order",
        "Consulta a situação de um pedido existente, confirmando a identidade do cliente por e-mail ou telefone. "
        "Use quando o cliente quiser saber o status, rastreio ou prazos de devolução de um pedido.",
        {
            "order_id": {"type": "integer", "minimum": 1, "maximum": 1000000, "description": "Número do pedido."},
            "email_or_phone": {"type": "string", "maxLength": 200,
                                "description": "E-mail ou telefone cadastrado do cliente."},
        },
        ["order_id", "email_or_phone"],
        _get_order,
    ),
    (
        "read_policy",
        "Retorna o texto oficial de uma seção do manual de políticas da loja. Use quando o cliente perguntar "
        "sobre horário, pagamento, trocas, frete, promoções, garantia ou privacidade.",
        {"section": {"type": "string", "enum": list(SERVED_SECTIONS), "description": "Seção da política desejada."}},
        ["section"],
        _read_policy,
    ),
    (
        "find_partner_stores",
        "Busca lojas parceiras que vendem um acessório que a Empório da Música não vende diretamente. Use quando "
        "o cliente procurar cordas, palhetas, cabos ou outros acessórios fora do catálogo.",
        {
            "accessory": {"type": "string", "maxLength": 200, "description": "Acessório procurado."},
            "city": {"type": "string", "maxLength": 200, "description": "Cidade preferida para a loja parceira."},
        },
        ["accessory"],
        _find_partner_stores,
    ),
    (
        "register_ticket",
        "Abre um chamado interno de reclamação ou de solicitação de atendimento humano. Use quando o cliente "
        "quiser reclamar de algo ou pedir para falar com um atendente humano.",
        {
            "type": {"type": "string", "enum": ["complaint", "human"], "description": "Tipo do chamado."},
            "summary": {"type": "string", "minLength": 1, "maxLength": MAX_SUMMARY,
                        "description": "Resumo do chamado."},
        },
        ["type", "summary"],
        _register_ticket,
    ),
]

TOOLS: list[dict] = []
_HANDLERS: dict[str, tuple[dict, list[str], Callable[[dict, ToolContext], dict]]] = {}

for _name, _description, _properties, _required, _handler in _TOOL_DEFS:
    _schema = {"type": "object", "additionalProperties": False, "properties": _properties}
    if _required:
        _schema["required"] = _required
    TOOLS.append({"name": _name, "description": _description, "input_schema": _schema})
    _HANDLERS[_name] = (_properties, _required, _handler)


def run_tool(name: str, args: dict, ctx: ToolContext) -> dict:
    entry = _HANDLERS.get(name)
    if entry is None:
        return {"erro": f"Ferramenta desconhecida: {name}"}
    if not isinstance(args, dict):
        return {"erro": "Parâmetros inválidos"}
    properties, required, handler = entry
    try:
        error = _validate_args(args, properties, required)
        if error is not None:
            return {"erro": error}
        return handler(args, ctx)
    except Exception:
        return {"erro": "Erro interno ao processar a solicitação."}
