# Architecture

Facts below come from `docs/DESIGN.md`, `docs/SPEC.md` and the code under `code/`. Python 3, standard library only (the Anthropic SDK is imported lazily inside `llm.py`).

## 1. Pipeline, request to `output.csv`

```
dataset files ──► loaders ──► adapters 1-8 ──► FACT STORE (in-memory SQLite, built once, no case state)
                                                   │
 request row ──► intent (prefilter + Haiku) ──► NOT_APPLICABLE, []   (non-deletion)
        │ deletion
        ▼
   identity (current + historical email, phone, hash, user_id)
        ▼
   dossier(case, audit_at)   two-clock cut, linking, account linker
        ▼
   policy                    category + required state per resource (deletion_rules.csv)
        ▼
   steps                     findings, remediation, verification; citations; ordering
        ▼
   output.csv (repo root)  +  runs/diagnostics.jsonl  +  runs/usage.jsonl
        ▼
   invariants (code/evaluation/main.py --invariants)
```

## 2. Module map

| Path | Role |
|---|---|
| `code/main.py`, `ghost/cli.py` | Entry point; wires intent, store, dossiers, linker, policy, output |
| `ghost/config.py`, `envfile.py` | Defaults, env variables, price table, budgets; `.env` loader (never printed) |
| `ghost/loaders.py`, `store.py`, `schema.py` | Reads the input files; SQLite store; JSON-schema checks for LLM replies |
| `ghost/intent.py` | Word prefilter with negation rules, plus the Haiku label; negation overrides the model |
| `ghost/llm.py` | The only LLM gateway: cache, caps, usage log, retries |
| `ghost/adapters/` | 1 `replica_sql`, 2 `snapshot_bundle`, 3 `service_export`, 4 `inventory_sql`, 5 `observability_log`, 6 `trace_log`, 7 `ledger_json`/`ledger_log`/`ledger_text` (shared `ledger_rows`), 8 `multimodal`; shared `logtok` (tokenizer), `logstate` (state rules), `common` |
| `ghost/multimodal_read.py` | Reads screenshots and PDFs through `llm.call`; two reads per file |
| `ghost/identity.py`, `activity_parse.py` | Email to user resolution and identifier set; parsed `system_activity` details |
| `ghost/dossier.py` | Per-case facts at `audit_at`: linking, state, citation candidates, clock assertion |
| `ghost/linker.py` | Account (L1) and analytics (MA) linking fallback, default `omit` |
| `ghost/policy.py` | Category, required end state, verdict, status, step list |
| `ghost/steps.py`, `invariants.py`, `diagnostics.py` | Step construction, ordering, serialization; output checks; per-case events |
| `code/evaluation/` | `main.py` (scoring, `--invariants`, `--custom`), `scoring.py`, `usage_report.py`, `custom_cases/`, the two reports |
| `code/extractions/` | Agreed screenshot/PDF facts (ids and hashes only), one JSON file per evidence file |
| `code/prompts/` | `intent.md`, `screenshot.md` |
| `code/tests/` | Unit and end-to-end tests, with fixture builders |

## 3. Data flow
1. **Evidence to adapters.** An adapter is chosen by file structure, never by name. Each writes `observation` rows (resource, state, the record's own time `record_at`), `subject_key` rows (identifiers a record carries), `join_key` rows (trace ids, references) and `capture_scope` rows.
2. **Scope.** `complete=1` is written only for replica SQL dumps. Bundles and inventories write `complete=0` (SPEC B1, I1), and logs, exports, ledgers and screenshots write no scope, so only a replica dump can prove absence by omission.
3. **Dossier.** For one case it keeps facts with `record_at <= audit_at`, links resources to the subject (direct identifier, trace join, reference join, activity link), and takes the latest observation per resource as its state. A direct identifier beats a trace join or a catalog claim (A8), and a name never links alone.
4. **Policy and steps.** Policy maps each resource to a category and the required state (delete, redact, anonymize, retain, block restore, disable flow). Findings need a cited file; remediation and verification use an empty `evidence_id` in INCOMPLETE cases.

## 4. Two clocks, Rule E and Rule O
- **Two clocks.** A record's own time (`record_at`) decides whether it may be used for linking and state, even inside a file captured later. The file's manifest capture time decides only citation: no file with a manifest after `audit_at` is cited.
- **Rule E.** Cite the latest file with manifest time at or before `audit_at` that names the resource and carries the same state as the winning observation. A recreation reopen cites its activity id instead.
- **Rule O.** For an absence with no explicit citation, cite the latest complete in-scope capture that omits the resource (a table dump is preferred over an inventory). If several qualify, the ambiguity is logged.

## 5. LLM gateway (`ghost/llm.py`)
- Every call goes through `llm.call(purpose, model, prompt, case_id, schema)`; the reply is parsed and validated, and any failure raises `LLMUnavailable` so the caller takes its deterministic path.
- **Cache:** `runs/cache/`, keyed by `sha256(model, prompt, params)`; `--cold` skips reads but still writes.
- **Caps:** `llm.configure(budgets=...)` sets a call and dollar limit per purpose, refused before a call is made. The CLI sets the screenshot budget (40 calls, $3.00).
- **Usage log:** one line per attempt in `runs/usage.jsonl` with real token counts, the error class, and `bad_response_kind` for a rejected reply (the reply text is never logged).

## 6. Determinism and invariants
- No sampling parameters are sent, rows are sorted by numeric `case_id`, steps by a fixed order, and JSON is serialized compactly. The `--no-llm` run and the LLM run gave the same `output.csv` (sha256 `ea379389...`).
- `python3 code/evaluation/main.py --invariants` checks header, ordering, step numbering, allowed operations and states, `NOT_APPLICABLE` only with `[]`, citations dated at or before `audit_at`, and that every finding has its remediation and verification.

## 7. Where to look
- `docs/SPEC.md`: contract and numbered decisions (U3, B1, I1, MM1, TR1, T19b).
- `docs/ASSUMPTIONS.md`: every assumption with case counts, and the measured-but-not-adopted changes.
- `docs/DESIGN.md`: the design this file summarizes.
- `code/evaluation/evaluation_report.md` and `usage_report.md`: results, failures found, cost.
