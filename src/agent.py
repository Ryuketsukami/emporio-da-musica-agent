"""The agent loop: one model call per round, tool dispatch, and a guarded reply."""

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from src.config import TICKETS_PATH
from src.data import Store
from src.guard import check
from src.llm import Turn
from src.prompt import system_prompt, user_turn
from src.session import Session, TicketLimitError, append_ticket
from src.tools import TOOLS, ToolContext, run_tool

MAX_TOOL_ROUNDS = 6

FALLBACK_TEXT = (
    "Quero te passar uma informação certinha, então vou pedir para alguém da equipe confirmar. "
    "Seu protocolo é {protocolo} e a equipe retorna em até 24 horas úteis."
)
FALLBACK_NO_TICKET = (
    "Quero te passar uma informação certinha, então vou pedir para alguém da equipe te ajudar. "
    "Você pode falar com a loja pelo WhatsApp (67) 3321-4500."
)
TECH_ERROR_TEXT = "Tive um probleminha técnico agora. Pode mandar sua mensagem de novo?"


class Agent:
    def __init__(
        self,
        backend,
        store: Store,
        policies: dict[str, str],
        session: Session,
        now_fn: Callable[[], datetime],
        tickets_path: Path = TICKETS_PATH,
    ):
        self.backend = backend
        self.store = store
        self.policies = policies
        self.session = session
        self.now_fn = now_fn
        self.tickets_path = tickets_path
        self.system = system_prompt(policies, store)

    def allowed_text(self) -> str:
        parts = [self.system, "\n".join(self.policies.values())]
        parts.extend(self.session.tool_results)
        parts.extend(self.session.customer_texts)
        return "\n".join(parts)

    def _run_tools(self, turn: Turn) -> None:
        session = self.session
        blocks = []
        for call in turn.tool_calls:
            ctx = ToolContext(self.store, self.policies, session, self.now_fn(), self.tickets_path)
            result = run_tool(call.name, call.input, ctx)
            dumped = json.dumps(result, ensure_ascii=False)
            session.tool_results.append(dumped)
            session.note("ferramenta",
                         f"{call.name} {json.dumps(call.input, ensure_ascii=False)} → {dumped[:160]}")
            blocks.append({"type": "tool_result", "tool_use_id": call.id, "content": dumped})
        session.messages.append({"role": "user", "content": blocks})

    def _fallback(self, problems: list[str]) -> str:
        session = self.session
        try:
            record = append_ticket(session, "human", f"Resposta bloqueada: {problems or 'limite de consultas'}",
                                    self.now_fn(), self.tickets_path)
            fallback = FALLBACK_TEXT.format(protocolo=record["protocolo"])
        except TicketLimitError:
            fallback = FALLBACK_NO_TICKET

        if session.messages and session.messages[-1]["role"] == "assistant":
            session.messages[-1] = {"role": "assistant", "content": [{"type": "text", "text": fallback}]}
        else:
            session.messages.append({"role": "assistant", "content": [{"type": "text", "text": fallback}]})
        session.note("duda", fallback)
        return fallback

    def reply(self, text: str) -> str:
        session = self.session
        start_len = len(session.messages)
        session.customer_texts.append(text)
        session.note("cliente", text)
        session.messages.append({"role": "user", "content": user_turn(text, self.now_fn())})

        rejected_indices: list[int] = []
        guard_failed_once = False
        problems: list[str] = []

        for _ in range(MAX_TOOL_ROUNDS + 1):
            try:
                turn = self.backend.call(self.system, session.messages, TOOLS)
            except Exception as exc:
                del session.messages[start_len:]
                session.note("verificacao", type(exc).__name__)
                return TECH_ERROR_TEXT

            session.messages.append({"role": "assistant", "content": turn.content})

            if turn.tool_calls:
                self._run_tools(turn)
                continue

            reply_text = turn.text or ""
            verdict = check(reply_text, self.allowed_text())
            if verdict.ok:
                for idx in sorted(rejected_indices, reverse=True):
                    del session.messages[idx]
                session.note("duda", reply_text)
                return reply_text

            problems = verdict.problems
            rejected_indices.append(len(session.messages) - 1)
            if not guard_failed_once:
                guard_failed_once = True
                session.note("verificacao", "; ".join(problems))
                correction = (
                    "[verificação automática] Sua resposta citou dados que não vieram de nenhuma consulta: "
                    + "; ".join(problems) + ". Consulte a ferramenta certa ou responda sem esses dados."
                )
                session.messages.append({"role": "user", "content": correction})
                rejected_indices.append(len(session.messages) - 1)
                continue
            break

        for idx in sorted(rejected_indices, reverse=True):
            del session.messages[idx]

        return self._fallback(problems)
