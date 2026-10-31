import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


def priced(turn, cost, usage=None):
    turn.cost_usd = cost
    if usage is not None:
        turn.usage = usage
    return turn


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

    def test_log_failure_never_breaks_a_reply(self):
        class BrokenLog:
            def __init__(self):
                self.calls = 0

            def write(self, kind, **fields):
                self.calls += 1
                raise OSError("disk full")

        log = BrokenLog()
        a = Agent(FakeBackend(tool("get_product", {"id_or_name": "Takamine GD20"}), text("O GD20 custa R$ 2.199,00.")),
                  STORE, POLICIES, new_session(NOW), lambda: NOW, Path(tempfile.mkdtemp()) / "t.jsonl", events=log)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(a.reply("quanto custa o gd20?"), "O GD20 custa R$ 2.199,00.")
        self.assertIsNone(a.events)
        self.assertEqual(log.calls, 1)
        self.assertEqual(buf.getvalue().strip(), "aviso: log da sessão desativado (OSError)")

    def test_reply_carries_the_spend_of_all_its_model_calls(self):
        b = FakeBackend(priced(tool("get_product", {"id_or_name": "Takamine GD20"}), 0.001, Usage(10, 2, 100, 0)),
                        priced(text("O GD20 custa R$ 2.199,00."), 0.002, Usage(20, 30, 100, 5)))
        a, log = logged_agent(b)
        a.reply("quanto custa o gd20?")
        events = read_events(log.path)
        self.assertEqual([e["cost_usd"] for e in events if e["kind"] == "model_call"], [0.001, 0.002])
        reply = events[-1]
        self.assertAlmostEqual(reply["cost_usd"], 0.003)
        self.assertEqual(reply["usage"], {"input": 30, "output": 32, "cache_read": 200, "cache_write": 5})
        self.assertAlmostEqual(a.session.cost_usd, 0.003)
        self.assertEqual(a.session.usage, Usage(30, 32, 200, 5))

    def test_session_spend_adds_up_across_replies_and_a_guard_retry(self):
        b = FakeBackend(priced(text("Custa R$ 1.999,00."), 0.01), priced(text("Vou consultar o preço certinho."), 0.02),
                        priced(text("Oi!"), 0.04))
        a, log = logged_agent(b)
        a.reply("preço?")
        a.reply("oi")
        replies = [e for e in read_events(log.path) if e["kind"] == "reply"]
        self.assertEqual([r["cost_usd"] for r in replies], [0.03, 0.04])
        self.assertAlmostEqual(a.session.cost_usd, 0.07)

    def test_fallback_and_error_carry_spend_too(self):
        a, log = logged_agent(FakeBackend(priced(text("R$ 1,00"), 0.01), priced(text("R$ 2,00"), 0.01)))
        a.reply("preço?")
        self.assertEqual([e["cost_usd"] for e in read_events(log.path) if e["kind"] == "fallback"], [0.02])
        a2, log2 = logged_agent(FakeBackend(priced(tool("list_active_promotions", {}), 0.01), LLMError("boom")))
        a2.reply("promo?")
        self.assertEqual([e["cost_usd"] for e in read_events(log2.path) if e["kind"] == "error"], [0.01])

    def test_one_unpriced_call_makes_the_spend_unknown(self):
        a, log = logged_agent(FakeBackend(tool("list_active_promotions", {}), priced(text("Sem promoções agora."), 0.01)))
        a.reply("promo?")
        self.assertIsNone(read_events(log.path)[-1]["cost_usd"])
        self.assertIsNone(a.session.cost_usd)

    def test_without_events_nothing_changes(self):
        a = agent(FakeBackend(text("Oi!")))
        self.assertIsNone(a.events)
        self.assertEqual(a.reply("oi"), "Oi!")

    def test_without_events_usage_is_not_converted(self):
        a = agent(FakeBackend(text("Oi!")))
        with mock.patch("src.agent.dataclasses.asdict", side_effect=AssertionError("asdict called")):
            self.assertEqual(a.reply("oi"), "Oi!")


class FakeJudge:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def call(self, system, messages, tools):
        self.calls.append((system, messages, tools))
        if isinstance(self.reply, Exception):
            raise self.reply
        return Turn(self.reply, [], Usage(), [{"type": "text", "text": self.reply}])


class ShippingCheckTest(unittest.TestCase):
    def test_shipping_after_product_lookup_needs_quote(self):
        b = FakeBackend(tool("get_product", {"id_or_name": "Yamaha C40"}), text("O frete sai grátis!"),
                        tool("quote", {"items": [{"product_id": 81, "qty": 1}], "city": "Campo Grande"}, "c2"),
                        text("O frete sai grátis!"))
        a = agent(b)
        self.assertEqual(a.reply("frete do C40 pra Campo Grande?"), "O frete sai grátis!")
        self.assertEqual(len(b.calls), 4)
        self.assertIn("quote", b.calls[2][-1]["content"])

    def test_flags_reach_the_system_prompt(self):
        a = Agent(FakeBackend(text("Oi")), STORE, POLICIES, new_session(NOW), lambda: NOW,
                  Path(tempfile.mkdtemp()) / "t.jsonl", prompt_flags=("call_tools_directly",))
        self.assertIn("# Regras adicionais", a.system)


class CliTest(unittest.TestCase):
    def test_script_run_writes_masked_transcript(self):
        d = Path(tempfile.mkdtemp())
        script = d / "s.txt"
        script.write_text("# Consulta de pedido\nmeu email é lucas.mendes@jmail.com\n", encoding="utf-8")
        out = d / "out.md"
        sessions_dir = Path(tempfile.mkdtemp())
        with mock.patch.object(cli, "SESSIONS_DIR", sessions_dir):
            code = cli.main(["--script", str(script), "--now", "2026-11-02T10:00", "--out", str(out)],
                            backend=FakeBackend(text("Obrigada!")))
        md = out.read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertTrue(md.startswith("# Consulta de pedido"))
        self.assertIn("**Cliente:** meu email é l***@jmail.com", md)
        self.assertIn("**Duda:** Obrigada!", md)
        self.assertIn("_Gerado por python -m src.cli em 02/11/2026 10:00, horário de Campo Grande (UTC−4) · backend fake_", md)

        jsonl_files = list(sessions_dir.glob("*.jsonl"))
        self.assertEqual(len(jsonl_files), 1)
        events = read_events(jsonl_files[0])
        self.assertEqual(events[0]["kind"], "header")
        self.assertEqual(events[0]["source"], "script")
        self.assertEqual(events[-1]["kind"], "labels")
        self.assertIsNone(events[-1]["judge"])
        content = jsonl_files[0].read_text(encoding="utf-8")
        self.assertNotIn("lucas.mendes", content)
        self.assertIn("<EMAIL_1>", content)

    def test_judge_flag_uses_judge_backend(self):
        d = Path(tempfile.mkdtemp())
        script = d / "s.txt"
        script.write_text("oi\n", encoding="utf-8")
        sessions_dir = Path(tempfile.mkdtemp())
        reply = ('{"resolution": "resolved", "resolution_evidence": "", "frustration": 0, '
                 '"frustration_evidence": ""}')
        with mock.patch.object(cli, "SESSIONS_DIR", sessions_dir):
            code = cli.main(["--script", str(script), "--now", "2026-11-02T10:00", "--judge"],
                            backend=FakeBackend(text("Obrigada!")), judge_backend=FakeJudge(reply))
        self.assertEqual(code, 0)
        jsonl_path = next(sessions_dir.glob("*.jsonl"))
        events = read_events(jsonl_path)
        self.assertEqual(events[-1]["judge"]["resolution"], "resolved")

    def test_label_failure_does_not_break_chat(self):
        d = Path(tempfile.mkdtemp())
        script = d / "s.txt"
        script.write_text("oi\n", encoding="utf-8")

        sessions_dir = Path(tempfile.mkdtemp())
        with mock.patch.object(cli, "SESSIONS_DIR", sessions_dir):
            code = cli.main(["--script", str(script), "--now", "2026-11-02T10:00", "--judge"],
                            backend=FakeBackend(text("Obrigada!")), judge_backend=FakeJudge(RuntimeError("boom")))
        self.assertEqual(code, 0)
        jsonl_path = next(sessions_dir.glob("*.jsonl"))
        events = read_events(jsonl_path)
        self.assertEqual(events[-1]["judge"], {"error": "RuntimeError: boom"})

        sessions_dir2 = Path(tempfile.mkdtemp())
        buf = io.StringIO()
        with mock.patch.object(cli, "SESSIONS_DIR", sessions_dir2), \
                mock.patch.object(cli, "label_session", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stdout(buf):
            code2 = cli.main(["--script", str(script), "--now", "2026-11-02T10:00"],
                             backend=FakeBackend(text("Obrigada!")))
        self.assertEqual(code2, 0)
        self.assertIn("aviso: rótulos não gerados", buf.getvalue())

    def test_header_write_failure_does_not_break_chat(self):
        d = Path(tempfile.mkdtemp())
        script = d / "s.txt"
        script.write_text("oi\n", encoding="utf-8")
        out = d / "out.md"
        buf = io.StringIO()
        with mock.patch.object(cli, "SESSIONS_DIR", Path(tempfile.mkdtemp())), \
                mock.patch.object(cli.EventLog, "write", side_effect=OSError("disk full")), \
                contextlib.redirect_stdout(buf):
            code = cli.main(["--script", str(script), "--now", "2026-11-02T10:00", "--out", str(out)],
                            backend=FakeBackend(text("Obrigada!")))
        self.assertEqual(code, 0)
        self.assertIn("duda> Obrigada!", buf.getvalue())
        self.assertIn("aviso: log da sessão desativado (OSError)", buf.getvalue())
        self.assertIn("**Duda:** Obrigada!", out.read_text(encoding="utf-8"))

    def test_end_is_written_when_saving_the_transcript_fails(self):
        d = Path(tempfile.mkdtemp())
        script = d / "s.txt"
        script.write_text("oi\n", encoding="utf-8")
        sessions_dir = Path(tempfile.mkdtemp())
        with mock.patch.object(cli, "SESSIONS_DIR", sessions_dir), \
                mock.patch.object(cli, "save_transcript", side_effect=OSError("disk full")), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                cli.main(["--script", str(script), "--now", "2026-11-02T10:00"], backend=FakeBackend(text("Obrigada!")))
        events = read_events(next(sessions_dir.glob("*.jsonl")))
        self.assertEqual(events[-1]["kind"], "end")

    def test_transcript_is_saved_before_labelling(self):
        d = Path(tempfile.mkdtemp())
        script = d / "s.txt"
        script.write_text("oi\n", encoding="utf-8")
        out = d / "out.md"
        with mock.patch.object(cli, "SESSIONS_DIR", Path(tempfile.mkdtemp())), \
                mock.patch.object(cli, "label_session", side_effect=KeyboardInterrupt), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                cli.main(["--script", str(script), "--now", "2026-11-02T10:00", "--out", str(out)],
                         backend=FakeBackend(text("Obrigada!")))
        self.assertIn("**Duda:** Obrigada!", out.read_text(encoding="utf-8"))
