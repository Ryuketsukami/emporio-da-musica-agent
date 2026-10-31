"""Pseudonymized session event log writer (spec §17).

Why placeholders, not masking (D14): `mask()` turns every distinct e-mail or phone into the
same shape (`l***@domain`, digits starred out), so a transcript with three wrong e-mails
followed by the right one reads as if the customer gave the same value four times — the
trajectory looks contradictory once replayed or used for training. A per-session placeholder
(`<EMAIL_1>`, `<EMAIL_2>`, ...) keeps distinct values distinguishable while never writing the
real value anywhere; the value-to-tag mapping lives only in the `Pseudonymizer` instance, for
the life of the session, and is never persisted.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from src.session import EMAIL_RE, PHONE_RE


class Pseudonymizer:
    """Replaces customer e-mails and phones with per-instance, first-seen-order placeholders.

    `keep_phones` holds last-8-digit strings and `keep_emails` holds lowercase addresses for
    store/partner contacts that must stay readable.
    """

    def __init__(self, keep_phones: frozenset[str] = frozenset(), keep_emails: frozenset[str] = frozenset()):
        self.keep_phones = keep_phones
        self.keep_emails = keep_emails
        self._email_tags: dict[str, str] = {}
        self._phone_tags: dict[str, str] = {}

    def _email_tag(self, value: str) -> str:
        key = value.lower()
        if key not in self._email_tags:
            self._email_tags[key] = f"<EMAIL_{len(self._email_tags) + 1}>"
        return self._email_tags[key]

    def _phone_tag(self, digits: str) -> str:
        if digits not in self._phone_tags:
            self._phone_tags[digits] = f"<PHONE_{len(self._phone_tags) + 1}>"
        return self._phone_tags[digits]

    def scrub(self, text: str) -> str:
        def _email_sub(m):
            value = m.group()
            if value.lower() in self.keep_emails:
                return value
            return self._email_tag(value)

        text = EMAIL_RE.sub(_email_sub, text)

        def _phone_sub(m):
            matched = m.group()
            digits = "".join(c for c in matched if c.isdigit())[-8:]
            if digits in self.keep_phones:
                return matched
            return self._phone_tag(digits)

        return PHONE_RE.sub(_phone_sub, text)

    def scrub_obj(self, obj):
        if isinstance(obj, dict):
            return {key: self.scrub_obj(value) for key, value in obj.items()}
        if isinstance(obj, list):
            return [self.scrub_obj(value) for value in obj]
        if isinstance(obj, tuple):
            return tuple(self.scrub_obj(value) for value in obj)
        if isinstance(obj, str):
            return self.scrub(obj)
        return obj


def append_event(path: Path, kind: str, fields: dict) -> None:
    """Appends one JSONL event line, flushed immediately: `{"t": ..., "kind": kind, **fields}`."""
    record = {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, **fields}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


class EventLog:
    """Appends one scrubbed JSONL event per call, flushed immediately."""

    def __init__(self, path: Path, pseudo: Pseudonymizer):
        self.path = path
        self.pseudo = pseudo

    def write(self, kind: str, **fields) -> None:
        append_event(self.path, kind, self.pseudo.scrub_obj(fields))


def public_contacts(policies: dict[str, str], store) -> tuple[frozenset[str], frozenset[str]]:
    """Last-8-digit phones and lowercase e-mails for store/partner contacts that must stay readable."""
    phones = set()
    emails = set()
    for key in ("sobre", "atendimento"):
        for match in PHONE_RE.finditer(policies[key]):
            phones.add("".join(c for c in match.group() if c.isdigit())[-8:])
        for match in EMAIL_RE.finditer(policies[key]):
            emails.add(match.group().lower())
    for partner in store.partners:
        only_digits = "".join(c for c in partner.phone if c.isdigit())
        if len(only_digits) >= 8:
            phones.add(only_digits[-8:])
    return frozenset(phones), frozenset(emails)


def read_events(path: Path) -> list[dict]:
    """Reads a JSONL event log back into a list of dicts, skipping blank lines."""
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        events.append(json.loads(line))
    return events
