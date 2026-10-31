"""Session state, PII masking, transcripts, and capped support tickets."""

import json
import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from src.config import SESSIONS_DIR, TICKETS_PATH

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
PHONE_RE = re.compile(r"(?:\(\d{2}\)\s?)?\b\d{4,5}-\d{4}\b")

MAX_TICKETS = 3
MAX_SUMMARY = 500

_KIND_PREFIXES = {
    "cliente": "**Cliente:** {}",
    "duda": "**Duda:** {}",
    "ferramenta": "> ferramenta: {}",
    "verificacao": "> verificação: {}",
}


class TicketLimitError(Exception):
    pass


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[0]}***@{domain}"


def mask_phone(phone: str) -> str:
    digit_positions = [i for i, c in enumerate(phone) if c.isdigit()]
    total = len(digit_positions)
    chars = list(phone)
    for pos, idx in enumerate(digit_positions):
        if pos < 3 or pos >= total - 4:
            continue
        chars[idx] = "*"
    return "".join(chars)


def mask(text: str, keep_phones: frozenset = frozenset()) -> str:
    text = EMAIL_RE.sub(lambda m: mask_email(m.group()), text)

    def _phone_sub(m: re.Match) -> str:
        matched = m.group()
        digits = "".join(c for c in matched if c.isdigit())
        if digits[-8:] in keep_phones:
            return matched
        return mask_phone(matched)

    return PHONE_RE.sub(_phone_sub, text)


def clean_text(text: str, limit: int) -> str:
    cleaned = "".join(c for c in text if unicodedata.category(c) != "Cc" or c in "\n\t")
    return cleaned.strip()[:limit]


@dataclass
class Session:
    id: str
    started_at: datetime
    messages: list[dict] = field(default_factory=list)
    log: list[tuple[str, str]] = field(default_factory=list)
    order_misses: int = 0
    tickets: int = 0
    tool_results: list[str] = field(default_factory=list)
    customer_texts: list[str] = field(default_factory=list)

    def note(self, kind: str, text: str) -> None:
        self.log.append((kind, text))


def new_session(now: datetime) -> Session:
    session_id = f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    return Session(id=session_id, started_at=now)


def append_ticket(session: Session, kind: str, summary: str, now: datetime, path: Path = TICKETS_PATH) -> dict:
    if session.tickets >= MAX_TICKETS:
        raise TicketLimitError(f"session {session.id} already has {session.tickets} tickets")
    record = {
        "protocolo": f"EM-{now:%Y%m%d}-{secrets.randbelow(10000):04d}",
        "tipo": kind,
        "resumo": clean_text(summary, MAX_SUMMARY),
        "sessao": session.id,
        "criado_em": now.isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    session.tickets += 1
    return record


def transcript_markdown(session: Session, title: str, note: str = "", keep_phones: frozenset = frozenset()) -> str:
    parts = [f"# {title}"]
    if note:
        parts.append(f"_{note}_")
    for kind, text in session.log:
        parts.append(_KIND_PREFIXES[kind].format(text))
    doc = "\n\n".join(parts) + "\n"
    return mask(doc, keep_phones)


def save_transcript(session: Session, title: str, path: Path | None = None, note: str = "",
                     keep_phones: frozenset = frozenset()) -> Path:
    out_path = path if path is not None else SESSIONS_DIR / f"{session.id}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(transcript_markdown(session, title, note, keep_phones), encoding="utf-8")
    return out_path
