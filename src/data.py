"""CSV loading, normalisation, and accent-/typo-tolerant product search."""

import csv
import difflib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from src.config import DATA_DIR
from src.money import apply_discount

STOPWORDS = frozenset(
    "de da do das dos e o a os as um uma uns umas para pra com em no na nos nas ate por que "
    "quero queria tem voces voce qual quais quanto custa custando preco valor opcao "
    "disponivel me meu minha loja comprar algum alguma modelo r real".split()
)

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")
_LETTER_DIGIT_BOUNDARY_RE = re.compile(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])")


@dataclass(frozen=True)
class Category:
    id: int
    name: str
    description: str


@dataclass(frozen=True)
class Product:
    id: int
    name: str
    category_id: int
    category: str
    description: str
    price_cents: int
    stock: int
    status: str
    specs: dict
    created_at: str


@dataclass(frozen=True)
class Promotion:
    id: int
    product_id: int
    pct: int
    campaign: str
    active: bool


@dataclass(frozen=True)
class Customer:
    id: int
    name: str
    phone: str
    email: str
    city: str


@dataclass(frozen=True)
class OrderItem:
    product_id: int
    qty: int


@dataclass(frozen=True)
class Order:
    id: int
    customer_id: int
    date: date
    status: str
    total_cents: int
    payment: str
    tracking: str | None
    estimated_delivery: date | None
    notes: str | None
    items: tuple[OrderItem, ...]


@dataclass(frozen=True)
class Partner:
    id: int
    name: str
    sells: str
    address: str
    city: str
    phone: str


@dataclass(frozen=True)
class Store:
    categories: dict[int, Category]
    products: dict[int, Product]
    promotions: list[Promotion]
    customers: dict[int, Customer]
    orders: dict[int, Order]
    partners: list[Partner]

    def active_promo(self, product_id: int) -> Promotion | None:
        matches = [p for p in self.promotions if p.product_id == product_id and p.active]
        return max(matches, key=lambda p: p.pct, default=None)


def to_cents(text: str) -> int:
    return int((Decimal(text) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _stem(token: str) -> str:
    if token.endswith("oes") or token.endswith("aes"):
        return token[:-3] + "ao"
    if token.endswith("ais"):
        return token[:-3] + "al"
    if token.endswith("eis"):
        return token[:-3] + "el"
    if token.endswith("s") and len(token) > 3:
        return token[:-1]
    return token


def normalize(text: str) -> list[str]:
    nfkd = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    lowered = stripped.lower()
    cleaned = _NON_ALNUM_RE.sub(" ", lowered)
    spaced = _LETTER_DIGIT_BOUNDARY_RE.sub(" ", cleaned)
    return [token if token.isdigit() else _stem(token) for token in spaced.split()]


def _query_tokens(text: str) -> list[str]:
    return [t for t in normalize(text) if t not in STOPWORDS]


def _token_match(q: str, t: str) -> bool:
    if q.isdigit() or t.isdigit():
        return q == t
    if q == t:
        return True
    shorter, longer = (q, t) if len(q) <= len(t) else (t, q)
    if len(shorter) >= 4 and longer.startswith(shorter):
        return True
    if len(q) >= 4 and len(t) >= 4 and difflib.SequenceMatcher(None, q, t).ratio() >= 0.8:
        return True
    return False


def effective_price(store: Store, product: Product) -> int:
    promo = store.active_promo(product.id)
    if promo is not None:
        return apply_discount(product.price_cents, promo.pct)
    return product.price_cents


def is_available(product: Product) -> bool:
    return product.status == "active" and product.stock > 0


def availability(product: Product) -> str:
    if product.status == "coming_soon":
        return "em breve"
    if product.status == "discontinued":
        return "fora de linha"
    if product.stock == 0:
        return "temporariamente indisponível"
    if product.stock <= 2:
        return "últimas unidades"
    return "disponível"


def is_local_city(city: str) -> bool:
    return normalize(city) == ["campo", "grande"]


def needs_special_freight(product: Product) -> bool:
    tokens = normalize(product.name)
    if product.category_id == 3 and "acustica" in tokens:
        return True
    if "piano" in tokens:
        return True
    return product.category_id == 8


def rank(store: Store, text: str, include_unavailable: bool = False) -> list[tuple[int, Product]]:
    query_tokens = _query_tokens(text)
    if not query_tokens:
        return []
    results = []
    for product in store.products.values():
        if not include_unavailable and not is_available(product):
            continue
        product_tokens = normalize(product.name + " " + product.category)
        score = sum(1 for q in query_tokens if any(_token_match(q, t) for t in product_tokens))
        if score >= 1:
            results.append((score, product))
    results.sort(key=lambda sp: (-sp[0], effective_price(store, sp[1]), sp[1].id))
    return results


def search(
    store: Store,
    text: str | None = None,
    category: str | None = None,
    min_cents: int | None = None,
    max_cents: int | None = None,
    include_unavailable: bool = False,
) -> list[Product]:
    category_id = None
    if category is not None:
        matches = [c.id for c in store.categories.values() if normalize(category) == normalize(c.name)]
        if not matches:
            return []
        category_id = matches[0]

    if text:
        pool = [product for _, product in rank(store, text, include_unavailable)]
    else:
        pool = [p for p in store.products.values() if include_unavailable or is_available(p)]
        pool.sort(key=lambda p: (effective_price(store, p), p.id))

    result = []
    for product in pool:
        if category_id is not None and product.category_id != category_id:
            continue
        price = effective_price(store, product)
        if min_cents is not None and price < min_cents:
            continue
        if max_cents is not None and price > max_cents:
            continue
        result.append(product)
    return result


def find_product(store: Store, id_or_name: str) -> Product | None:
    if id_or_name.isdigit():
        return store.products.get(int(id_or_name))

    query_tokens = _query_tokens(id_or_name)
    if not query_tokens:
        return None

    candidates = rank(store, id_or_name, include_unavailable=True)
    if not candidates:
        return None

    top_score = candidates[0][0]
    if top_score / len(query_tokens) < 0.5:
        return None

    top_candidates = [p for score, p in candidates if score == top_score]
    top_candidates.sort(key=lambda p: (len(normalize(p.name)), effective_price(store, p), p.id))
    return top_candidates[0]


def alternatives(store: Store, product: Product, n: int = 3) -> list[Product]:
    target = effective_price(store, product)
    candidates = [
        p
        for p in store.products.values()
        if p.category_id == product.category_id and p.id != product.id and is_available(p)
    ]
    candidates.sort(key=lambda p: (abs(effective_price(store, p) - target), p.id))
    return candidates[:n]


def find_partners(store: Store, accessory: str, city: str | None = None) -> list[Partner]:
    query_tokens = _query_tokens(accessory)
    matches = []
    for partner in store.partners:
        sells_tokens = normalize(partner.sells)
        if any(_token_match(q, t) for q in query_tokens for t in sells_tokens):
            matches.append(partner)

    if city is not None:
        city_tokens = normalize(city)
        in_city = [p for p in matches if normalize(p.city) == city_tokens]
        if in_city:
            matches = in_city

    matches.sort(key=lambda p: (not is_local_city(p.city), p.id))
    return matches


def _table_path(data_dir: Path, table: str) -> Path:
    return data_dir / f"desafio_tecnico_ai_eng - {table}.csv"


def _read_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _load_categories(data_dir: Path) -> dict[int, Category]:
    categories = {}
    for row in _read_rows(_table_path(data_dir, "categories")):
        cat_id = int(row["category_id"])
        categories[cat_id] = Category(id=cat_id, name=row["name"], description=row["description"])
    return categories


def _load_products(data_dir: Path, categories: dict[int, Category]) -> dict[int, Product]:
    products = {}
    for row in _read_rows(_table_path(data_dir, "products")):
        product_id = int(row["product_id"])
        category_id = int(row["category_id"])
        products[product_id] = Product(
            id=product_id,
            name=row["name"],
            category_id=category_id,
            category=categories[category_id].name,
            description=row["description"],
            price_cents=to_cents(row["price_brl"]),
            stock=int(row["stock_quantity"]),
            status=row["status"],
            specs=json.loads(row["specs"]),
            created_at=row["created_at"],
        )
    return products


def _load_promotions(data_dir: Path) -> list[Promotion]:
    promotions = []
    for row in _read_rows(_table_path(data_dir, "promotions")):
        promotions.append(
            Promotion(
                id=int(row["promotion_id"]),
                product_id=int(row["product_id"]),
                pct=int(row["discount_percent"]),
                campaign=row["description"],
                active=row["is_active"] == "1",
            )
        )
    return promotions


def _load_customers(data_dir: Path) -> dict[int, Customer]:
    customers = {}
    for row in _read_rows(_table_path(data_dir, "customers")):
        customer_id = int(row["customer_id"])
        customers[customer_id] = Customer(
            id=customer_id, name=row["name"], phone=row["phone"], email=row["email"], city=row["city"]
        )
    return customers


def _load_order_items(data_dir: Path) -> dict[int, list[OrderItem]]:
    items_by_order: dict[int, list[OrderItem]] = {}
    for row in _read_rows(_table_path(data_dir, "order_items")):
        order_id = int(row["order_id"])
        item = OrderItem(product_id=int(row["product_id"]), qty=int(row["quantity"]))
        items_by_order.setdefault(order_id, []).append(item)
    return items_by_order


def _load_orders(data_dir: Path) -> dict[int, Order]:
    items_by_order = _load_order_items(data_dir)
    orders = {}
    for row in _read_rows(_table_path(data_dir, "orders")):
        order_id = int(row["order_id"])
        estimated_delivery_text = row["estimated_delivery"].strip()
        tracking = row["tracking_code"].strip() or None
        notes = row["notes"].strip() or None
        orders[order_id] = Order(
            id=order_id,
            customer_id=int(row["customer_id"]),
            date=date.fromisoformat(row["order_date"].strip()),
            status=row["status"],
            total_cents=to_cents(row["total_brl"]),
            payment=row["payment_method"],
            tracking=tracking,
            estimated_delivery=date.fromisoformat(estimated_delivery_text) if estimated_delivery_text else None,
            notes=notes,
            items=tuple(items_by_order.get(order_id, [])),
        )
    return orders


def _load_partners(data_dir: Path) -> list[Partner]:
    partners = []
    for row in _read_rows(data_dir / "partner_stores.csv"):
        partners.append(
            Partner(
                id=int(row["partner_id"]),
                name=row["name"],
                sells=row["sells"],
                address=row["address"],
                city=row["city"],
                phone=row["phone"],
            )
        )
    return partners


def load_store(data_dir: Path = DATA_DIR) -> Store:
    categories = _load_categories(data_dir)
    products = _load_products(data_dir, categories)
    promotions = _load_promotions(data_dir)
    customers = _load_customers(data_dir)
    orders = _load_orders(data_dir)
    partners = _load_partners(data_dir)
    return Store(
        categories=categories,
        products=products,
        promotions=promotions,
        customers=customers,
        orders=orders,
        partners=partners,
    )
