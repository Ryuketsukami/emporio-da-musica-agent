"""Code-checked evals: run scripted conversations against a backend and check tool
calls and reply content, independent of any model's self-report."""

import argparse
import json
from datetime import datetime
from pathlib import Path

from src.agent import Agent
from src.clock import parse_now
from src.config import ROOT, RUNTIME_DIR, load_settings
from src.data import load_store
from src.eventlog import EventLog, Pseudonymizer, public_contacts
from src.labels import label_session
from src.llm import make_backend
from src.policies import load_policies
from src.session import Session, new_session, save_transcript
from src.tools import TOOLS
from src.versioning import build_header

DEFAULT_NOW = "2026-11-02T10:00"

FILE_PATHS = {"cases": ROOT / "evals" / "cases.jsonl", "edge": ROOT / "evals" / "edge_cases.jsonl"}


def load_cases(path: Path) -> list[dict]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        cases.append(json.loads(stripped))
    return cases


def evaluate(case: dict, replies: list[str], called: list[str]) -> list[str]:
    failures = []
    joined = "\n".join(replies).lower()

    for name in case.get("must_call", []):
        if name not in called:
            failures.append(f"não chamou {name}")

    for group in case.get("must_call_any", []):
        if not any(name in called for name in group):
            failures.append(f"não chamou nenhuma de {', '.join(group)}")

    for name in case.get("must_not_call", []):
        if name in called:
            failures.append(f"chamou {name}")

    for item in case.get("must_contain", []):
        alts = item if isinstance(item, list) else [item]
        if not any(alt.lower() in joined for alt in alts):
            quoted = " ou ".join(f"'{alt}'" for alt in alts)
            failures.append(f"faltou {quoted}")

    for item in case.get("must_not_contain", []):
        alts = item if isinstance(item, list) else [item]
        for alt in alts:
            if alt.lower() in joined:
                failures.append(f"não devia conter '{alt}'")

    return failures


def _called_tools(session: Session) -> list[str]:
    names = []
    for message in session.messages:
        content = message["content"]
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                names.append(block["name"])
    return names


def run_case(case: dict, backend, events_dir: Path | None = None) -> tuple[list[str], list[str], Session]:
    """Logging and labelling never break a case: a failure while starting the log keeps the
    case unlogged (`agent.events` stays `None`); a failed write during the case switches the log
    off (`Agent._emit`); a failure while closing it out is reported but doesn't touch the replies
    or tool calls already collected."""
    now_text = case.get("now", DEFAULT_NOW)
    now_fn = lambda: parse_now(now_text)
    store = load_store()
    policies = load_policies()
    session = new_session(now_fn())
    agent = Agent(backend, store, policies, session, now_fn)

    log = None
    if events_dir is not None:
        try:
            keep_phones, keep_emails = public_contacts(policies, store)
            log = EventLog(events_dir / f"{case['id']}.jsonl", Pseudonymizer(keep_phones, keep_emails))
            log.write("header", **build_header(session.id, "eval", backend.label, agent.system, TOOLS, now_text))
            agent.events = log
        except Exception as exc:
            print(f"aviso: log do caso {case['id']} não gerado ({type(exc).__name__})")
            log = None

    replies = [agent.reply(text) for text in case["messages"]]
    called = _called_tools(session)

    if log is not None and agent.events is not None:  # skip a log switched off mid-case
        try:
            log.write("end")
            label_session(log.path)
        except Exception as exc:
            print(f"aviso: log do caso {case['id']} não gerado ({type(exc).__name__})")

    return replies, called, session


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m evals.run_evals")
    parser.add_argument("--file", choices=["cases", "edge", "all"], default="all")
    parser.add_argument("--only")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, backend=None) -> int:
    args = _parse_args(argv)
    settings = load_settings()
    backend = backend or make_backend(settings)

    names = ["cases", "edge"] if args.file == "all" else [args.file]
    selected: list[tuple[str, dict]] = []
    for name in names:
        for case in load_cases(FILE_PATHS[name]):
            if args.only and case["id"] != args.only:
                continue
            selected.append((name, case))

    if args.only and not selected:
        print(f"erro: nenhum caso com id {args.only!r} em {', '.join(names)}")
        return 1

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = RUNTIME_DIR / "evals"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{timestamp}.jsonl"
    transcripts_dir = out_dir / timestamp

    passed = 0
    with out_path.open("w", encoding="utf-8") as f:
        for name, case in selected:
            replies, called, session = run_case(case, backend, events_dir=transcripts_dir)
            failures = evaluate(case, replies, called)
            save_transcript(session, case["id"], transcripts_dir / f"{case['id']}.md")
            record = {
                "id": case["id"], "file": name, "ok": not failures, "failures": failures,
                "replies": replies, "called": called,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            status = "OK" if not failures else "FAIL"
            detail = f": {'; '.join(failures)}" if failures else ""
            print(f"{status} {case['id']}{detail}")
            if not failures:
                passed += 1

    total = len(selected)
    print(f"total: {passed}/{total}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
