import tempfile
import unittest
from pathlib import Path

from src import cli
from src.agent import FALLBACK_NO_TICKET, MAX_TOOL_ROUNDS, TECH_ERROR_TEXT, Agent
from src.clock import parse_now
from src.data import load_store
from src.llm import LLMError, ToolCall, Turn, Usage
from src.policies import load_policies
from src.session import new_session

STORE, POLICIES = load_store(), load_policies()
NOW = parse_now("2026-11-02T10:00")


def text(t):
    return Turn(t, [], Usage(), [{"type": "text", "text": t}])


def tool(name, args, cid="c1"):
    return Turn(None, [ToolCall(cid, name, args)], Usage(), [{"type": "tool_use", "id": cid, "name": name, "input": args}])


class FakeBackend:
    label = "fake"

    def __init__(self, *turns):
        self.turns, self.calls = list(turns), []

    def call(self, system, messages, tools):
        self.calls.append([dict(m) for m in messages])
        nxt = self.turns.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def agent(backend, tickets=None):
    tickets = tickets or Path(tempfile.mkdtemp()) / "t.jsonl"
    return Agent(backend, STORE, POLICIES, new_session(NOW), lambda: NOW, tickets)


class AgentTest(unittest.TestCase):
    def test_plain_answer(self):
        a = agent(FakeBackend(text("Oi! Como posso ajudar?")))
        self.assertEqual(a.reply("oi"), "Oi! Como posso ajudar?")
        self.assertTrue(a.session.messages[0]["content"].startswith("[agora: seg 02/11/2026 10:00"))
        self.assertEqual([m["role"] for m in a.session.messages], ["user", "assistant"])

    def test_tool_round(self):
        b = FakeBackend(tool("get_product", {"id_or_name": "Takamine GD20"}), text("O GD20 custa R$ 2.199,00."))
        a = agent(b)
        self.assertEqual(a.reply("quanto custa o gd20?"), "O GD20 custa R$ 2.199,00.")
        result_msg = b.calls[1][-1]
        self.assertEqual(result_msg["content"][0]["type"], "tool_result")
        self.assertIn("R$ 2.199,00", result_msg["content"][0]["content"])
        self.assertIn("ferramenta", [k for k, _ in a.session.log])

    def test_guard_retry_then_clean_history(self):
        b = FakeBackend(text("Custa R$ 1.999,00."), text("Vou consultar o preço certinho."))
        a = agent(b)
        self.assertEqual(a.reply("preço?"), "Vou consultar o preço certinho.")
        self.assertIn("R$ 1.999,00", b.calls[1][-1]["content"])
        self.assertEqual([m["role"] for m in a.session.messages], ["user", "assistant"])
        self.assertNotIn("1.999", str(a.session.messages))

    def test_guard_double_fail_falls_back_with_ticket(self):
        tickets = Path(tempfile.mkdtemp()) / "t.jsonl"
        a = agent(FakeBackend(text("R$ 1,00"), text("R$ 2,00")), tickets)
        out = a.reply("preço?")
        self.assertIn("protocolo é EM-20261102-", out)
        self.assertEqual(len(tickets.read_text(encoding="utf-8").splitlines()), 1)
        self.assertEqual(a.session.messages[-1], {"role": "assistant", "content": [{"type": "text", "text": out}]})

    def test_fallback_without_ticket_when_capped(self):
        a = agent(FakeBackend(text("R$ 1,00"), text("R$ 2,00")))
        a.session.tickets = 3
        self.assertEqual(a.reply("preço?"), FALLBACK_NO_TICKET)

    def test_tool_rounds_are_capped(self):
        b = FakeBackend(*[tool("list_active_promotions", {}, f"c{i}") for i in range(MAX_TOOL_ROUNDS + 1)])
        out = agent(b).reply("promo?")
        self.assertEqual(len(b.calls), MAX_TOOL_ROUNDS + 1)
        self.assertIn("protocolo", out)

    def test_backend_error_rolls_back(self):
        a = agent(FakeBackend(LLMError("boom")))
        self.assertEqual(a.reply("oi"), TECH_ERROR_TEXT)
        self.assertEqual(a.session.messages, [])


class CliTest(unittest.TestCase):
    def test_script_run_writes_masked_transcript(self):
        d = Path(tempfile.mkdtemp())
        script = d / "s.txt"
        script.write_text("# Consulta de pedido\nmeu email é lucas.mendes@jmail.com\n", encoding="utf-8")
        out = d / "out.md"
        code = cli.main(["--script", str(script), "--now", "2026-11-02T10:00", "--out", str(out)],
                        backend=FakeBackend(text("Obrigada!")))
        md = out.read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertTrue(md.startswith("# Consulta de pedido"))
        self.assertIn("**Cliente:** meu email é l***@jmail.com", md)
        self.assertIn("**Duda:** Obrigada!", md)
