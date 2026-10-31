# Duda — customer-service agent for Empório da Música

## Index

1. [What it is and quick start](#what-it-is-and-quick-start)
2. [Features](#features)
3. [How it works](#how-it-works)
4. [Choices](#choices)
5. [Security: assume the model is hostile](#security-assume-the-model-is-hostile)
6. [Data handling](#data-handling)
7. [Session logs and labels](#session-logs-and-labels)
8. [Assumptions](#assumptions)
9. [Tests, evals and edge cases](#tests-evals-and-edge-cases)
10. [Examples](#examples)
11. [Limitations and what we would do with more time](#limitations-and-what-we-would-do-with-more-time)
12. [AI use and time](#ai-use-and-time)

The full design is in [`spec/SPEC.md`](spec/SPEC.md).

## What it is and quick start

Duda is a text chat agent for Empório da Música, a fictional instrument store in Campo Grande/MS.
She answers customers in Brazilian Portuguese from the store's CSV data and its policy manual.
The model picks the tool. Code computes every price, checks every order lookup, and checks every reply.

You need Python 3.11 or later and an Anthropic API key.

```bash
python3 -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -r requirements.txt     # anthropic, nothing else
cp .env.example .env                # then set LLM_API_KEY in .env
python -m src.cli                   # chat; type "sair" to quit
```

No API key? Set `LLM_DEV=true` in `.env`. Each model call then goes through your local `claude` CLI
and your Claude subscription. The CLI must be installed and logged in.
On a normally logged-in machine, the `claude` CLI adds the account owner's e-mail to the model's context.
For clean dev runs, create a token with `claude setup-token` and export it as `CLAUDE_CODE_OAUTH_TOKEN` in your shell (the app does not read it from `.env`). The backend then runs the CLI with an empty home.

| Setting | Default | Effect |
|---|---|---|
| `LLM_API_KEY` | empty | Anthropic API key. Required when `LLM_DEV=false`. |
| `LLM_MODEL` | `claude-sonnet-5` | Model for the SDK backend. `claude-haiku-4-5` also works. |
| `LLM_DEV` | `false` | `true` sends each model call through `claude -p` instead of the SDK. |
| `LLM_DEV_MODEL` | `sonnet` | Model for the CLI backend. |
| `LLM_JUDGE` | `false` | `true` labels every CLI session with the LLM judge, like `--judge`. |
| `LLM_JUDGE_MODEL` | `claude-haiku-4-5` | Judge model for the SDK backend. |
| `LLM_DEV_JUDGE_MODEL` | `haiku` | Judge model for the CLI backend. |

Environment variables with the same names override `.env`.

Two more ways to run it:

```bash
python -m src.cli --now 2026-11-01T20:00     # pretend it is Sunday 20:00 (store closed)
python -m src.cli --script examples/scripts/04-arrependimento-pedido.txt --now 2025-12-26T10:00
```

`--script` replays a file of customer lines, one message per line. `--now` fixes the store clock.
Each run writes a masked transcript to `_runtime/sessions/`, or to the path in `--out`. It also writes
an event log to `_runtime/sessions/<id>.jsonl` (see [Session logs and labels](#session-logs-and-labels)).

```bash
python -m src.cli --judge                    # at the end, also label the session with the LLM judge
python -m src.cli --script examples/scripts/05-cordas-loja-parceira.txt --source example   # tag: chat, script or example
python -m scripts.report                     # outcome rates per version, chat sessions only
python -m scripts.report --all --label       # all sessions; judge the unjudged ones first
```

By default a `--script` run is tagged `script` and a live chat is tagged `chat`.

## Features

Ten behaviours. Everything that touches money, orders or the final reply is enforced in code.

- **Persona.** Duda follows the manual's §7 tone: informal but professional, short WhatsApp-sized replies, Portuguese only.
- **Data vs policy lookups.** Stock, prices, promotions and orders come from tools that read the CSVs. Store rules come from `read_policy`, one manual section per call.
- **Prices computed in code.** Promo price, PIX discount, installments, combined payment and shipping come out of `src/money.py` as ready strings ("R$ 2.199,00"). The model copies them.
- **Order lookup with verification.** `get_order` needs the order number and the e-mail or phone on file. Three misses lock it for the session. It returns the first name only.
- **Return windows.** For a delivered order, `get_order` returns each window (arrependimento, troca por preferência, troca por defeito, garantia legal) as open or closed, with its deadline.
- **Partner stores.** The store sells only instruments. Accessory requests go to partner stores from `data/partner_stores.csv`.
- **Complaints and handoff tickets.** `register_ticket` writes a protocol (`EM-YYYYMMDD-NNNN`) to `_runtime/tickets.jsonl` and promises a reply in 24 business hours.
- **Out-of-hours awareness.** Every customer turn starts with the store time and whether the store is open. When closed, Duda gives the reopening time.
- **Reply guard.** Every money amount ("R$ 1.500" or "1.500 reais"), phone number, e-mail, link and bare domain in a reply must appear in a tool result, the manual, the system prompt or the customer's own messages. A failed or empty reply gets one retry, then a safe fallback and a human ticket.
- **Masked transcripts.** Every session is saved with its tool calls. E-mails and customer phones are masked (`l***@jmail.com`, `(67) 9****-3456`).

## How it works

One loop in `src/agent.py`: the model asks for tools until it can answer, code runs the tools, and a guard checks the answer.

```
customer text
   │
   ▼
Agent.reply(text)                              src/agent.py
   │  history + time line ("agora: …")
   ▼
backend.call(system, messages, tools) ──► Turn{text, tool_calls, usage} src/llm.py
   │      ├─ SDK backend (LLM_DEV=false): native tool_use
   │      └─ CLI backend (LLM_DEV=true): claude -p, tools described in prompt,
   │                                     reply is JSON {"text", "tool_calls"}
   ▼
tool_calls? ──yes──► run_tool(name, args, ctx) → result JSON ──► back to backend.call
   │                (input checks, per-session limits)       (max 6 tool rounds/turn)
   no
   ▼
guard.check(reply, allowed) ──fail──► one retry with the reason ──fail──► safe fallback + ticket
   │ pass
   ▼
reply to customer, appended to masked transcript
```

Each customer message gets a time line, for example
`[agora: seg 02/11/2026 10:00, horário de Campo Grande (UTC−4) — loja aberta | fecha às 18:00]`.
The model sees the system prompt, the session history and the 8 tool schemas. The SDK backend
caches the system prompt. When the model asks for a tool, `run_tool` (`src/tools.py`) checks the arguments against the
schema and returns JSON with Portuguese labels and preformatted money, so the model copies numbers
instead of computing them. The loop allows 6 tool rounds per turn. If the model writes text beside a
tool call, the loop logs that text and drops it, so only the final answer reaches the customer. When
the model answers in text, `src/guard.py` checks every amount, phone, e-mail, link and bare domain in it.

Only the one model call differs between the two backends. So the dev backend runs the same loop,
history, guard and transcript code that production runs.

| Module | Job |
|---|---|
| `src/config.py` | `.env` loader and settings |
| `src/clock.py` | store time (UTC−4), opening hours, next opening |
| `src/data.py` | CSV loading, normalisation, product search |
| `src/money.py` | BRL formatting; promo, PIX, installment and shipping math |
| `src/policies.py` | splits `data/policies.md` into sections |
| `src/tools.py` | the 8 tools: schemas, input checks, session limits |
| `src/prompt.py` | system prompt and the per-turn time line |
| `src/llm.py` | `Turn` type, SDK backend, CLI backend |
| `src/guard.py` | reply guard |
| `src/session.py` | session state, masking, transcripts, tickets |
| `src/agent.py` | the loop |
| `src/cli.py` | chat and `--script` replay |
| `src/eventlog.py` | pseudonymized JSONL event log |
| `src/versioning.py` | version header: commit, prompt, tool and data hashes |
| `src/labels.py` | outcome labels: code signals and the LLM judge |
| `scripts/report.py` | outcome rates per version |

## Choices

Every choice favours fewer moving parts that a reviewer can trace.

| Decision | Chosen | Avoided, and why |
|---|---|---|
| Agent approach | **Own tool loop; code does the math.** The model picks which of the 8 tools to call. Code computes every number. | LangChain/LangGraph: extra dependencies, harder to trace, no gain at 8 tools. A SQL agent: a model that writes free SQL over customer data is a leak path (LGPD). |
| Model | **Claude Sonnet 5: reliable tool choice and natural Brazilian Portuguese.** It runs through the Anthropic SDK. Haiku 4.5 is the cost swap: set `LLM_MODEL=claude-haiku-4-5`. | Multi-provider support: more code and tests for no graded gain. |
| Prompt strategy | **One static, cached system prompt; facts stay behind tools.** It holds the identity, the manual's §7 verbatim as the tone and flow rules, 10 numbered hard rules, a security block, style rules (one says: no text beside tool calls), and the names of the policy sections and categories. The time line goes into each customer turn, outside the cache. Policy bodies stay behind `read_policy`, so each lookup shows in the transcript. | Catalog and manual pasted into the prompt: you cannot see which facts an answer used. A prompt that changes every turn: it breaks the cache. |
| Policies | **One tool per manual section; no vector search, 8 pages don't need it.** The PDF manual is transcribed to `data/policies.md`, and a unit test checks every line against the PDF. The prompt lists the section names. `read_policy(section)` returns one section. | The whole manual in the prompt (about 5K tokens): it works, but you cannot see when the agent used it. Embeddings: a wrong chunk is a new failure mode. |
| Dev backend | **Same loop, model call swapped.** `LLM_DEV=true` sends the call through `claude -p` with all built-in tools off. | The CLI with our tools over MCP: the CLI would run its own loop, so dev would test different code than production ships. |
| Interface | **CLI chat plus `--script` replay.** Replay writes a transcript. | A web UI or API: the brief puts the focus on the agent. |
| History | **One session only; saved masked.** | Memory across sessions: there is no login, so we cannot know it is the same customer. Keeping personal data without consent conflicts with LGPD (manual §9). |
| Order privacy | **Order number plus contact on file; 3 misses lock it.** | Name only: two customers go by Diego Castro. A code by e-mail or SMS: the e-mails are fake domains, and evaluators cannot read those inboxes. |
| Installments | **Manual §3.1 tiers win: R$50 / R$80 / R$100 minimum.** | The summary table's flat R$100 minimum: it contradicts the detailed rules in §3.1. |
| Time | **Fixed UTC−4, shown as Campo Grande time.** Campo Grande has had no DST since 2019. `--now` overrides the clock. | `zoneinfo`: it needs the `tzdata` package on Windows. A fixed offset is correct here and needs nothing. |
| Accessories | **Synthetic partner-store CSV.** | Store names in the prompt: nobody can check them, and they break the "only say what the data says" rule. |
| Dependencies | **`anthropic` only at runtime.** Our own short `.env` parser. `pypdf` only for the policy-text script (`python -m scripts.pdf_text`) and the test that uses it. | pandas: not needed for 65 rows. python-dotenv: one more package for one file. |
| Tests | **stdlib `unittest`; evals checked by code.** | pytest: an extra dependency for no gain. An LLM judge: non-deterministic grading of a non-deterministic system. |

## Security: assume the model is hostile

The model can act only through 8 tools and its reply text, and code bounds each one.
The threat model: the model believes an injected instruction, or acts against the store for any other reason.

| Vector | Worst a hostile model could do | Code guard |
|---|---|---|
| `search_products`, `get_product`, `list_active_promotions`, `read_policy`, `find_partner_stores` | Read public catalog and policy data | Nothing to protect. `read_policy` takes a fixed list of section names, not paths. |
| `quote` | Nothing. It only computes from CSV prices. | Prices never come from the model. |
| `get_order` | Probe other customers' orders | Contact match in code. 3 misses lock it for the session. One generic error for "no such order" and "wrong contact". It never returns the full name, e-mail, phone or city. No tool lists customers. |
| `register_ticket` | Flood or poison the staff queue | At most 3 per session, at most 500 characters, control characters stripped, written as one JSON line. |
| Reply text | Invent a price, phone, e-mail or link | The reply guard. |
| Files, shell, network | — | No tool touches them. Code generates every file name. The dev CLI runs with `--tools ""`. |
| Script input (`--script`) | — | Each line is only a customer message. |

Every tool checks its arguments against its schema (types, enums, ranges, string lengths)
and never raises. Bad input returns `{"erro": "..."}`.

**Remaining risks.** The guard cannot catch three things:

- A false statement with no number in it, such as "you can return it in 60 days". The evals check policy answers for the right facts instead, such as the return deadline.
- A number the customer typed. The customer's own messages are in the allowed set, so the model could repeat a false price the customer quotes. Eval `e15` measures this; both final runs passed it.
- A number spelled out in words, such as "mil e quinhentos reais".

## Data handling

The data files are used unchanged. Code applies the rules below, and the agent says only what the data says.

**Product status.** Exact stock counts are never shown.

| Data state | Label | Behaviour |
|---|---|---|
| `active`, stock 3 or more | "disponível" | normal |
| `active`, stock 1–2 | "últimas unidades" | normal |
| `active`, stock 0 | "temporariamente indisponível" | plus up to 3 in-stock alternatives, same category, nearest price |
| `discontinued` | "fora de linha" | plus up to 3 alternatives, same category |
| `coming_soon` | "em breve" | cannot be quoted or bought |
| Woodwinds, brass, orchestral strings (no products) | "no momento não temos modelos dessa categoria" | the store does sell them, so Duda never says "we don't sell" |

Search ignores accents and case, splits letters from digits ("gd 20" finds "GD20"), and tolerates
small typos ("takamini" finds Takamine). List results leave out unavailable items by default. `get_product` finds them all.

**Known data issues.**

- Two order totals do not match their items: order 3 (R$ 3.450,00 stored, R$ 3.498,00 from items) and
  order 20 (R$ 1.400,00 stored, R$ 1.488,00 from items). We always show the stored total, because it is what the customer paid.
- Product 130 is `coming_soon`, but it is in delivered order 3. The order lookup shows it as bought. The catalog shows it as "em breve".
- The manual says "300+ instruments". The data has 65, and woodwinds, brass and orchestral strings have none. Duda claims only what the data has.
- Promotions have no dates, only `is_active`. Only the 4 active ones count. Duda never shows an inactive promotion.

**Installments.** The manual's §3.1 says "sem valor mínimo de parcela (exceto abaixo de R$ 50,00)".
We read it as a R$50 minimum installment. The full ladder: R$50 for 1–3x, R$80 for 4–6x, R$100 for 7–12x.
The §3.1 detail wins over the summary table's flat R$100.

**PIX.** The 5% PIX discount applies only to items without an active promotion. The manual says the two do not stack.

**Partner stores** are synthetic (`data/partner_stores.csv`, same CSV style as the given data).
The manual says "suggest partner stores when possible", but it names none.

## Session logs and labels

Every session is also saved as structured, pseudonymized, versioned events, so it can be replayed, measured and turned into training data later.
Logging only observes: Duda's replies, tools and guard work as before. The design is in [spec §17](spec/SPEC.md#17-session-logs-labels-and-report).

The CLI writes `_runtime/sessions/<id>.jsonl`, also when `--out` moves the `.md` transcript. The eval runner writes
one log per case to `_runtime/evals/`. Each event is one JSON line, written and flushed when it happens, so a crash keeps everything before it.

| Event | Holds |
|---|---|
| `header` | the version fields below, the source (`chat`, `script`, `example`, `eval`) and the `--now` override |
| `customer` | one customer message |
| `model_call` | the blocks the model returned, the model id the API reports (null on the CLI backend), token usage, latency |
| `tool` | the tool name, its input and the full result |
| `guard` | a rejected or empty reply and the reasons |
| `fallback` | the safe fallback text and its reason (`guard` or `tool_cap`) |
| `error` | the exception class of a failed model call |
| `reply` | the reply the customer saw |
| `end` | the end of the session |
| `labels` | outcome labels, added after the session |

**Placeholders, not masking.** Customer e-mails and phones become `<EMAIL_1>`, `<PHONE_1>` and so on.
A phone is found by the shape of a Brazilian number: an optional `+55`, an optional DDD (2 digits, with or without
parentheses or a leading 0), then a mobile 9 plus 8 digits or a landline starting with 2 to 5 plus 7 digits.
Spaces, dots or hyphens may separate the groups, as in `(67) 9 9812-3456`, `998123456` or `067998123456`.
The same value keeps the same tag for the whole session, and the value-to-tag mapping is never stored.
The `.md` masking is not enough here: two different e-mails can mask to the same `l***@jmail.com`, so a
replay can show one value where the customer typed several. Store and partner contacts stay readable.

**Version header.** Each log starts with the git commit and a dirty flag, the backend and the configured model,
and hashes of the system prompt, the tool definitions and `data/`. The commit alone is not enough: a dirty run
can carry a different prompt or data under the same commit, and the hashes tell those runs apart. The report groups sessions by all of them.

**Labels.** At the end of a session, code appends a `labels` event with the rubric version.

- Code signals, always: turns, tickets, guard hits, fallbacks, the tool-round cap, order misses and lockout, technical errors, and a repeated customer message.
- An LLM judge, on request (`--judge` or `LLM_JUDGE=true`): Claude Haiku 4.5 reads the chat and returns a resolution (`resolved`, `partial`, `unresolved`, `handed_off`) and a frustration level from 0 to 2, each with a quote. Code checks that each quote is in the chat and records the result as `resolution_evidence_found` and `frustration_evidence_found`. A failed judge call records an error and does not stop the run.
- Labels can be redone: `python -m scripts.report --label` judges every session that has no valid judge label for the current rubric. It appends a new `labels` event, and the newest one counts. Eval logs get code signals only.

In the live check, two sessions on each backend, the judge labelled all 4 sessions and code found every quote in the chat.

**Report.** `python -m scripts.report` reads the logs in `_runtime/sessions/` (`--dir` picks another folder) and prints rates per version group (commit and dirty flag, prompt, tools and data hashes, model):
resolution, frustration, fallback, guard hit and technical error, plus the number of handoff tickets. It writes the same
numbers to `_runtime/summary.json`. It counts `chat` sessions only, because script, example and eval runs are not real
customers. `--all` counts them too. It rebuilds the numbers from the logs on every run, so a late label shows on the
next run. Only judge labels made with the current rubric count as judged. After a rubric change, `--label` judges the sessions again.

Avoided:

- An `end_session` tool where Duda reports success and frustration: the model would grade its own work, most chats end without the call, and a possibly hostile model gets one more write path.
- A counter updated after each session: two processes race on it, a crash loses it, and it cannot be recomputed when the rubric changes.
- A training format written by the app: training formats change, raw events do not. A separate step can convert the logs later.

The evals still use no LLM judge (see [Choices](#choices)). The judge only labels real sessions for analysis. It never decides whether an eval passes.

## Assumptions

Each reading below fills a gap in the manual or the data. The installment reading, the stored totals
and the synthetic partner stores are in [Data handling](#data-handling).

- **Receipt date = `estimated_delivery`.** The data has no real delivery date. The return windows count from it, and Duda tells the customer so.
- **"Região metropolitana de Campo Grande" = the city of Campo Grande.** The local delivery rules in §5.1 apply only there.
- **The free-shipping threshold uses the subtotal after promotions, before the PIX discount.**
- **"Contrabaixos" in §5.2 (special freight) are acoustic double basses**, an empty category, not electric basses.
- **The §4.3 preference exchange counts 7 days from receipt**, like arrependimento.

## Tests, evals and edge cases

181 unit tests check the code without a model. 27 live cases check the agent end to end. The final SDK run passed 26 of 27.

```bash
python -m unittest discover -s tests -t . -q      # 181 tests, no model calls; the PDF check skips without pypdf
python -m evals.run_evals                         # 27 live cases on the configured backend
python -m evals.run_evals --file edge --only e09-injecao
```

**Unit tests** (`tests/`) cover search and typos, status labels and alternatives, every `quote` rule
(promo, PIX exclusion, the installment edges at R$50/80/100, combined payment, shipping thresholds),
the clock, order match, mismatch and lock, return windows, tool input checks, the reply guard and every
phone form it must find or skip, masking, ticket limits, the agent loop (retries, empty replies, the
tool-round cap), the CLI backend's JSON parsing and history, and the session logs (placeholders, the version
header, labels with a fake judge, the report).

**Evals** (`evals/cases.jsonl`, 12 cases) are scripted conversations for the brief's scenarios.
Code checks each one: which tools were called (`must_call`, `must_call_any`, `must_not_call`) and
which facts the replies contain (`must_contain`, `must_not_contain`). No LLM grades them.

**Edge cases** (`evals/edge_cases.jsonl`, 15 cases) are a separate synthetic file on purpose: they test
the paths the five examples don't reach. These are out of stock, discontinued, coming soon, a typo in
a product name, an empty category, a promo price without the PIX discount, a return inside and outside
the window, wrong contacts and the lockout, prompt injection, a fake manager, accessories, off-topic
questions, out of hours, and a false price the customer quotes.

| Backend | `cases.jsonl` | `edge_cases.jsonl` |
|---|---|---|
| SDK, `claude-sonnet-5` (final run) | 12/12 | 14/15 |
| Dev CLI, `sonnet` (final run, after the history fix) | 12/12 | 14/15 |

Both runs come after the final fixes. The one SDK failure is a tool-use miss: in `e13-fora-do-horario`
the model gave the correct reopening time but asked for the subject before it opened the ticket. The
one dev failure, `e07`, is a reply that stopped early; the previous dev run passed it. Dev
runs before the history fix are not comparable: the dev model did not see its own earlier replies.
Details per case: [`evals/RESULTS.md`](evals/RESULTS.md).

The order lockout is proven by unit tests, not by a live run. In the live evals the model never
tried a fourth lookup after the misses, so no live call reached the locked branch.
`tests/test_tools.py` proves that a fourth `get_order` is refused, even with the correct contact.

Each eval run writes its results and transcripts to `_runtime/evals/`.

## Examples

Five real CLI runs on the SDK backend, made after the final fixes, replayed from `examples/scripts/` and saved as masked transcripts. Nobody edited them.

1. [Violões até R$1000](examples/01-violoes-ate-1000.md): a catalog search with a price limit. It shows the 10 cheapest of 12 and says there are 2 more, then gives a beginner recommendation.
2. [Endereço e horário](examples/02-endereco-e-horario.md): the address and Saturday hours, from two policy lookups.
3. [Preço do Takamine GD20](examples/03-preco-takamine.md): the price, the PIX price and installments, all computed in code.
4. [Arrependimento de compra](examples/04-arrependimento-pedido.md): the non-trivial one. Identity check, order lookup and the return window on 26/12/2025, counted from the estimated delivery date.
5. [Cordas e loja parceira](examples/05-cordas-loja-parceira.md): accessories the store does not sell, sent to partner stores. This example had an acceptance rule: Duda must say the store sells only instruments and must call `find_partner_stores`. Its first run passed, and that run is the one shown.

To regenerate them:

```bash
for s in examples/scripts/*.txt; do
  n=$(basename "$s" .txt); now=2026-11-02T10:00
  [ "$n" = 04-arrependimento-pedido ] && now=2025-12-26T10:00
  python -m src.cli --script "$s" --now "$now" --source example --out "examples/$n.md"
done
```

## Limitations and what we would do with more time

The largest gap: code cannot catch a false statement that has no number in it.

- **The guard checks numbers and contacts only.** It misses false statements with no number, numbers the customer typed, and numbers spelled out in words (see [Security](#security-assume-the-model-is-hostile)).
- **Order verification happens in the chat.** In production we would send a one-time code to the email or SMS on file before showing the order.
- **The model can skip a tool.** The final SDK run had 1 such miss out of 27 (`e13`). Each eval ran once, so the run gives no pass rate. With more time we would run each case several times and report the rate.
- **Dev-mode tool choice is emulated.** The CLI backend asks the model for a JSON envelope. It is a close copy of native tool use, not the real thing. The final examples and eval numbers come from the SDK.
- **No cart, checkout or real shipping calculation by CEP.** Shipping outside Campo Grande is "calculated by CEP at checkout".
- **No holidays or special-date hours.**
- **No memory across sessions**, by choice (see [Choices](#choices)).
- **Judge labels are model opinions.** Code checks that each quoted piece of evidence is in the chat, but a label is not ground truth. The judge sees only the messages, not the tool results or the store clock. In one live dev run it marked as resolved a reply that wrongly said the return window had closed.
- **Session logs grow without rotation.** Each session adds a file to `_runtime/sessions/`, and nothing deletes old ones.
- **Session logs keep the customer's first name.** `get_order` returns it, and the log stores tool results in full.
- **Dev runs can ignore `--now`.** The `claude` CLI tells the model the machine's real date, so a dev reply can contradict the `--now` clock. SDK runs follow it.
- **Out of scope:** WhatsApp integration, a web UI, replies in other languages.

## AI use and time

Claude Code built this, running as "Straxia", an agent on QUYSS (the author's own fleet manager for AI agents). The author made every design decision.

1. The agent read the brief and the data, then grilled the author in 3 question rounds. Each question came with a recommended answer.
2. It wrote [`spec/SPEC.md`](spec/SPEC.md) from the answers.
3. It wrote the 12-task plan in [`docs/superpowers/plans/`](docs/superpowers/plans/2026-10-31-emporio-agent.md).
4. It built the plan with the superpowers workflow: one fresh implementer sub-agent per task under TDD, then one reviewer sub-agent per task. Review findings went back to the implementer. Three tasks needed one fix round: a crash on non-dict tool arguments, a history-cleanup bug after a guard retry, and an eval that passed without testing what its name said.
5. Live evals ran on the dev CLI backend during the build. The SDK produced the final examples and eval numbers.
6. A final review of the whole branch found 1 critical and 8 important issues. The critical one: the product search cut 12 results to 10 without telling the model, so the first example left out two guitars. One important one: the dev backend dropped the model's own earlier replies from the history. One fix wave fixed all of them test-first, then re-ran both eval sets and regenerated every example.

**Time spent:** about 3 h 20 min of wall-clock on 2026-10-31, from 11:46 to about 15:05 UTC. The design interview and spec took
about 1 h (11:46 to the first commit, `4979c4b`, at 12:40). The plan, build, final review and fixes took about 2 h 25 min
(12:40 to the last commit). The times come from `git log`.
