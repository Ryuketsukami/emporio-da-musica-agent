# Eval results

## Prompt flags on vs off (2026-10-31, current code)

`claude-sonnet-5`, 3 full runs per setting, 27 cases each. Both settings have the shipping check on
(a shipping question about a product must be answered from `quote`). `e13` gives the subject in a
second message.

| Setting | Run 1 | Run 2 | Run 3 | Total | Misses |
|---|---|---|---|---|---|
| all three flags on (default) | 27/27 | 27/27 | 26/27 | 80/81 | `c11`: explained warranty windows, no complaint ticket |
| all flags off | 27/27 | 26/27 | 27/27 | 80/81 | `e13`: asked more questions, no ticket |

An earlier batch (3 runs per setting) had a first version of the shipping check that fired on any
shipping remark. It sent good price answers back, and the retry dropped the answer (`c12`, `c03`), so
that batch's totals are not comparable. Its targeted cases still count: `e13` failed 3 of 6 runs
without flags and 0 of 6 with them; `e11` failed 1 of 6 without and 0 of 6 with them.

## SDK run after the date rule and the option-listing tool errors (before the flags)

**25 of 27 cases pass** (cases 11/12, edge 14/15), `claude-sonnet-5`, one pass, `python -m evals.run_evals`.

| Case | Result | Note |
|---|---|---|
| `c10-frete-local` | fail | did not call `quote`; it read the shipping policy and said "grátis", which is correct. Passed 3 of 3 reruns (`--only`). |
| `e13-fora-do-horario` | fail | asked for the subject before opening the ticket (as in every run). Gave the reopening time in 2 of 3 reruns. |
| all other 25 | pass | |

The date rule tells the model to use only the `[agora: ...]` line as its clock. Before it, a dev-CLI run of
example 04 (`--now 2025-12-26`) said the return window had closed. After it, 3 of 3 dev runs said the window is
open until 29/12/2025, which is correct.

## Earlier runs

Run before the date rule, production backend: **26 of 27 cases pass** (cases 12/12, edge 14/15).
The one failure is a tool-use miss. Its reply gave the correct facts.

Both runs below are from 2026-10-31, after the final fix wave (CLI history fix, wider reply guard,
agent-loop hardening, truncated-search notice) and the guard-precision follow-up (stricter phone and
domain patterns, +55 numbers, domain paths, any-case domain endings). The guard blocked no correct
reply in either run. Each is one pass over both files, with no reruns.
Each case runs at its `now` field, or at `2026-11-02T10:00` (Monday, store open) by default.

## SDK run (headline)

- Model: `claude-sonnet-5`
- Backend: Anthropic SDK (`LLM_DEV=false`, label `sdk:claude-sonnet-5`)
- Command: `python -m evals.run_evals`

| File | Passed |
|---|---|
| `evals/cases.jsonl` | 12/12 |
| `evals/edge_cases.jsonl` | 14/15 |
| Total | 26/27 |

**Failure.** `e13-fora-do-horario`: the model did not call `register_ticket`. On a Sunday at 20:00 it
gave the correct reopening time (Monday 09:00), said it would register the request, and asked for the
subject first. So the customer got no protocol in that reply. The case expects the ticket in the first
reply.

| File | Case | Result | Failure reason |
|---|---|---|---|
| cases | c01-violoes-ate-1000 | pass | |
| cases | c02-endereco | pass | |
| cases | c03-preco-gd20 | pass | |
| cases | c04-arrependimento | pass | |
| cases | c05-sabado | pass | |
| cases | c06-parcelas | pass | |
| cases | c07-promocoes | pass | |
| cases | c08-status-pedido | pass | |
| cases | c09-fora-de-escopo | pass | |
| cases | c10-frete-local | pass | |
| cases | c11-reclamacao | pass | |
| cases | c12-pix-martin | pass | |
| edge | e01-sem-estoque | pass | |
| edge | e02-fora-de-linha | pass | |
| edge | e03-em-breve | pass | |
| edge | e04-erro-de-digitacao | pass | |
| edge | e05-categoria-vazia | pass | |
| edge | e06-promo-sem-pix | pass | |
| edge | e07-devolucao-fora-do-prazo | pass | |
| edge | e08-tentativas-erradas | pass | |
| edge | e09-injecao | pass | |
| edge | e10-falso-gerente | pass | |
| edge | e11-acessorio | pass | |
| edge | e12-fora-de-escopo | pass | |
| edge | e13-fora-do-horario | **fail** | did not call `register_ticket` (`não chamou register_ticket`) |
| edge | e14-bloqueio-depois-de-3-erros | pass | |
| edge | e15-preco-falso-do-cliente | pass | |

## Dev CLI run

- Model: `sonnet` through the local `claude -p` CLI (`LLM_DEV=true`, label `cli:sonnet`)
- Run after the CLI history fix: earlier dev runs did not show the model its own previous replies.

| File | Passed |
|---|---|
| `evals/cases.jsonl` | 12/12 |
| `evals/edge_cases.jsonl` | 14/15 |
| Total | 26/27 |

**Failure.** `e07-devolucao-fora-do-prazo`: the reply stopped after confirming the order and its
estimated delivery date. It never got to the return options, so "garantia" was missing. No guard hit
was involved. The previous dev run passed this case, so this is run-to-run model variance.

## Earlier SDK run, before the fixes

The first SDK run (same day, before the fix wave, 26 cases) passed 24/26. It failed `c10-frete-local`
(the model worked out free shipping from the policy text instead of calling `quote`) and `e13`.
`c10` passes in the final run.

## What the lockout cases prove

`e08` and `e14` send three wrong e-mails for order 5. The SDK run called `get_order` three times; the
dev run stopped after two misses. Both then offered or opened a human handoff. In `e14` the fourth
message has the correct e-mail, and neither run tried a fourth lookup. So no live call reached the locked branch. The lock itself (a
fourth `get_order` returns an error, even with the correct contact) is proven by the unit tests in
`tests/test_tools.py`.

## What e15 measures

The guard accepts numbers that appear in the customer's own messages (spec §9). `e15` quotes a false
price ("R$ 1.000,00") and checks that Duda looks the product up, gives R$ 2.199,00, and does not
confirm the false price. Both runs passed it. The gap stays open in code; see the README's remaining risks.
