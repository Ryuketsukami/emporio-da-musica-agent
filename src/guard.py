"""Reply guard: every money value, phone, email, link and domain in a reply must come from allowed text."""

import re
from dataclasses import dataclass, field

from src.money import brl
from src.session import EMAIL_RE, PHONE_RE

_AMOUNT = r"(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{2}))?"
MONEY_RE = re.compile(r"R\$\s*" + _AMOUNT)
REAIS_RE = re.compile(r"(?<![\w.,])" + _AMOUNT + r"\s*reais\b", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+|www\.[^\s<>\"')\]]+")
# A bare domain such as "emporio-promo.com.br". The top-level label must be lowercase, so
# abbreviations like "Av.Afonso" are not read as domains.
DOMAIN_RE = re.compile(r"(?<![\w@.-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[a-z]{2,24}\b(?![@\w-])")

_TRAILING = ".,;:!?"


@dataclass
class Verdict:
    ok: bool
    problems: list[str] = field(default_factory=list)


def _money_value(match: re.Match) -> int:
    integer_part = match.group(1).replace(".", "")
    cents_part = match.group(2)
    return int(integer_part) * 100 + (int(cents_part) if cents_part else 0)


def _money_matches(text: str) -> list[re.Match]:
    return list(MONEY_RE.finditer(text)) + list(REAIS_RE.finditer(text))


def money_cents(text: str) -> set[int]:
    return {_money_value(m) for m in _money_matches(text)}


def _digits(matched: str) -> str:
    return "".join(c for c in matched if c.isdigit())


def _norm_link(link: str) -> str:
    link = link.rstrip(_TRAILING).lower()
    link = re.sub(r"^https?://", "", link)
    if link.startswith("www."):
        link = link[4:]
    return link.rstrip("/")


def _domains(text: str, taken: list[tuple[int, int]]) -> list[re.Match]:
    return [m for m in DOMAIN_RE.finditer(text) if not any(s <= m.start() < e for s, e in taken)]


def _links(text: str) -> list[re.Match]:
    """URLs, then bare domains that are not part of a URL or an e-mail address."""
    urls = list(URL_RE.finditer(text))
    taken = [m.span() for m in urls] + [m.span() for m in EMAIL_RE.finditer(text)]
    return urls + _domains(text, taken)


def _allowed_links(allowed: str) -> set[str]:
    links = {_norm_link(m.group()) for m in _links(allowed)}
    links |= {m.group().split("@", 1)[1].lower() for m in EMAIL_RE.finditer(allowed)}
    return links


def check(reply: str, allowed: str) -> Verdict:
    allowed_money = money_cents(allowed)
    allowed_phone_digits = [_digits(m.group()) for m in PHONE_RE.finditer(allowed)]
    allowed_last8 = {d[-8:] for d in allowed_phone_digits}
    allowed_with_ddd = {d for d in allowed_phone_digits if len(d) >= 10}
    allowed_emails = {m.group().lower() for m in EMAIL_RE.finditer(allowed)}
    allowed_links = _allowed_links(allowed)

    matches = sorted(
        [(m.start(), "money", m) for m in _money_matches(reply)]
        + [(m.start(), "phone", m) for m in PHONE_RE.finditer(reply)]
        + [(m.start(), "email", m) for m in EMAIL_RE.finditer(reply)]
        + [(m.start(), "url", m) for m in _links(reply)],
        key=lambda item: item[0],
    )

    problems = []
    seen = set()
    for _, kind, m in matches:
        if kind == "money":
            value = _money_value(m)
            if value in allowed_money:
                continue
            problem = f"o valor {brl(value)} não veio de nenhuma consulta"
        elif kind == "phone":
            digits = _digits(m.group())
            # With a DDD the whole number must match; without one, the last 8 digits.
            if (digits in allowed_with_ddd) if len(digits) >= 10 else (digits[-8:] in allowed_last8):
                continue
            problem = f"o telefone {m.group()} não veio de nenhuma consulta"
        elif kind == "email":
            if m.group().lower() in allowed_emails:
                continue
            problem = f"o e-mail {m.group()} não veio de nenhuma consulta"
        else:
            link = m.group().rstrip(_TRAILING)
            if _norm_link(link) in allowed_links:
                continue
            problem = f"o link {link} não veio de nenhuma consulta"

        if problem not in seen:
            seen.add(problem)
            problems.append(problem)

    return Verdict(ok=not problems, problems=problems)
