# Eval results

Final run on the production backend: **24 of 26 cases pass** (cases 11/12, edge 13/14).
Both failures are tool-use misses. Each reply was correct.

- Date: 2026-10-31
- Model: `claude-sonnet-5`
- Backend: Anthropic SDK (`LLM_DEV=false`, label `sdk:claude-sonnet-5`)
- Command: `python -m evals.run_evals` (one pass over both files, no reruns)
- Clock: each case runs at its `now` field, or at `2026-11-02T10:00` (Monday, store open) by default

| File | Passed |
|---|---|
| `evals/cases.jsonl` | 11/12 |
| `evals/edge_cases.jsonl` | 13/14 |
| Total | 24/26 |

The same two files passed 12/12 and 14/14 on the dev backend (`cli:sonnet`) during the build.

## Failures

- **c10-frete-local** — the model did not call `quote`. It read the shipping policy and worked out
  "R$ 599,90 is over R$ 500, so shipping is free" itself. The answer is correct, but the prompt says
  shipping figures come from `quote`. The reply guard let it through, because R$ 500 is in the policy text.
- **e13-fora-do-horario** — the model did not call `register_ticket`. On a Sunday at 20:00 it asked the
  customer what the subject was before it opened the ticket. It gave the correct reopening time
  (Monday 09:00). The case expects the ticket in the first reply.

## Per case

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
| cases | c10-frete-local | **fail** | did not call `quote` (`não chamou quote`) |
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

## What the lockout cases prove

`e08` and `e14` send three wrong e-mails for order 5. The model called `get_order` three times,
then opened a `human` ticket. In `e14` the fourth message has the correct e-mail, and the model
did not try a fourth lookup. So no live call reached the locked branch. The lock itself (a fourth
`get_order` returns an error, even with the correct contact) is proven by the unit tests in
`tests/test_tools.py`.
