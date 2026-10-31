import tempfile
import unittest
from pathlib import Path

from src import cli
from src.agent import FALLBACK_NO_TICKET, MAX_TOOL_ROUNDS, TECH_ERROR_TEXT, Agent
from src.clock import parse_now
from src.data import load_store
from src.eventlog import EventLog, Pseudonymizer, read_events
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

    def test_guard_retry_with_tool_round_in_between_cleans_history(self):
        b = FakeBackend(
            text("Custa R$ 1.999,00."),
            tool("get_product", {"id_or_name": "Takamine GD20"}),
            text("O GD20 custa R$ 2.199,00."),
        )
        a = agent(b)
        self.assertEqual(a.reply("quanto custa o gd20?"), "O GD20 custa R$ 2.199,00.")
        self.assertNotIn("1.999", str(a.session.messages))
        roles = [m["role"] for m in a.session.messages]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant"])
        self.assertTrue(roles[::2] == ["user", "user"] and roles[1::2] == ["assistant", "assistant"])
        self.assertEqual(roles[-1], "assistant")
        tool_use_msg = a.session.messages[1]
        self.assertEqual(tool_use_msg["content"][0]["type"], "tool_use")
        self.assertEqual(tool_use_msg["content"][0]["name"], "get_product")
        result_msg = a.session.messages[2]
        self.assertEqual(result_msg["content"][0]["type"], "tool_result")
        self.assertIn("R$ 2.199,00", result_msg["content"][0]["content"])

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
        a = agent(b)
        out = a.reply("promo?")
        self.assertEqual(len(b.calls), MAX_TOOL_ROUNDS + 1)
        self.assertIn("protocolo", out)
        self.assertEqual([k for k, _ in a.session.log].count("ferramenta"), MAX_TOOL_ROUNDS)
        msgs = a.session.messages
        for i, m in enumerate(msgs):
            if m["role"] == "assistant" and m["content"][0]["type"] == "tool_use":
                self.assertEqual(msgs[i + 1]["content"][0]["type"], "tool_result")
        self.assertEqual(msgs[-1]["content"][0]["text"], out)

    def test_empty_reply_is_retried(self):
        a = agent(FakeBackend(text(""), text("Oi! Como posso ajudar?")))
        self.assertEqual(a.reply("oi"), "Oi! Como posso ajudar?")
        self.assertIn(("verificacao", "resposta vazia"), a.session.log)
        self.assertEqual([m["role"] for m in a.session.messages], ["user", "assistant"])

    def test_empty_reply_twice_falls_back_without_empty_messages(self):
        a = agent(FakeBackend(text("  "), Turn(None, [], Usage(), [])))
        self.assertIn("protocolo", a.reply("oi"))
        for m in a.session.messages:
            self.assertTrue(m["content"])
            if m["role"] == "assistant":
                self.assertTrue(all(blk.get("text", "x").strip() for blk in m["content"]))

    def test_text_beside_tool_call_is_dropped_and_logged(self):
        both = Turn("Vou consultar.", [ToolCall("c1", "get_product", {"id_or_name": "Takamine GD20"})], Usage(),
                    [{"type": "text", "text": "Vou consultar."},
                     {"type": "tool_use", "id": "c1", "name": "get_product", "input": {"id_or_name": "Takamine GD20"}}])
        a = agent(FakeBackend(both, text("O GD20 custa R$ 2.199,00.")))
        self.assertEqual(a.reply("gd20?"), "O GD20 custa R$ 2.199,00.")
        self.assertEqual([blk["type"] for blk in a.session.messages[1]["content"]], ["tool_use"])
        self.assertIn(("verificacao", "texto descartado junto com ferramenta: Vou consultar."), a.session.log)

    def test_every_guard_hit_is_logged(self):
        a = agent(FakeBackend(text("R$ 1,00"), text("R$ 2,00")))
        a.reply("preço?")
        hits = [v for k, v in a.session.log if k == "verificacao"]
        self.assertEqual(len(hits), 2)
        self.assertIn("R$ 2,00", hits[1])

    def test_backend_error_rolls_back(self):
        a = agent(FakeBackend(LLMError("boom")))
        self.assertEqual(a.reply("oi"), TECH_ERROR_TEXT)
        self.assertEqual(a.session.messages, [])


def logged_agent(backend):
    d = Path(tempfile.mkdtemp())
    log = EventLog(d / "s.jsonl", Pseudonymizer())
    a = Agent(backend, STORE, POLICIES, new_session(NOW), lambda: NOW, d / "t.jsonl", events=log)
    return a, log


class EventEmissionTest(unittest.TestCase):
    def kinds(self, log):
        return [e["kind"] for e in read_events(log.path)]

    def test_tool_round_and_reply(self):
        a, log = logged_agent(FakeBackend(tool("get_product", {"id_or_name": "Takamine GD20"}), text("O GD20 custa R$ 2.199,00.")))
        a.reply("quanto custa o gd20? meu e-mail é x@y.com")
        events = read_events(log.path)
        self.assertEqual(self.kinds(log), ["customer", "model_call", "tool", "model_call", "reply"])
        self.assertEqual(events[0]["text"], "quanto custa o gd20? meu e-mail é <EMAIL_1>")
        self.assertEqual(events[2]["result"]["preco"], "R$ 2.199,00")
        self.assertIn("especificacoes", events[2]["result"])  # full result, not truncated
        self.assertEqual(events[1]["round"], 0)
        self.assertIsInstance(events[1]["latency_ms"], int)
        self.assertEqual(set(events[1]["usage"]), {"input", "output", "cache_read", "cache_write"})

    def test_guard_rejection_keeps_rejected_text(self):
        a, log = logged_agent(FakeBackend(text("Custa R$ 1.999,00."), text("Vou consultar o preço certinho.")))
        a.reply("preço?")
        guard = [e for e in read_events(log.path) if e["kind"] == "guard"]
        self.assertEqual(guard[0]["rejected_text"], "Custa R$ 1.999,00.")
        self.assertEqual(guard[0]["problems"], ["o valor R$ 1.999,00 não veio de nenhuma consulta"])
        self.assertEqual(self.kinds(log)[-1], "reply")

    def test_fallback_and_error(self):
        a, log = logged_agent(FakeBackend(text("R$ 1,00"), text("R$ 2,00")))
        a.reply("preço?")
        fb = [e for e in read_events(log.path) if e["kind"] == "fallback"]
        self.assertEqual((len(fb), fb[0]["reason"]), (1, "guard"))
        a2, log2 = logged_agent(FakeBackend(LLMError("boom")))
        a2.reply("oi")
        self.assertEqual(self.kinds(log2), ["customer", "error"])
        self.assertEqual(read_events(log2.path)[1]["error"], "LLMError")

    def test_tool_cap_fallback_reason(self):
        b = FakeBackend(*[tool("list_active_promotions", {}, f"c{i}") for i in range(MAX_TOOL_ROUNDS + 1)])
        a, log = logged_agent(b)
        a.reply("promo?")
        self.assertEqual([e["reason"] for e in read_events(log.path) if e["kind"] == "fallback"], ["tool_cap"])

    def test_without_events_nothing_changes(self):
        a = agent(FakeBackend(text("Oi!")))
        self.assertIsNone(a.events)
        self.assertEqual(a.reply("oi"), "Oi!")


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
        self.assertIn("_Gerado por python -m src.cli em 02/11/2026 10:00, horário de Campo Grande (UTC−4) · backend fake_", md)
