# Session Logs, Labels and Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development/SKILL.md (recommended) or executing-plans/SKILL.md to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record every Duda session as a structured, pseudonymized, versioned event log; label each session's outcome after it ends (code signals + optional LLM judge); report rates per version — without changing any reply.

**Architecture:** `src/eventlog.py` (placeholders + append-only JSONL writer), `src/versioning.py` (header: commit, hashes), an optional `events` hook in `src/agent.py`, `src/labels.py` (signals, judge, `label_session`), `scripts/report.py` (rates + `summary.json`), wired into `src/cli.py` and `evals/run_evals.py`.

**Tech Stack:** Python 3.11+, stdlib only (plus the existing `anthropic`), `unittest`.

**Spec:** `spec/SPEC.md` §17 (binding), plus §8 (hostile model) and §10 (transcripts).

## Global Constraints

- Python 3.11 compatible syntax. No new runtime dependencies.
- Tests: `.venv/bin/python -m unittest discover -s tests -t . -q` from the repo root; unit tests never call a model, never touch the network, never write outside temp dirs.
- **No behaviour change:** every existing test keeps passing unchanged; Duda's replies, guard, tools and the `.md` transcripts behave exactly as before. Logging and labelling only observe.
- `Agent(..., events=None)` must behave byte-for-byte as today.
- No real customer e-mail or phone ever reaches a `.jsonl` file. Store and partner contacts stay readable.
- JSONL lines: `json.dumps(record, ensure_ascii=False)`, one per event, appended and flushed immediately.
- Code, identifiers, comments, commit messages: English. Commit messages end with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Never read, print or commit `.env`.

---

### Task 1: Placeholders and the event log writer

**Files:**
- Create: `src/eventlog.py`, `tests/test_eventlog.py`

**Interfaces:**
- Consumes: `src.session.EMAIL_RE`, `src.session.PHONE_RE`.
- Produces:
  - `class Pseudonymizer(keep_phones: frozenset[str] = frozenset(), keep_emails: frozenset[str] = frozenset())` with `scrub(text: str) -> str` and `scrub_obj(obj)` (recursive over dict values, lists, tuples, strings; other types unchanged; dict keys unchanged). `keep_phones` holds last-8-digit strings; `keep_emails` lowercase addresses.
  - `class EventLog(path: Path, pseudo: Pseudonymizer)` with `write(kind: str, **fields) -> None`, attribute `path`.
  - `public_contacts(policies: dict[str, str], store) -> tuple[frozenset[str], frozenset[str]]` — (last-8 digits of every phone in `policies["sobre"]`, `policies["atendimento"]` and partner phones; lowercase e-mails in those two policy sections).
  - `read_events(path: Path) -> list[dict]` (skips blank lines).

- [ ] **Step 1: Write the failing tests** — `tests/test_eventlog.py`:
```python
import json
import tempfile
import unittest
from pathlib import Path

from src.data import load_store
from src.eventlog import EventLog, Pseudonymizer, public_contacts, read_events
from src.policies import load_policies


class PseudonymizerTest(unittest.TestCase):
    def test_same_value_same_tag_different_value_new_tag(self):
        p = Pseudonymizer()
        self.assertEqual(p.scrub("a@x.com e b@x.com e A@X.com"), "<EMAIL_1> e <EMAIL_2> e <EMAIL_1>")
        self.assertEqual(p.scrub("fone (67) 99812-3456 ou 99812-3456"), "fone <PHONE_1> ou <PHONE_1>")
        self.assertEqual(p.scrub("outro (67) 99654-7821"), "outro <PHONE_2>")

    def test_public_contacts_stay(self):
        p = Pseudonymizer(keep_phones=frozenset({"33414444"}), keep_emails=frozenset({"contato@emporiodamusica.com.br"}))
        self.assertEqual(p.scrub("Liga (67) 3341-4444 ou contato@emporiodamusica.com.br"),
                         "Liga (67) 3341-4444 ou contato@emporiodamusica.com.br")

    def test_scrub_obj_recurses(self):
        p = Pseudonymizer()
        obj = {"input": {"email_or_phone": "lucas.mendes@jmail.com"}, "list": ["x@y.com", 3, None], "n": 4}
        self.assertEqual(p.scrub_obj(obj), {"input": {"email_or_phone": "<EMAIL_1>"}, "list": ["<EMAIL_2>", 3, None], "n": 4})

    def test_public_contacts_from_data(self):
        phones, emails = public_contacts(load_policies(), load_store())
        self.assertTrue({"33414444", "33214500", "33251180"} <= phones)
        self.assertIn("contato@emporiodamusica.com.br", emails)


class EventLogTest(unittest.TestCase):
    def test_writes_scrubbed_lines_immediately(self):
        path = Path(tempfile.mkdtemp()) / "s" / "x.jsonl"
        log = EventLog(path, Pseudonymizer())
        log.write("customer", text="meu e-mail é lucas.mendes@jmail.com")
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)  # flushed before the next write
        log.write("tool", name="get_order", input={"order_id": 4, "email_or_phone": "lucas.mendes@jmail.com"}, result={"pedido": 4})
        events = read_events(path)
        self.assertEqual([e["kind"] for e in events], ["customer", "tool"])
        self.assertEqual(events[0]["text"], "meu e-mail é <EMAIL_1>")
        self.assertEqual(events[1]["input"]["email_or_phone"], "<EMAIL_1>")
        self.assertRegex(events[0]["t"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        self.assertNotIn("lucas", path.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8").splitlines()[1])["kind"], "tool")
```

- [ ] **Step 2: Run to verify RED** — `.venv/bin/python -m unittest tests.test_eventlog -q` → `ModuleNotFoundError: No module named 'src.eventlog'`.

- [ ] **Step 3: Implement `src/eventlog.py`**
  - E-mails: replace each `EMAIL_RE` match not in `keep_emails` (lowercased) with `<EMAIL_n>`, n assigned in first-seen order per lowercased value.
  - Phones: replace each `PHONE_RE` match whose last 8 digits are not in `keep_phones` with `<PHONE_n>`, keyed by the last 8 digits (so `(67) 99812-3456` and `99812-3456` share a tag).
  - E-mails first, then phones (same order as `session.mask`).
  - `EventLog.write`: `record = {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, **pseudo.scrub_obj(fields)}`; create parent dir; open in append mode, write one line, close (closing flushes).
  - The mapping lives only in the `Pseudonymizer` instance; it is never written anywhere.
  - Module docstring: why placeholders, not masking (spec §17 D14).

- [ ] **Step 4: GREEN** — targeted then full suite → `OK`.
- [ ] **Step 5: Commit** — `Add pseudonymized session event log writer`.

---

### Task 2: Version header

**Files:**
- Create: `src/versioning.py`, `tests/test_versioning.py`
- Modify: `src/llm.py` (`Turn` gets `model: str | None = None` as its LAST field; `SdkBackend.call` sets it from `getattr(resp, "model", None)`; `CliBackend` leaves it None), `tests/test_llm.py` (one assertion)

**Interfaces:**
- Produces:
  - `app_version(run=subprocess.run) -> dict` → `{"commit": "<40-hex>", "dirty": bool}`; any failure (non-zero exit, exception) → `{"commit": "unknown", "dirty": None}`. Runs `git rev-parse HEAD` and `git status --porcelain` with `cwd=ROOT`.
  - `sha12(text: str) -> str` — first 12 hex chars of SHA-256 of UTF-8 text.
  - `data_sha(data_dir: Path = DATA_DIR) -> str` — SHA-256 over (relative name, bytes) of every file under `data_dir`, sorted by relative path; first 12 hex.
  - `build_header(session_id: str, source: str, backend_label: str, system: str, tools: list[dict], now_override: str | None, run=subprocess.run, data_dir: Path = DATA_DIR) -> dict` → fields of spec §17 `header` (without `t`/`kind`): `schema` 1, `session`, `source`, `app`, `backend` (part of `backend_label` before `:`), `model` (part after `:`), `prompt_sha` (`sha12(system)`), `tools_sha` (`sha12(json.dumps(tools, sort_keys=True, ensure_ascii=False))`), `data_sha`, `now_override`.
  - `SOURCES = ("chat", "script", "example", "eval")`; `build_header` raises `ValueError` for another source.

- [ ] **Step 1: Failing tests** — `tests/test_versioning.py`:
```python
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.versioning import app_version, build_header, data_sha, sha12


def fake_run(outputs):
    def run(argv, **kw):
        out, code = outputs[argv[1]]
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")
    return run


class VersioningTest(unittest.TestCase):
    def test_app_version_clean_dirty_and_failure(self):
        sha = "a" * 40
        self.assertEqual(app_version(fake_run({"rev-parse": (sha + "\n", 0), "status": ("", 0)})), {"commit": sha, "dirty": False})
        self.assertEqual(app_version(fake_run({"rev-parse": (sha + "\n", 0), "status": (" M src/x.py\n", 0)}))["dirty"], True)
        self.assertEqual(app_version(fake_run({"rev-parse": ("", 128), "status": ("", 128)})), {"commit": "unknown", "dirty": None})

        def boom(argv, **kw):
            raise FileNotFoundError("git")
        self.assertEqual(app_version(boom), {"commit": "unknown", "dirty": None})

    def test_hashes_are_stable_and_sensitive(self):
        self.assertEqual(sha12("abc"), sha12("abc"))
        self.assertNotEqual(sha12("abc"), sha12("abd"))
        self.assertEqual(len(sha12("abc")), 12)
        d = Path(tempfile.mkdtemp())
        (d / "a.csv").write_text("1", encoding="utf-8")
        first = data_sha(d)
        (d / "a.csv").write_text("2", encoding="utf-8")
        self.assertNotEqual(first, data_sha(d))

    def test_header(self):
        run = fake_run({"rev-parse": ("b" * 40 + "\n", 0), "status": ("", 0)})
        h = build_header("20261102-100000-abcd", "chat", "sdk:claude-sonnet-5", "SYS", [{"name": "t"}], None, run=run)
        self.assertEqual((h["schema"], h["session"], h["source"], h["backend"], h["model"]),
                         (1, "20261102-100000-abcd", "chat", "sdk", "claude-sonnet-5"))
        self.assertEqual(h["app"], {"commit": "b" * 40, "dirty": False})
        self.assertEqual(h["prompt_sha"], sha12("SYS"))
        self.assertEqual(set(h), {"schema", "session", "source", "app", "backend", "model", "prompt_sha", "tools_sha", "data_sha", "now_override"})
        with self.assertRaises(ValueError):
            build_header("x", "prod", "sdk:m", "S", [], None, run=run)
```
  In `tests/test_llm.py` `SdkTest.test_maps_blocks_and_caches_system`, add `model="claude-sonnet-5-20261001"` to the fake response `NS(...)` and assert `turn.model == "claude-sonnet-5-20261001"`; in `CliTest.test_flags_env_and_tool_calls` assert `turn.model is None`.

- [ ] **Step 2: RED**, **Step 3: implement**, **Step 4: GREEN** (full suite), **Step 5: commit** — `Add the session version header and record the API-reported model`.

---

### Task 3: The agent emits events

**Files:**
- Modify: `src/agent.py`, `tests/test_agent.py`

**Interfaces:**
- Consumes: `EventLog` (Task 1), `Turn.model` (Task 2).
- Produces: `Agent(..., tickets_path=TICKETS_PATH, events: EventLog | None = None)` — new keyword-only-in-spirit last parameter. A private `_emit(kind, **fields)` that does nothing when `events is None`.

Emission points (spec §17 table), exactly:
- `customer` {text} — at the start of `reply()`.
- `model_call` {round, content, model, usage (dict of the 4 counters), latency_ms (int, wall-clock around `backend.call`)} — after every successful `backend.call`, with `content = turn.content` as returned (including any text that is later dropped).
- `tool` {name, input, result} — per tool call, full result dict (not truncated).
- `guard` {problems, rejected_text} — every guard failure: the rejected reply text, or `None` for an empty reply.
- `fallback` {reason: "guard" | "tool_cap", problems, text} — whenever `_fallback` produces the reply.
- `error` {error: exception class name} — when `backend.call` raises.
- `reply` {text} — when a guarded reply is returned (not for fallbacks or the tech-error text; those are covered by `fallback`/`error`).

- [ ] **Step 1: Failing tests** — append to `tests/test_agent.py` (reuse its `FakeBackend`, `text`, `tool`, `STORE`, `POLICIES`, `NOW`):
```python
from src.eventlog import EventLog, Pseudonymizer, read_events


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
```
  (Add `MAX_TOOL_ROUNDS` to the existing import from `src.agent` if not already there; `agent()` is the existing helper.)

- [ ] **Step 2: RED**, **Step 3: implement** (no change to control flow; every existing agent test must pass untouched), **Step 4: GREEN** full suite, **Step 5: commit** — `Emit session events from the agent loop`.

---

### Task 4: Labels — code signals, judge, label_session

**Files:**
- Create: `src/labels.py`, `tests/test_labels.py`
- Modify: `src/config.py` (`Settings` gets `judge: bool = False`, `judge_model: str = "claude-haiku-4-5"`, `dev_judge_model: str = "haiku"` as trailing defaulted fields; `load_settings` reads `LLM_JUDGE`, `LLM_JUDGE_MODEL`, `LLM_DEV_JUDGE_MODEL` with the same truthy rule as `LLM_DEV`), `tests/test_config.py` (defaults + override), `src/llm.py` (`make_judge_backend(settings)` → `CliBackend(settings.dev_judge_model)` when `settings.dev`, else `SdkBackend(settings.api_key, settings.judge_model, max_tokens=512)`; empty key → `LLMError` like `make_backend`), `.env.example` (the three keys, commented)

**Interfaces:**
- Produces:
  - `RUBRIC_VERSION = "1"`, `RESOLUTIONS = ("resolved", "partial", "unresolved", "handed_off")`
  - `code_signals(events: list[dict]) -> dict` with exactly: `turns` (# customer events), `tickets` {"complaint": n, "human": n} (from `tool` events named `register_ticket` whose result has `protocolo`, by `input.type`; plus `fallback` events whose text contains "protocolo" count as human), `guard_hits` (# guard events), `fallbacks` (# fallback events), `tool_cap_hit` (any fallback with reason tool_cap), `order_misses` (# get_order tool results with `tentativas_restantes`), `order_locked` (any get_order result whose `erro` contains "bloqueada"), `tech_errors` (# error events), `repeated_question` (any two customer texts with `difflib.SequenceMatcher` ratio ≥ 0.9 after lowercasing and collapsing spaces).
  - `transcript_text(events) -> str` — lines `CLIENTE: …` / `DUDA: …` from `customer`, `reply` and `fallback` events in order (fallback text is what the customer saw).
  - `JUDGE_SYSTEM` — the rubric (below), verbatim.
  - `judge(events, backend) -> dict` — calls `backend.call(JUDGE_SYSTEM, [{"role": "user", "content": transcript_text(events)}], [])`; parses `turn.text` with `src.llm.extract_json`; validates `resolution ∈ RESOLUTIONS`, `frustration ∈ {0,1,2}` (int, not bool), evidences are strings; adds `resolution_evidence_found` / `frustration_evidence_found` = evidence is empty or occurs verbatim (case-insensitive, whitespace-collapsed) in the transcript. Any failure (exception, bad JSON, bad enum) → `{"error": "<short reason>"}`; never raises.
  - `label_session(path: Path, judge_backend=None, judge_model: str | None = None) -> dict` — reads events, builds `{"rubric_version", "signals", "judge" (dict or None), "judge_model"}`, appends it as a `labels` event via a plain append (no pseudonymizer needed: it only contains already-scrubbed text) and returns it.
  - `latest_labels(events) -> dict | None` — the last `labels` event.

Judge rubric (`JUDGE_SYSTEM`, English, verbatim):
```
You label one finished customer-service chat between a music store's assistant (DUDA) and a customer (CLIENTE). The chat is in Portuguese. Read it and reply with ONE JSON object and nothing else:
{"resolution": "resolved" | "partial" | "unresolved" | "handed_off", "resolution_evidence": "<short exact quote from the chat>", "frustration": 0 | 1 | 2, "frustration_evidence": "<short exact quote from the chat, or empty if 0>"}
resolution: resolved = everything the customer asked was answered or done, nothing left pending; a polite, correct refusal of an out-of-scope request counts as resolved. partial = some of the requests were answered, some were not. unresolved = the main request was not answered, was answered wrongly, or the chat ended mid-request. handed_off = the main outcome is a referral to a human (a ticket/protocol, or "talk to the team").
frustration: 0 = none. 1 = mild: impatience, repeating the question, short annoyed replies. 2 = clear: complaints about the service or the assistant, anger, giving up.
Quotes must be copied exactly from the chat. Judge only what is in the chat.
```

- [ ] **Step 1: Failing tests** — `tests/test_labels.py`:
```python
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
```
  In `tests/test_config.py` add: defaults `(s.judge, s.judge_model, s.dev_judge_model) == (False, "claude-haiku-4-5", "haiku")`, and `LLM_JUDGE=true` → `judge is True`.

- [ ] **Step 2: RED**, **Step 3: implement**, **Step 4: GREEN** full suite, **Step 5: commit** — `Label finished sessions from code signals and an optional judge`.

---

### Task 5: Wiring — CLI, evals, report

**Files:**
- Modify: `src/cli.py`, `evals/run_evals.py`, `tests/test_agent.py` (CLI test), `tests/test_evals.py`
- Create: `scripts/report.py`, `tests/test_report.py`

**Interfaces:**
- CLI: new flags `--judge` (store_true) and `--source` (choices `chat`, `script`, `example`; default `script` when `--script` is given, else `chat`). Flow in `main`:
  1. after creating the session: `keep_phones, keep_emails = public_contacts(policies, store)`; `log = EventLog(SESSIONS_DIR / f"{session.id}.jsonl", Pseudonymizer(keep_phones, keep_emails))`; `log.write("header", **build_header(session.id, source, backend.label, agent.system, TOOLS, args.now))`; pass `events=log` to `Agent` (build the agent first, or compute the system prompt the same way — the header's `prompt_sha` must hash exactly `agent.system`).
  2. after the conversation: `log.write("end")`; then `judge_on = args.judge or settings.judge`; `label_session(log.path, make_judge_backend(settings) if judge_on else None, <judge model name> if judge_on else None)` inside `try/except Exception` that prints `aviso: rótulos não gerados (<ExcName>)` and continues (labelling must never break a chat). Judge model name: `settings.dev_judge_model` when `settings.dev` else `settings.judge_model`.
  3. replace `_keep_phones` with the phones from `public_contacts` (delete `_keep_phones`; masking result must stay identical — the existing CLI test proves it).
  4. print the `.md` path as today, then the `.jsonl` path on the next line.
  - `main(argv=None, backend=None, judge_backend=None)`: tests inject a fake judge; when `judge_backend` is given and judging is on, use it instead of `make_judge_backend`.
- Evals: `run_case(case, backend, events_dir: Path | None = None)` — when `events_dir` is given, log to `events_dir / f"{case['id']}.jsonl"` with header `source="eval"`, then write `end` and `label_session(path)` (code signals only; evals never call the judge). `main` passes `transcripts_dir` (the existing per-run folder). The pass/fail logic and output lines are unchanged.
- `scripts/report.py`:
  - `load_sessions(dirs: list[Path]) -> list[dict]` — each `*.jsonl` file → `{"path", "header", "labels"}` (header = first `header` event, labels = `latest_labels`); files without a header are skipped.
  - `summarize(sessions: list[dict], include_all: bool = False) -> dict` — keep `source == "chat"` unless `include_all`; group by `(app.commit[:7] + ("-dirty" if dirty else ""), prompt_sha, model)`; per group: `sessions`, `labelled` (has labels), `judged` (labels.judge without "error"), `resolution` rates (share of judged per value, 3 decimals), `frustrated_rate` (judged with frustration ≥ 1), `fallback_rate`, `guard_hit_rate` (share of labelled sessions with ≥ 1), `tech_error_rate`, `handoff_tickets` (sum of human tickets). Rates are `None` when the denominator is 0. Returns `{"generated_at", "filter": "chat" | "all", "groups": [ {"version": {...}, ...} ]}`.
  - `main(argv=None, judge_backend=None) -> int` — flags `--all`, `--label`, `--dir` (default `SESSIONS_DIR`). `--label`: for each session without a judge label for `RUBRIC_VERSION`, run `label_session(path, judge)` (judge = `judge_backend` or `make_judge_backend(load_settings())`). Then summarize, print one plain-text table row per group, and write `RUNTIME_DIR / "summary.json"` atomically (write `summary.json.tmp`, then `os.replace`). Returns 0.
  - Run as `python -m scripts.report`.

- [ ] **Step 1: Failing tests**
  - `tests/test_agent.py` `CliTest`: extend `test_script_run_writes_masked_transcript` — after the run, the session `.jsonl` exists in a temp `SESSIONS_DIR` (patch `src.cli.SESSIONS_DIR` with `unittest.mock.patch`), first event is a `header` with `source == "script"`, last event is `labels` with `judge is None`, the file does not contain `lucas.mendes`, and contains `<EMAIL_1>`. Add `test_judge_flag_uses_judge_backend`: run with `--judge` and `judge_backend=FakeJudge('{"resolution": "resolved", "resolution_evidence": "", "frustration": 0, "frustration_evidence": ""}')` (import `FakeJudge` pattern locally) → last event's `judge["resolution"] == "resolved"`. Add `test_label_failure_does_not_break_chat`: `judge_backend` whose `call` raises → exit code 0, labels event has `judge == {"error": ...}` (judge() never raises) — and a separate case where `label_session` itself is patched to raise → exit code 0 and stdout contains `aviso: rótulos não gerados`.
  - `tests/test_evals.py`: `run_case(case, FakeBackend-like, events_dir=tmp)` writes `<id>.jsonl` with `source == "eval"` and a final `labels` event; `run_case` without `events_dir` writes nothing.
  - `tests/test_report.py`: build 4 synthetic session files in a temp dir (2 chat sessions same version: one judged resolved/frustration 0 with a fallback, one judged handed_off/frustration 2; 1 chat session from another commit, unlabelled; 1 eval session) and assert: default filter has 2 groups and excludes the eval; group 1 `sessions == 2`, `judged == 2`, `resolution["resolved"] == 0.5`, `frustrated_rate == 0.5`, `fallback_rate == 0.5`; the unlabelled group has rates `None`; `--all` includes the eval; `main(["--dir", tmp])` writes `summary.json` (patch `RUNTIME_DIR`) that parses and equals `summarize(...)` minus `generated_at`; `--label` with a fake judge appends judge labels only to sessions lacking a rubric-1 judge label.
- [ ] **Step 2: RED**, **Step 3: implement**, **Step 4: GREEN** full suite, **Step 5: commit** — two commits: `Log, label and tag every CLI and eval session` and `Add the session report`.

---

### Task 6: Live check and README

**Files:**
- Modify: `README.md`, `.env.example` (if Task 4 missed it), `spec/SPEC.md` layout block (§3: add `src/eventlog.py`, `src/versioning.py`, `src/labels.py`, `scripts/report.py`)

- [ ] **Step 1: Live check, dev backend** (source the controller's devenv helper as instructed in the dispatch): run `python -m src.cli --script examples/scripts/04-arrependimento-pedido.txt --now 2025-12-26T10:00 --judge` and `python -m src.cli --script examples/scripts/05-cordas-loja-parceira.txt --source chat --judge`; then `python -m scripts.report --all`. Check by reading the files: header complete (commit = HEAD, dirty flag right), no real e-mail/phone, `<EMAIL_1>` consistent between customer text and the `get_order` input, labels present with judge output and `*_evidence_found`. Paste the report output and one labels event into the task report.
- [ ] **Step 2: README** — STE plain English, same voice as the existing README:
  - Index: add "Session logs and labels".
  - New section "Session logs and labels", first line signal-heavy (e.g. "Every session is also saved as structured, pseudonymized, versioned events, so it can be replayed, measured and turned into training data later."). Then: what a log holds (the event kinds, one line each), placeholders vs masking and why, the version header and why more than the commit, labels (code signals always; judge opt-in, Haiku 4.5, rubric versioned, evidence checked; re-runnable), the report (rates per version, chat-only by default, rebuilt not incremented). One short "Avoided" list: self-reported end-session tool, running counter, training format in the app — one reason each. One line on D12: the evals still use no LLM judge; the judge only labels real sessions for analysis.
  - Quick start: `--judge`, `--source`, `python -m scripts.report [--all] [--label]`, the three `LLM_JUDGE*` settings in the settings table.
  - Tests section: new unit test count (run the suite).
  - Limitations: judge labels are model opinions (evidence-checked, not ground truth); logs grow without rotation.
- [ ] **Step 3: Verify** — full unit suite `OK`; every README link resolves; commit `README: session logs, labels and report`.
