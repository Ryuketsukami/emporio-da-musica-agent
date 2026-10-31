"""Interactive chat and --script replay for Duda, the Empório da Música agent."""

import argparse
from pathlib import Path

from src import clock
from src.agent import Agent
from src.clock import TZ_LABEL, parse_now
from src.config import SESSIONS_DIR, load_settings
from src.data import load_store
from src.llm import make_backend
from src.policies import load_policies
from src.session import PHONE_RE, new_session, save_transcript

DEFAULT_TITLE = "Atendimento"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m src.cli")
    parser.add_argument("--script", type=Path)
    parser.add_argument("--now")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--title")
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


def _keep_phones(policies: dict[str, str], store) -> frozenset:
    digits = set()
    for key in ("sobre", "atendimento"):
        for match in PHONE_RE.finditer(policies[key]):
            digits.add("".join(c for c in match.group() if c.isdigit())[-8:])
    for partner in store.partners:
        only_digits = "".join(c for c in partner.phone if c.isdigit())
        if len(only_digits) >= 8:
            digits.add(only_digits[-8:])
    return frozenset(digits)


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


def main(argv: list[str] | None = None, backend=None) -> int:
    args = _parse_args(argv)
    settings = load_settings()
    backend = backend or make_backend(settings)
    now_fn = (lambda: parse_now(args.now)) if args.now else clock.now

    store = load_store()
    policies = load_policies()
    session = new_session(now_fn())
    agent = Agent(backend, store, policies, session, now_fn)

    script_title = None
    if args.script:
        script_title = _run_script(agent, args.script)
    else:
        _run_interactive(agent)

    title = args.title or script_title or DEFAULT_TITLE
    now = now_fn()
    note = f"Gerado por python -m src.cli em {now:%d/%m/%Y %H:%M}, {TZ_LABEL} · backend {backend.label}"
    keep_phones = _keep_phones(policies, store)
    out_path = args.out if args.out else SESSIONS_DIR / f"{session.id}.md"
    saved = save_transcript(session, title, out_path, note, keep_phones)
    print(saved)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
