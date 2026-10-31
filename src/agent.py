"""The agent loop: one model call per round, tool dispatch, and a guarded reply."""

import dataclasses
import json
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from src.config import TICKETS_PATH
from src.data import Store
from src.eventlog import EventLog
from src.guard import check, shipping_without_quote
from src.llm import Turn, Usage
from src.pricing import add_cost, add_usage
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
EMPTY_REPLY = "resposta vazia"
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
        events: EventLog | None = None,
        prompt_flags: tuple[str, ...] = (),
    ):
        self.backend = backend
        self.store = store
        self.policies = policies
        self.session = session
        self.now_fn = now_fn
        self.tickets_path = tickets_path
        self.events = events
        self.system = system_prompt(policies, store, prompt_flags)

    def _emit(self, kind: str, **fields) -> None:
        """Logging only observes: the first failed write switches the log off for the session."""
        if self.events is None:
            return
        try:
            self.events.write(kind, **fields)
        except Exception as exc:
            print(f"aviso: log da sessão desativado ({type(exc).__name__})")
            self.events = None

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
            self._emit("tool", name=call.name, input=call.input, result=result)
            dumped = json.dumps(result, ensure_ascii=False)
            session.tool_results.append(dumped)
            session.note("ferramenta",
                         f"{call.name} {json.dumps(call.input, ensure_ascii=False)} → {dumped[:160]}")
            blocks.append({"type": "tool_result", "tool_use_id": call.id, "content": dumped})
        session.messages.append({"role": "user", "content": blocks})

    def _spend(self, usage: Usage, cost: float | None) -> dict:
        """The spend fields for a reply, fallback or error event; empty without a log."""
        if self.events is None:
            return {}
        return {"usage": dataclasses.asdict(usage), "cost_usd": None if cost is None else round(cost, 6)}

    def _fallback(self, problems: list[str], reason: str, spend: dict) -> str:
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
        self._emit("fallback", reason=reason, problems=problems, text=fallback, **spend)
        return fallback

    def reply(self, text: str) -> str:
        session = self.session
        start_len = len(session.messages)
        session.customer_texts.append(text)
        session.note("cliente", text)
        self._emit("customer", text=text)
        session.messages.append({"role": "user", "content": user_turn(text, self.now_fn())})

        rejected_indices: list[int] = []
        guard_failed_once = False
        problems: list[str] = []
        fallback_reason = "guard"
        tools_this_turn: set[str] = set()
        usage, cost = Usage(), 0.0

        for round_no in range(MAX_TOOL_ROUNDS + 1):
            try:
                call_start = time.monotonic()
                turn = self.backend.call(self.system, session.messages, TOOLS)
                latency_ms = int((time.monotonic() - call_start) * 1000)
            except Exception as exc:
                del session.messages[start_len:]
                session.note("verificacao", type(exc).__name__)
                self._emit("error", error=type(exc).__name__, **self._spend(usage, cost))
                return TECH_ERROR_TEXT

            usage, cost = add_usage(usage, turn.usage), add_cost(cost, turn.cost_usd)
            session.usage, session.cost_usd = add_usage(session.usage, turn.usage), add_cost(session.cost_usd, turn.cost_usd)
            if self.events is not None:
                self._emit("model_call", round=round_no, content=turn.content, model=turn.model,
                           usage=dataclasses.asdict(turn.usage), cost_usd=turn.cost_usd, latency_ms=latency_ms)

            if turn.tool_calls:
                if round_no == MAX_TOOL_ROUNDS:
                    problems = []
                    fallback_reason = "tool_cap"
                    session.note("verificacao", "limite de consultas atingido")
                    break
                if turn.text and turn.text.strip():
                    session.note("verificacao", f"texto descartado junto com ferramenta: {turn.text.strip()[:120]}")
                tool_blocks = [block for block in turn.content if block.get("type") == "tool_use"]
                session.messages.append({"role": "assistant", "content": tool_blocks})
                tools_this_turn.update(call.name for call in turn.tool_calls)
                self._run_tools(turn)
                continue

            reply_text = turn.text or ""
            if not reply_text.strip():
                problems = [EMPTY_REPLY]
                self._emit("guard", problems=problems, rejected_text=None)
            else:
                session.messages.append({"role": "assistant", "content": turn.content})
                verdict = check(reply_text, self.allowed_text())
                shipping = shipping_without_quote(reply_text, tools_this_turn, text)
                if verdict.ok and shipping is None:
                    for idx in sorted(rejected_indices, reverse=True):
                        del session.messages[idx]
                    session.note("duda", reply_text)
                    self._emit("reply", text=reply_text, **self._spend(usage, cost))
                    return reply_text
                problems = verdict.problems + ([shipping] if shipping else [])
                rejected_indices.append(len(session.messages) - 1)
                self._emit("guard", problems=problems, rejected_text=reply_text)

            session.note("verificacao", "; ".join(problems))
            if guard_failed_once:
                break
            guard_failed_once = True
            if problems == [EMPTY_REPLY]:
                correction = "[verificação automática] Sua resposta veio vazia. Responda ao cliente com texto."
            else:
                correction = (
                    "[verificação automática] Sua resposta citou dados que não vieram de nenhuma consulta: "
                    + "; ".join(problems) + ". Consulte a ferramenta certa ou responda sem esses dados."
                )
            session.messages.append({"role": "user", "content": correction})
            rejected_indices.append(len(session.messages) - 1)

        for idx in sorted(rejected_indices, reverse=True):
            del session.messages[idx]

        return self._fallback(problems, fallback_reason, self._spend(usage, cost))
