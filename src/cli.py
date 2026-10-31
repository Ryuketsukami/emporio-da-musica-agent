"""Interactive chat and --script replay for Duda, the Empório da Música agent."""

import argparse
from pathlib import Path

from src import clock
from src.agent import Agent
from src.clock import TZ_LABEL, parse_now
from src.config import SESSIONS_DIR, load_settings
from src.data import load_store
from src.eventlog import EventLog, Pseudonymizer, public_contacts
from src.labels import label_session
from src.llm import judge_model_name, make_backend, make_judge_backend
from src.policies import load_policies
from src.session import new_session, save_transcript
from src.tools import TOOLS
from src.versioning import build_header

DEFAULT_TITLE = "Atendimento"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m src.cli")
    parser.add_argument("--script", type=Path)
    parser.add_argument("--now")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--title")
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--source", choices=("chat", "script", "example"))
    return parser.parse_args(argv)


def _read_script(path: Path) -> tuple[str | None, list[str]]:
    title = None
    messages = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if title is None and stripped.startswith("# "):
                title = stripped[2:].strip()
            continue
        messages.append(stripped)
    return title, messages


def _run_script(agent: Agent, path: Path) -> str | None:
    title, lines = _read_script(path)
    for line in lines:
        print(f"você> {line}")
        print(f"duda> {agent.reply(line)}")
    return title


def _run_interactive(agent: Agent) -> None:
    print('Duda — Empório da Música (digite "sair" para encerrar)')
    while True:
        try:
            text = input("você> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if text.strip().lower() == "sair":
            break
        print(f"duda> {agent.reply(text)}")


def main(argv: list[str] | None = None, backend=None, judge_backend=None) -> int:
    args = _parse_args(argv)
    settings = load_settings()
    backend = backend or make_backend(settings)
    now_fn = (lambda: parse_now(args.now)) if args.now else clock.now

    store = load_store()
    policies = load_policies()
    session = new_session(now_fn())
    source = args.source or ("script" if args.script else "chat")
    keep_phones, keep_emails = public_contacts(policies, store)
    log = EventLog(SESSIONS_DIR / f"{session.id}.jsonl", Pseudonymizer(keep_phones, keep_emails))
    agent = Agent(backend, store, policies, session, now_fn, events=log)
    try:
        log.write("header", **build_header(session.id, source, backend.label, agent.system, TOOLS, args.now))
    except Exception as exc:
        print(f"aviso: log da sessão desativado ({type(exc).__name__})")
        agent.events = None

    script_title = None
    if args.script:
        script_title = _run_script(agent, args.script)
    else:
        _run_interactive(agent)

    title = args.title or script_title or DEFAULT_TITLE
    now = now_fn()
    note = f"Gerado por python -m src.cli em {now:%d/%m/%Y %H:%M}, {TZ_LABEL} · backend {backend.label}"
    out_path = args.out if args.out else SESSIONS_DIR / f"{session.id}.md"
    saved = save_transcript(session, title, out_path, note, keep_phones)

    if agent.events is None:  # a failed write switched logging off
        print(saved)
        return 0

    # The transcript is already on disk, so a slow or interrupted judge cannot lose it.
    judge_on = args.judge or settings.judge
    judge_model = judge_model_name(settings)
    try:
        log.write("end")
        judge = (judge_backend if judge_backend is not None else make_judge_backend(settings)) if judge_on else None
        label_session(log.path, judge, judge_model if judge_on else None)
    except Exception as exc:
        print(f"aviso: rótulos não gerados ({type(exc).__name__})")

    print(saved)
    print(log.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
