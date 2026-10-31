"""Money formatting and promo/PIX/installment/shipping quote math."""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

PIX_PCT = 5
COMBINED_MIN_CENTS = 200000
FREE_SHIPPING_MIN_CENTS = 50000
LOCAL_FEE_CENTS = 3500

_INSTALLMENT_MIN_CENTS = {2: 5000, 3: 5000, 4: 8000, 5: 8000, 6: 8000, 7: 10000, 8: 10000,
                           9: 10000, 10: 10000, 11: 10000, 12: 10000}


def brl(cents: int) -> str:
    return f"R$ {cents // 100:,}".replace(",", ".") + f",{cents % 100:02d}"


def apply_discount(cents: int, pct: int) -> int:
    return int((Decimal(cents) * (100 - pct) / 100).to_integral_value(rounding=ROUND_HALF_UP))


def max_installments(total_cents: int) -> int:
    best = 1
    for n in range(1, 13):
        if n == 1 or total_cents >= _INSTALLMENT_MIN_CENTS[n] * n:
            best = n
    return best


def installment_value(total_cents: int, n: int) -> int:
    return int((Decimal(total_cents) / n).to_integral_value(rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class QuoteLine:
    product_id: int
    name: str
    qty: int
    unit_cents: int
    promo_pct: int | None
    campaign: str | None
    special_freight: bool


def _shipping(lines: list[QuoteLine], local: bool | None, subtotal: int) -> dict:
    if local is None:
        return {"situacao": "informe a cidade de entrega para calcular o frete"}
    if local is True:
        valor = "grátis" if subtotal > FREE_SHIPPING_MIN_CENTS else brl(LOCAL_FEE_CENTS)
        return {"valor": valor, "prazo": "1 a 3 dias úteis", "modalidade": "motoboy próprio"}
    if any(line.special_freight for line in lines):
        return {"valor": "frete especial com cotação individual"}
    return {
        "valor": "calculado pelo CEP no fechamento",
        "opcoes": ["PAC (Correios): 5 a 12 dias úteis", "SEDEX (Correios): 2 a 5 dias úteis",
                   "Jadlog (.package): 3 a 8 dias úteis"],
    }


def build_quote(lines: list[QuoteLine], local: bool | None) -> dict:
    itens = []
    subtotal = 0
    pix_total = 0
    for line in lines:
        if line.promo_pct is not None:
            unit_final = apply_discount(line.unit_cents, line.promo_pct)
            promocao = {
                "preco_original": brl(line.unit_cents),
                "desconto": f"{line.promo_pct}%",
                "preco_promocional": brl(unit_final),
                "campanha": line.campaign,
            }
        else:
            unit_final = line.unit_cents
            promocao = None

        total_item = unit_final * line.qty
        subtotal += total_item
        if promocao is not None:
            pix_total += total_item
        else:
            pix_total += apply_discount(total_item, PIX_PCT)

        itens.append({
            "produto": line.name,
            "id": line.product_id,
            "qtd": line.qty,
            "preco_unitario": brl(unit_final),
            "promocao": promocao,
            "total_item": brl(total_item),
        })

    n = max_installments(subtotal)
    return {
        "itens": itens,
        "subtotal": brl(subtotal),
        "pix": {"total": brl(pix_total), "observacao": "5% de desconto no PIX só nos itens sem promoção"},
        "cartao_credito": {"max_parcelas": n, "valor_parcela": brl(installment_value(subtotal, n)), "sem_juros": True},
        "cartao_debito": "à vista",
        "boleto": "à vista, compensação em até 3 dias úteis",
        "pagamento_combinado_permitido": subtotal > COMBINED_MIN_CENTS,
        "frete": _shipping(lines, local, subtotal),
    }
