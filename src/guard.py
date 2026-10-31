"""Reply guard: every money value, phone, email and link in a reply must come from allowed text."""

import re
from dataclasses import dataclass, field

from src.money import brl
from src.session import EMAIL_RE, PHONE_RE

MONEY_RE = re.compile(r"R\$\s?(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{2}))?")
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+|www\.[^\s<>\"')\]]+")


@dataclass
class Verdict:
    ok: bool
    problems: list[str] = field(default_factory=list)


def _money_value(match: re.Match) -> int:
    integer_part = match.group(1).replace(".", "")
    cents_part = match.group(2)
    return int(integer_part) * 100 + (int(cents_part) if cents_part else 0)


def money_cents(text: str) -> set[int]:
    return {_money_value(m) for m in MONEY_RE.finditer(text)}


def _phone_digits(matched: str) -> str:
    return "".join(c for c in matched if c.isdigit())[-8:]


def check(reply: str, allowed: str) -> Verdict:
    allowed_money = money_cents(allowed)
    allowed_phones = {_phone_digits(m.group()) for m in PHONE_RE.finditer(allowed)}
    allowed_emails = {m.group().lower() for m in EMAIL_RE.finditer(allowed)}
    allowed_lower = allowed.lower()

    matches = sorted(
        [(m.start(), "money", m) for m in MONEY_RE.finditer(reply)]
        + [(m.start(), "phone", m) for m in PHONE_RE.finditer(reply)]
        + [(m.start(), "email", m) for m in EMAIL_RE.finditer(reply)]
        + [(m.start(), "url", m) for m in URL_RE.finditer(reply)],
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
            if _phone_digits(m.group()) in allowed_phones:
                continue
            problem = f"o telefone {m.group()} não veio de nenhuma consulta"
        elif kind == "email":
            if m.group().lower() in allowed_emails:
                continue
            problem = f"o e-mail {m.group()} não veio de nenhuma consulta"
        else:
            link = m.group().rstrip(".,;:!?")
            if link.lower() in allowed_lower:
                continue
            problem = f"o link {link} não veio de nenhuma consulta"

        if problem not in seen:
            seen.add(problem)
            problems.append(problem)

    return Verdict(ok=not problems, problems=problems)
