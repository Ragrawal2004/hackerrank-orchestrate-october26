# Usage report

Requests: 518 | run_id: run-20261025T080414Z

| Model | Calls | Cache hits | Errors | Input tokens | Output tokens | Cost (USD) |
|---|---|---|---|---|---|---|
| claude-haiku-5-5 | 381 | 137 | 1 | 181661 | 19972 | 0.0282 |
| **Total** | 381 | 137 | 1 | 181661 | 19972 | 0.0282 |

Average tokens per request: 389.3
Estimated cost per request (USD): 0.000054

## Prices used

- claude-haiku-5-5: input 0.1 / output 0.5 USD per million tokens (source: https://platform.claude.com/docs/en/about-claude/pricing, as of 2026-10-24)

## Development-time LLM usage (not part of the final run)

Everything above is the final run that produced `output.csv`. The calls below were made while building and testing the solution and are listed separately. They come from the usage logs written during development, read after the fact; costs use the same `config.PRICES` as above. Cache hits are not counted as calls.

| Activity | Model | Purpose | Calls | Cache hits | Errors | Input tokens | Output tokens | Cost (USD) |
|---|---|---|---|---|---|---|---|---|
| earlier full intent run, 2026-10-24 | claude-haiku-5-5 | intent | 379 | 139 | 1 | 180664 | 19940 | 0.0280 |
| LLM gateway smoke test, 2026-10-24 | claude-haiku-5-5 | smoke | 3 | 0 | 1 | 64 | 18 | 0.0000 |
| T17 screenshot/PDF bake-off, Opus side, 2026-10-25 | claude-haiku-5-5 | intent | 1 | 1 | 0 | 477 | 53 | 0.0001 |
| T17 screenshot/PDF bake-off, Opus side, 2026-10-25 | claude-opus-5-5 | screenshot | 30 | 30 | 0 | 89370 | 15676 | 0.6710 |
| T17 screenshot/PDF bake-off, Sonnet side, 2026-10-25 | claude-haiku-5-5 | intent | 1 | 0 | 0 | 477 | 53 | 0.0001 |
| T17 screenshot/PDF bake-off, Sonnet side, 2026-10-25 | claude-sonnet-5-5 | screenshot | 30 | 0 | 0 | 89370 | 6590 | 0.2446 |

| Model | Calls | Cache hits | Errors | Input tokens | Output tokens | Cost (USD) |
|---|---|---|---|---|---|---|
| claude-haiku-5-5 | 384 | 140 | 2 | 181682 | 20064 | 0.0282 |
| claude-opus-5-5 | 30 | 30 | 0 | 89370 | 15676 | 0.6710 |
| claude-sonnet-5-5 | 30 | 0 | 0 | 89370 | 6590 | 0.2446 |
| **Development total** | 444 | 170 | 2 | 360422 | 42330 | 0.9438 |

Sources: `runs/usage.jsonl` (the earlier full intent run; the final run used a fresh runs directory), `runs/smoke/usage.jsonl`, and the two bake-off logs, which were written outside the repository (`/tmp/bakeoff/<model>/usage.jsonl`) and are not committed. The Opus screenshot/PDF reads that produced the committed `code/extractions/` are the `claude-opus-5-5` screenshot line above (30 real calls; the second Opus pass in that log was served from cache). Sonnet 5.5 was run on the same 15 files for comparison only. Image-bearing calls are priced at the token counts the API reported. The smoke log includes one call that logged a `TypeError`.
The final run above (this report's main table) cost $0.0282; all development-time usage together cost $0.9438.
