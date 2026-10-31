"""Store time: fixed UTC-4 (Campo Grande, no DST since 2019), opening hours."""

from datetime import date, datetime, timedelta, timezone

STORE_TZ = timezone(timedelta(hours=-4))
TZ_LABEL = "horário de Campo Grande (UTC−4)"

WEEKDAYS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]

# Monday=0 .. Sunday=6. None means closed all day. Holidays not modelled.
HOURS = {0: (9, 18), 1: (9, 18), 2: (9, 18), 3: (9, 18), 4: (9, 18), 5: (9, 13), 6: None}


def parse_now(text: str) -> datetime:
    t = datetime.fromisoformat(text)
    if t.tzinfo is None:
        return t.replace(tzinfo=STORE_TZ)
    return t.astimezone(STORE_TZ)


def now(override: datetime | None = None) -> datetime:
    at = override if override is not None else datetime.now(timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=STORE_TZ)
    return at.astimezone(STORE_TZ)


def _opening_closing(at: datetime):
    hours = HOURS[at.weekday()]
    if hours is None:
        return None, None
    open_h, close_h = hours
    opening = at.replace(hour=open_h, minute=0, second=0, microsecond=0)
    closing = at.replace(hour=close_h, minute=0, second=0, microsecond=0)
    return opening, closing


def is_open(at: datetime) -> bool:
    opening, closing = _opening_closing(at)
    if opening is None:
        return False
    return opening <= at < closing


def closing_time(at: datetime) -> datetime | None:
    _, closing = _opening_closing(at)
    return closing


def next_opening(at: datetime) -> datetime:
    for day_offset in range(8):
        day = at + timedelta(days=day_offset)
        opening, _ = _opening_closing(day)
        if opening is None:
            continue
        if opening > at:
            return opening
    raise RuntimeError("no opening found within 7 days")


def reopening_text(at: datetime) -> str:
    o = next_opening(at)
    return f"{WEEKDAYS[o.weekday()]} {o:%d/%m} às {o:%H:%M}"


def time_line(at: datetime) -> str:
    head = f"[agora: {WEEKDAYS[at.weekday()]} {at:%d/%m/%Y %H:%M}, {TZ_LABEL} — "
    if is_open(at):
        return head + f"loja aberta | fecha às {closing_time(at):%H:%M}]"
    return head + f"loja fechada | reabre {reopening_text(at)}]"


def fmt_date(d: date) -> str:
    return f"{d:%d/%m/%Y}"
