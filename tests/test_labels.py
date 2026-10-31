import json
import tempfile
import unittest
from pathlib import Path

from src.labels import code_signals, judge, label_session, latest_labels, transcript_text
from src.llm import Turn, Usage
from src.eventlog import read_events


def ev(kind, **f):
    return {"t": "2026-11-02T13:00:00+00:00", "kind": kind, **f}


EVENTS = [
    ev("header", schema=1, session="s", source="chat"),
    ev("customer", text="Status do pedido 5? <EMAIL_1>"),
    ev("tool", name="get_order", input={"order_id": 5, "email_or_phone": "<EMAIL_1>"},
       result={"erro": "Não encontrei um pedido com esses dados.", "tentativas_restantes": 2}),
    ev("reply", text="Não encontrei esse pedido. Confere o e-mail?"),
    ev("customer", text="status do pedido 5?  <EMAIL_1>"),
    ev("tool", name="get_order", input={}, result={"erro": "Consulta de pedidos bloqueada nesta conversa por segurança."}),
    ev("guard", problems=["x"], rejected_text="R$ 1,00"),
    ev("tool", name="register_ticket", input={"type": "human", "summary": "s"}, result={"protocolo": "EM-1"}),
    ev("fallback", reason="guard", problems=["x"], text="Seu protocolo é EM-2 e a equipe retorna."),
    ev("error", error="LLMError"),
]


class SignalsTest(unittest.TestCase):
    def test_signals(self):
        self.assertEqual(code_signals(EVENTS), {
            "turns": 2, "tickets": {"complaint": 0, "human": 2}, "guard_hits": 1, "fallbacks": 1, "tool_cap_hit": False,
            "order_misses": 1, "order_locked": True, "tech_errors": 1, "repeated_question": True})

    def test_short_repeats_are_not_repeated_questions(self):
        events = [ev("customer", text="sim"), ev("customer", text="sim"), ev("customer", text="ok, obrigado")]
        self.assertFalse(code_signals(events)["repeated_question"])

    def test_transcript_text(self):
        t = transcript_text(EVENTS)
        self.assertTrue(t.startswith("CLIENTE: Status do pedido 5? <EMAIL_1>\nDUDA: Não encontrei esse pedido."))
        self.assertTrue(t.endswith("DUDA: Seu protocolo é EM-2 e a equipe retorna."))


class FakeJudge:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def call(self, system, messages, tools):
        self.calls.append((system, messages, tools))
        if isinstance(self.reply, Exception):
            raise self.reply
        return Turn(self.reply, [], Usage(), [{"type": "text", "text": self.reply}])


class JudgeTest(unittest.TestCase):
    def test_valid_reply_and_evidence_check(self):
        reply = '```json\n{"resolution": "handed_off", "resolution_evidence": "Seu protocolo é EM-2", "frustration": 1, "frustration_evidence": "frase inventada"}\n```'
        j = FakeJudge(reply)
        out = judge(EVENTS, j)
        self.assertEqual((out["resolution"], out["frustration"]), ("handed_off", 1))
        self.assertTrue(out["resolution_evidence_found"])
        self.assertFalse(out["frustration_evidence_found"])
        self.assertEqual(j.calls[0][2], [])

    def test_bad_replies_never_raise(self):
        for reply in ['{"resolution": "great", "frustration": 0}', '{"resolution": "resolved", "frustration": true}', "nope", RuntimeError("x")]:
            self.assertIn("error", judge(EVENTS, FakeJudge(reply)), reply)


class LabelSessionTest(unittest.TestCase):
    def test_appends_labels_and_latest_wins(self):
        path = Path(tempfile.mkdtemp()) / "s.jsonl"
        path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in EVENTS) + "\n", encoding="utf-8")
        first = label_session(path)
        self.assertEqual((first["judge"], first["judge_model"], first["rubric_version"]), (None, None, "1"))
        reply = '{"resolution": "resolved", "resolution_evidence": "", "frustration": 0, "frustration_evidence": ""}'
        second = label_session(path, FakeJudge(reply), "claude-haiku-4-5")
        events = read_events(path)
        self.assertEqual([e["kind"] for e in events].count("labels"), 2)
        self.assertEqual(latest_labels(events)["judge"]["resolution"], "resolved")
        self.assertEqual(second["judge_model"], "claude-haiku-4-5")
