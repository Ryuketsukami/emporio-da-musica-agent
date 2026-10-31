"""Session outcome labels (spec §17, D16): code signals always, an optional LLM judge.

`code_signals` is cheap and always available; `judge` asks a model to grade the finished
chat against a versioned rubric and never raises — a bad or missing reply just yields
`{"error": ...}` so labelling never breaks a report run. `label_session` combines both
into one `labels` event, appended to the session's own JSONL file.
"""

from difflib import SequenceMatcher
from pathlib import Path

from src.eventlog import append_event, read_events
from src.llm import extract_json

RUBRIC_VERSION = "1"
RESOLUTIONS = ("resolved", "partial", "unresolved", "handed_off")
REPEAT_MIN_CHARS = 15  # shorter messages ("sim", "ok") repeat naturally

JUDGE_SYSTEM = (
    "You label one finished customer-service chat between a music store's assistant (DUDA) and a customer "
    "(CLIENTE). The chat is in Portuguese. Read it and reply with ONE JSON object and nothing else:\n"
    '{"resolution": "resolved" | "partial" | "unresolved" | "handed_off", "resolution_evidence": "<short exact '
    'quote from the chat>", "frustration": 0 | 1 | 2, "frustration_evidence": "<short exact quote from the chat, '
    'or empty if 0>"}\n'
    "resolution: resolved = everything the customer asked was answered or done, nothing left pending; a polite, "
    "correct refusal of an out-of-scope request counts as resolved. partial = some of the requests were answered, "
    "some were not. unresolved = the main request was not answered, was answered wrongly, or the chat ended "
    "mid-request. handed_off = the main outcome is a referral to a human (a ticket/protocol, or \"talk to the "
    "team\").\n"
    "frustration: 0 = none. 1 = mild: impatience, repeating the question, short annoyed replies. 2 = clear: "
    "complaints about the service or the assistant, anger, giving up.\n"
    "Quotes must be copied exactly from the chat. Judge only what is in the chat."
)


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def code_signals(events: list[dict]) -> dict:
    customer_texts = [e.get("text", "") for e in events if e.get("kind") == "customer"]

    tickets = {"complaint": 0, "human": 0}
    for e in events:
        if e.get("kind") == "tool" and e.get("name") == "register_ticket" and "protocolo" in (e.get("result") or {}):
            ticket_type = (e.get("input") or {}).get("type")
            if ticket_type in tickets:
                tickets[ticket_type] += 1
        elif e.get("kind") == "fallback" and "protocolo" in (e.get("text") or ""):
            tickets["human"] += 1

    order_results = [
        e.get("result") or {} for e in events if e.get("kind") == "tool" and e.get("name") == "get_order"
    ]

    normed_texts = [n for n in (_norm(t) for t in customer_texts) if len(n) >= REPEAT_MIN_CHARS]
    repeated_question = any(
        SequenceMatcher(None, normed_texts[i], normed_texts[j]).ratio() >= 0.9
        for i in range(len(normed_texts))
        for j in range(i + 1, len(normed_texts))
    )

    return {
        "turns": len(customer_texts),
        "tickets": tickets,
        "guard_hits": sum(1 for e in events if e.get("kind") == "guard"),
        "fallbacks": sum(1 for e in events if e.get("kind") == "fallback"),
        "tool_cap_hit": any(e.get("kind") == "fallback" and e.get("reason") == "tool_cap" for e in events),
        "order_misses": sum(1 for r in order_results if "tentativas_restantes" in r),
        "order_locked": any("bloqueada" in (r.get("erro") or "") for r in order_results),
        "tech_errors": sum(1 for e in events if e.get("kind") == "error"),
        "repeated_question": repeated_question,
    }


def transcript_text(events: list[dict]) -> str:
    lines = []
    for e in events:
        kind = e.get("kind")
        if kind == "customer":
            lines.append(f"CLIENTE: {e.get('text', '')}")
        elif kind in ("reply", "fallback"):
            lines.append(f"DUDA: {e.get('text', '')}")
    return "\n".join(lines)


def judge(events: list[dict], backend) -> dict:
    """The judge's labels plus its own `cost_usd`; an error result keeps the cost of a call that answered."""
    turn = None
    try:
        transcript = transcript_text(events)
        turn = backend.call(JUDGE_SYSTEM, [{"role": "user", "content": transcript}], [])
        data = extract_json(turn.text)

        resolution = data["resolution"]
        resolution_evidence = data["resolution_evidence"]
        frustration = data["frustration"]
        frustration_evidence = data["frustration_evidence"]

        if resolution not in RESOLUTIONS:
            raise ValueError(f"bad resolution: {resolution!r}")
        if type(frustration) is not int or frustration not in (0, 1, 2):
            raise ValueError(f"bad frustration: {frustration!r}")
        if not isinstance(resolution_evidence, str) or not isinstance(frustration_evidence, str):
            raise ValueError("evidence must be a string")
    except Exception as exc:
        error = {"error": f"{type(exc).__name__}: {exc}"}
        return error if turn is None else {**error, "cost_usd": turn.cost_usd}

    normed_transcript = _norm(transcript)

    def _found(evidence: str) -> bool:
        return not evidence or _norm(evidence) in normed_transcript

    return {
        "resolution": resolution,
        "resolution_evidence": resolution_evidence,
        "frustration": frustration,
        "frustration_evidence": frustration_evidence,
        "resolution_evidence_found": _found(resolution_evidence),
        "frustration_evidence_found": _found(frustration_evidence),
        "cost_usd": turn.cost_usd,
    }


def label_session(path: Path, judge_backend=None, judge_model: str | None = None) -> dict:
    events = read_events(path)
    label = {
        "rubric_version": RUBRIC_VERSION,
        "signals": code_signals(events),
        "judge": judge(events, judge_backend) if judge_backend is not None else None,
        "judge_model": judge_model,
    }
    append_event(path, "labels", label)
    return label


def latest_labels(events: list[dict]) -> dict | None:
    labels = [e for e in events if e.get("kind") == "labels"]
    return labels[-1] if labels else None
