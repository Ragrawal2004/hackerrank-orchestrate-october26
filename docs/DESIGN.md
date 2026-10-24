# Ghost Data Still Exists — Design

Status: draft for review (2026-10-24). No solution code yet.
Builds on `SPEC.md` (contract and decisions A1–A13, U3, L1), `SAMPLE_DERIVATION.md` (sample families, citation rule A6) and `FAMILY_DISCOVERY.md` (anchor files, the families, and how many cases each has).

## 0. Core idea: the anchor file
502/518 evaluation cases have **exactly one** evidence file
with a manifest time ≤ `audit_at` that contains a subject identifier.
That file is the **anchor**:
- it is captured at, or shortly before, `audit_at`;
- it fixes the case's family;
- in the samples it is the file the gold cites (29/32; the other 3 rely on absence by omission).

The pipeline does **not** trust only the anchor. Every file ≤ `audit_at` is considered, plus early records inside later files (U3). But the anchor is the default source of state and of citations, and it is the first thing diagnostics report for a case.

```
dataset/ ──► loaders + adapters ──► MASTER FACT STORE (built once, no case knowledge)
                                          │
case row ──► intent ──► identity ──► DOSSIER(case, audit_at)  ← two-clock cut, linking, account linker (L1)
                                          │
                                   POLICY ENGINE  → required treatment vs observed state per resource
                                          │
                                   STEP GENERATOR → steps, citations (A6), ordering, serialization
                                          │
                     output.csv  +  runs/diagnostics.jsonl  +  runs/usage.jsonl
```

- **Language:** Python 3 (stdlib `sqlite3`, `json`, `csv`, `hashlib`).
- **External tools:** `pdftotext` for PDFs, and an LLM (Anthropic SDK, reached only through `llm.call`) for request intent, screenshot/PDF extraction and optional category disambiguation (A13).
- **Without an API key** the run still completes: the deterministic intent fallback is used, multimodal extraction is skipped, and both are logged. Determinism comes from the response cache (`runs/cache/`) and the prefilter override (§2), not from sampling parameters: the API call sends none.

## 1. Master fact store
The store is built once per run from `dataset/` and holds no per-case state. It is an in-memory SQLite database, so it can also be dumped to `runs/facts.sqlite` for debugging. Adapters write **only** to it.

| Table | Columns | Notes |
|---|---|---|
| `identity` | `user_id, kind, value, valid_from, valid_to, source` | `kind` ∈ email, email_hash, phone, name, user_id. Emails are lowercased and trimmed. The rows come from `user_identities` (current), the old and new values in `user_audit_log`, and the computed `sha256(email)` of every email. |
| `evidence_file` | `file_id, path, origin, format, adapter, manifest_at` | From `evidence_files.csv`. `format` is decided by structure (§3), not by `file_id`. |
| `observation` | `obs_id, file_id, locator, record_at, system_id, resource_id, category, state, state_basis, attrs_json` | One row per resource fact. `locator` = line number / table+rowid / JSON path, kept for diagnostics. `record_at` = the record's **own** clock (line ts, inner snapshot `captured_at`, `observed_at`, `recorded_at`); if a record has none, the manifest time is used. `state` comes from the canonical vocabulary below. `state_basis` ∈ explicit, omission. |
| `subject_key` | `obs_id, kind, value` | Identifiers that a record carries (email, hash, phone, name, user_id/subject_reference, lookup key). Linking is an exact equality join against `identity.value`. |
| `join_key` | `file_id, key_kind, key_value, obs_id, record_at` | Trace ids (`trace_id`, `requestId`, `[tr-…]`) and foreign references (`source_resource_id`, `customer_reference`), used for joins inside a file. |
| `capture_scope` | `file_id, system_id, record_at, complete` | Which systems a capture fully covers: `snapshot_scope` rows, bundle `data_store`s, export sections, full replica tables. Used for absence by omission (A11) and Rule O. |
| `activity` | `activity_id, event_time, event_type, source_system, target_system, resource_id, reported_result, details, kind, target_resource, downstream_resource, linked_identifier, linked_user_id` | `system_activity.csv` with the details parsed. `source_system` and `target_system` replace a single `system_id` (the CSV has both, and which one counts is decided later); `reported_result` is the CSV column of the same name, which is a claim; `linked_user_id` is the `user_id` named by a `LINK_RESOURCE_IDENTITY` row, next to its email in `linked_identifier`. Parsed details: "primary profile" / "Target resource", "repopulated downstream resource X", `LINK_RESOURCE_IDENTITY` targets, `CLASSIFY_REQUEST`. Activities are **claims** and never set state by themselves, with one exception: recreation events reopen a resource (§2.4). |
| `rule` | `category, condition, action` | `deletion_rules.csv`. |
| `extraction` | `file_id, method, model, payload_json` | Cached multimodal extraction results (§3.8), so reruns are deterministic. |

**Canonical states**
- Records: `PRESENT`, `ABSENT`, `IDENTIFIABLE` (PII still exposed), `REDACTED`, `ANONYMIZED`.
- Flows: `ACTIVE`, `DISABLED`.
- Backups: `RESTORABLE`, `BLOCKED`.
- Each adapter maps its own vocabulary into these, e.g. `rows=0 http_status=404`→ABSENT, `enabled=true`→ACTIVE, `restore_allowed=true`→RESTORABLE, ledger `NOT_FOUND`→ABSENT, `FOUND`→PRESENT, `IDENTIFIABLE`→IDENTIFIABLE, an analytics record with all identifiers null→ANONYMIZED.

## 2. Per-case dossier (two-clock cut at `audit_at`)
`build_dossier(case)` is a pure function of the case row and the store. It returns a JSON-serializable object, which is written to diagnostics.

1. **Intent** (SPEC §2). If the request is a non-deletion → `NOT_APPLICABLE`, `[]`, stop.
   - **Haiku classifies all 518 requests** (`claude-haiku-5-5`, prompt `code/prompts/intent.md`). The output must validate against the schema `{"intent": "DELETION"|"NON_DELETION", "reason": str}`, and responses are cached. Determinism comes from the response cache and the prefilter override (below), not from sampling parameters.
   - **The deterministic prefilter runs on every case as the safety net.** It looks for negation and keep clauses ("do not close/delete", "not asking for deletion", "only … marketing", in every supported language) and uses multilingual deletion-vocabulary regexes. Its label is used alone when there is no API key, the call errors, or the response fails validation.
   - **Precedence when both labels exist:**
     1. If the prefilter found a **negation/keep clause**, its NON_DELETION label **overrides** the LLM.
     2. Otherwise **the LLM label wins**.
   - **Every disagreement is logged** as `intent_disagreement`, with both labels, which side won (`override=negation` or `llm`), and the LLM's reason. The evaluation report states the **agreement rate**, the number of negation overrides, and the case_ids that disagree.
2. **Identity.** Resolve the email (current, then historical). Collect every identifier for that `user_id` (the full audit chain, every hash). Guard: drop identity rows dated after `audit_at` (none exist today).
3. **Clock 1, usable facts.** Keep observations with `record_at ≤ audit_at`, from **any** file including later-manifest companions (U3), and activities with `event_time ≤ audit_at`. Records after `audit_at` are never read for this case (trap families F-B4/F-D4). The dossier asserts this.
4. **Linking.** Grow the set of the subject's resources until nothing new is added:
   - **direct**: an observation whose `subject_key` equals one of the subject's identifiers;
   - **trace join**: same `file_id` and `join_key` (started line → completed line);
   - **reference join**: `source_resource_id` / `customer_reference` of an already-linked resource;
   - **activity link**: `LINK_RESOURCE_IDENTITY` naming a subject identifier; "repopulated downstream resource X" from a linked resource; a sync from a linked resource;
   - **account linker (L1)**: only when no primary account was linked by the above. It respects `LINK_FALLBACK`, and every pick is `confidence=low`.
   - **analytics linker (MA only)**: only when no analytics record was linked by evidence. It removes `ANONYMIZE_RECORD` candidates in the learned window (−114 … +29 min around `reported_at`) that are evidence-linked to another case. It picks one **only if exactly one remains**, and the pick is `confidence=low`. **On ties (or zero candidates) no analytics step is emitted**, under both `LINK_FALLBACK=guess` and `omit`. There is no rank alignment and no nearest-offset guess for analytics.
   Conflicts (A8): a direct identifier match beats a catalog or activity claim, and the conflict is logged.
5. **State.** For each `(system_id, resource_id)` the latest observation by `record_at` wins. Ties are broken by `explicit` over `omission`, then by the later manifest. Absence by omission is derived from `capture_scope` (complete, in scope, `record_at ≤ audit_at`) and is only used when there is no explicit observation, or when the omission is later than the explicit one.
   **Recreation:** a recreation activity (`SYNC … final checkpoint` to a replica, `repopulated`, `RESTORE_BACKUP`) after the latest ABSENT observation reopens the resource as PRESENT. The evidence is the activity_id, and it is flagged. Plain SUCCESS/FAILED results never change state.
6. **Clock 2, citable files.** For each resource, the list of files with `manifest_at ≤ audit_at` that name it (Rule E), and the in-scope complete captures that omit it (Rule O), ordered by manifest time. The anchor and the family tag are recorded too.

## 3. Adapters (built in the volume order from FAMILY_DISCOVERY)
An adapter is chosen by the file's **structure** (SQL header topology / table set, JSON top-level keys, log line syntax), never by `file_id` or by filename alone; ledger filenames are misleading. Every adapter emits `observation`, `subject_key`, `join_key` and `capture_scope` rows, and has a fixture test on one real file.

Order = number of evaluation cases the adapter unlocks (anchor cases + early-companion cases), largest first:

| # | Adapter | Files | Cases unlocked | Samples to validate on |
|---|---|---|---|---|
| 1 | **Replica-drift SQL**: `customer_data_*_replica*`, `customer_accounts_failover_replica` | 11 + e54 | 105 anchor | 05, 16, 22 (+01, 02, 04 by omission) |
| 2 | **Snapshot bundle JSON** (`snapshots[]`) | 12 | 10 anchor + 90 early companion (+30 traps that must be parsed so they can be excluded) | 21, 23–30, 31, 32 |
| 3 | **Service read-back export JSON** | 11 | 100 anchor | 21, 23–30 |
| 4 | **Inventory SQL** (typed tables + `snapshot_scope`) | 11 | 99 anchor | 09 |
| 5 | **Observability logs**: shared tokenizer for logfmt, bracketed, pipe, RFC5424, JSON-per-line, AWS escaped-kv | ~10 | 40 anchor + 59 early companion | 12, 15, 17, 19 (+09 via e06) |
| 6 | **Read-back trace logs** (`resource_lookup_trace_*`, uses the tokenizer from 5) | 11 | 96 anchor | 03, 18, 20 |
| 7 | **Ledger rows**: JSON lists, log `record_lookup` lines, handover text; one row schema in three containers | 6 | 47 anchor | 11, 13, 14 |
| 8 | **Multimodal**: PDF via `pdftotext -layout`; screenshots via a vision LLM. Every extracted id is checked against ids known from activity, identities and other evidence; ids that don't check out are dropped | 4 + 11 | 12 anchor | 06, 07, 08 (handcrafted) |

Adapter notes:
- **1 Replica SQL.** Load into an in-memory SQLite DB. Rows in a replica table that are missing from the primary are ghosts: the logical `system_id` comes from `origin_system`/`collection_name`, and the `resource_id` is unchanged (A10). Parse `record_json` per collection. A complete table produces a `capture_scope` row, both for the primary and for each replica.
- **2 Bundle.** One observation per record, with `record_at` = the inner snapshot's `captured_at`. **Deviation (SPEC B1, amended in T13):** scope rows are written with `complete=0`, because snapshots are targeted (including empty `records: []` ones), so a bundle omission never proves absence. `operational_record_history` rows are treated as state events (support_storage / backup_restore / privacy_treatment).
- **3 Service export.** Map each section: `profile_lookups` 404 → ABSENT; `analytics.profiles` identifier fields → IDENTIFIABLE/ANONYMIZED; `restore_entries.restore_allowed`; `connectors.enabled`; `index_queries.hits`; `audit_records.retention_until`; `support.tickets`. `record_at` = `observed_at`. **Deviation (SPEC S1):** the export is a targeted read-back, not a full dump, so no `capture_scope` row is written and omission from it proves nothing; only an explicit 404 lookup or an explicit 0-hit query counts as absence (the earlier "0 hits = in-scope omission" idea is dropped, and the data has no 0-hit row today). A query is a subject key by shape (email/phone/hash/user id keep their kind, plain text is a `name` key that never links alone).
- **4 Inventory SQL.** Typed tables map directly, `record_at` = the row's own `updated_at`. **Deviation (SPEC I1):** `snapshot_scope` is written as `capture_scope` rows with `complete=0`, because an inventory is a filtered extract and never proves absence (case_05's gold contradicts `complete=1`); a system missing from `snapshot_scope` gets no row. Rule O prefers a table dump to an inventory (U4). The shared SQL loader also accepts `CREATE INDEX`. `support_records` is an open category assumption (SPEC I2).
- **5/6 Logs.** The tokenizer (`adapters/logtok.py`) turns each line into `(ts, level, service, trace_id, event, kv)` with one parser per syntax, and un-escapes the AWS message before splitting it. Started/completed lines are paired by trace id (a `trace_id` join key on both). **Adapter 5 (`adapters/observability_log.py`, SPEC O1):**
  - detection is by structure: a dominant syntax, `instance` and `region` fields, and a whitelisted state event;
  - `record_at` is the line time, and no capture scope is written;
  - failed operations (`_failed` events, 5xx, error/timeout) set no state;
  - an unlisted event carrying a resource or subject key is unparsed; other unlisted events are noise, counted per event in the parse report.
- **6 Trace logs (`adapters/trace_log.py`, SPEC TR1).** Detected by structure (logfmt, no `instance`/`region`/`pod`, a started/completed pair with `resource_id` and `lookup_key`). `record_at` is the line time and no capture scope is written (a targeted `point_in_time` list). A started line is a resource-less link observation and a completed line carries the state, both joined by a trace-id join key, so the dossier's trace join and A8 check do the linking. State rules shared with adapter 5 live in `adapters/logstate.py` (failed operations set no state). Unlisted events with a resource or key are unparsed; mismatching export counts, reused trace ids and start/complete resource mismatches are unparsed too.
- **7 Ledgers.** `record_type` is the policy category and `result` the state.
- **8 Multimodal.** The prompt asks for a fixed JSON schema (resource ids, system, identifiers, state fields), and the result is cached by the image's sha256. With no API key, cached results are used if present; otherwise the file is skipped and the skip is logged.

## 4. Policy engine
For each linked resource:
1. **Category (A7)** comes from content: collection / `record_type` / table name and fields. The ID prefix is only a last resort, and using it is logged. Content decides between pairs such as SUPPORT_MESSAGE (shared conversation) and SUPPORT_ATTACHMENT (subject-only file), or ANALYTICS_PROFILE and a contact lookup (SECONDARY_IDENTIFIER, sample 12).
2. **Required treatment** comes from `rule`:

   | Rule action | Required end state |
   |---|---|
   | DELETE | ABSENT |
   | REDACT | REDACTED; tombstones keep `subject_reference` and have name/email/phone/hash nulled |
   | ANONYMIZE | ANONYMIZED |
   | RETAIN | PRESENT; for backups also BLOCKED |
   | `data_flow_*` targeting the subject | DISABLED |

   Conditions are evaluated at `audit_at`: `retention_until > audit_at`, legal hold active, the email delivery log contains an email, the attachment is subject-only.
3. **Compliance** = the observed state (dossier §2.5) equals the required state.
   - **Violations:** DELETE-category record present; PII still exposed (IDENTIFIABLE); flow ACTIVE; backup RESTORABLE.
   - **Retained record missing (A9):** no allowed operation exists, so no step is emitted and the status is unaffected. This is still open. The engine **counts** these as `retained_missing`: for each case, the case_id and the missing resource_ids with their category (SECURITY_AUDIT_LOG / LEGAL_HOLD_RECORD / BACKUP_ARCHIVE). The total number of cases and records is printed at the end of every run and copied into the evaluation report, so the size of the open question is measured rather than guessed.
4. **Status.** `INCOMPLETE` if any violation is supported by evidence; otherwise `COMPLETE`. Low-confidence links (L1, MA) never create violations.

### 4.1 Optional LLM category disambiguation (A7)
- **When:** only for resources where the content classifier is ambiguous, meaning it returns more than one candidate category or has to fall back to the ID prefix. Examples: SUPPORT_MESSAGE vs SUPPORT_ATTACHMENT; ANALYTICS_PROFILE vs SECONDARY_IDENTIFIER (sample 12's contact lookup); PRODUCT_TELEMETRY vs ANALYTICS_PROFILE.
- **Input:** the record's field names and values with direct identifiers masked (`<EMAIL>`, `<PHONE>`, `<HASH>`, `<NAME>`), plus the candidate categories with their rule conditions from `deletion_rules.csv`. Prompt: `code/prompts/category.md`.
- **Output:** JSON `{"category": <one of the candidates>, "reason": str}`. It is schema-validated, and a category outside the candidate set counts as a failed validation.
- **Cache:** keyed by `sha256(model + prompt + masked record)`.
- **Flag:** `CATEGORY_LLM=on|off` (env; CLI `--category-llm`), **default `off`**.
- **Deterministic fallback**, used when the flag is off, there is no key, the call errors or validation fails: the fixed rule order of §4 step 1 (collection/record_type → field signature → ID prefix).
- **Diagnostics:** every use is logged as `category_llm` (deterministic pick, LLM pick, used pick). The usage report counts these calls under `purpose=category`.
- This extends SPEC A13 (amended accordingly).

## 5. Step generator and ordering
- **Templates per resource:**

  | Violation | FINDING | REMEDIATION | VERIFICATION |
  |---|---|---|---|
  | Record remains | `REPORT_REMAINING_RECORD` | `DELETE_RECORD` | `VERIFY_RECORD_ABSENT` |
  | PII exposed | `REPORT_EXPOSED_PII` | `REDACT_RECORD` / `ANONYMIZE_RECORD` | `_REDACTED` / `_ANONYMIZED` |
  | Active flow | `REPORT_ACTIVE_DATA_FLOW` | `DISABLE_DATA_FLOW` | `VERIFY_DATA_FLOW_DISABLED` |
  | Restore risk | `REPORT_RESTORE_RISK` | `BLOCK_RESTORE` | `VERIFY_RESTORE_BLOCKED` |

  The state is fixed by the operation (SPEC §1.2).
- **INCOMPLETE:** findings for the violations; remediation for each finding; verification for the **full end state of every linked resource**, including ones that are already compliant, the account (L1: always) and retained audit records (A3/A4). Remediation and verification steps have `evidence_id=""`.
- **COMPLETE:** only verification steps, one per linked resource in its required end state, each cited per A6. If a resource has no citable file, it is dropped and logged as `unciteable`. The one exception is the low-confidence (L1) account step:
  - it cites the latest in-scope **Rule O** capture of `account_service` (manifest ≤ `audit_at`) if one exists;
  - otherwise it is still emitted with `evidence_id=""` and logged as `guessed_account_uncited`.
  A guessed account never takes a Rule E citation.
- **Citation (A6):** Rule E (the latest file ≤ `audit_at` that names the resource), then Rule O (the latest in-scope omission). Ambiguity from multiple Rule O candidates is logged. A recreation reopen cites its activity_id (the only exception to A2).
- **Ordering:** phase → canonical operation order → `system_id` → `resource_id` (plain string order). Then `step_no` = 1…n. This matches all 32 samples, and the canonical operation order already puts disable/block before record changes.
- **Serialization:** `json.dumps(steps, ensure_ascii=False, separators=(",", ":"))` with fixed key order. CSV is written with `csv.writer` (minimal quoting), rows sorted by numeric `case_id` (A1), at the repo-root `output.csv`.

## 6. Usage counter
- Every LLM call goes through `llm.call(purpose, model, prompt, case_id, schema)`. It returns the reply parsed and validated against `schema`, and raises `LLMUnavailable` on any error, timeout or invalid reply so the caller uses its deterministic path (retries are bounded; the SDK is imported lazily inside `llm.py`). Nothing else imports the SDK. A grep check in the invariant suite enforces this.
- Each call appends a row to `runs/usage.jsonl`:
  `ts, purpose (intent|category|screenshot|pdf), model, case_id, input_tokens, output_tokens, cache_hit (bool), error, run_id`. One line is written per API attempt (a failed attempt logs the exception class name in `error`; a reply that fails validation keeps the tokens it cost, with `error=BadResponse`) and one per cache hit.
  The token counts come from the API response `usage` field, never from estimates.
- Responses are cached by `sha256(model, prompt, params)` in `runs/cache/`, where `params` = `{max_tokens, schema}` (no sampling parameters are sent; image input extends the key when it is added in T17). A cache hit makes no API call and logs zero tokens. `--cold` never reads the cache but still writes it. **The final run is cold** (`--cold`), so `usage.jsonl` reflects real calls.
- `code/evaluation/usage_report.py` aggregates per model and overall: calls, input and output tokens, total and average tokens per request (÷ 518), and cost from a per-model price table in `code/ghost/config.py`. A cache hit counts as zero calls and zero cost, and total calls are shown separately from total cache hits. The price table carries a source URL and date per model, no numbers are assumed, and the report fails loudly while any price is missing. The script renders `code/evaluation/usage_report.md`.
- Model plan (expected calls in a cold full run):
  - intent: `claude-haiku-5-5`, **one call for each of the 518 requests**;
  - screenshots: `claude-sonnet-5-5`, about 11 calls;
  - category disambiguation (§4.1): `claude-haiku-5-5`, 0 calls by default, and only for the ambiguous records when `CATEGORY_LLM=on`.
  All models are configurable through env vars. `purpose` ∈ intent | category | screenshot | pdf.

## 7. Diagnostics
`runs/diagnostics.jsonl` has one line per case:
- anchor file, family tag, identifiers used;
- linked resources with method and `confidence`;
- every L1 pick (method, candidates, competitors);
- events: A8 conflicts, Rule O ambiguity, unciteable steps, `retained_missing` (A9), `intent_disagreement`, `category_llm`, MA ties (`analytics_unresolved`), skipped multimodal files, recreation reopens;
- LLM calls (`purpose`, `cache_hit`, `error`; token counts stay in `runs/usage.jsonl`).

`runs/` is gitignored and not shipped. A summary table (counts per flag) is copied into `evaluation_report.md`.

## 8. Validation plan
1. **Sample scoring** (`python3 code/evaluation/main.py`): run the pipeline on the 32 samples and compare with `sample_output.csv`.
   - **case** status accuracy;
   - **step** precision, recall and F1, matching on `(phase, operation, system_id, resource_id, state, evidence_id)` and ignoring `step_no`;
   - **field** accuracy over aligned steps, aligned by `(phase, operation, resource_id)`;
   - **sequence**: exact match of the whole array.
   Every metric is reported twice, side by side:
   - **strict**: all fields, including `evidence_id`;
   - **relaxed**: `evidence_id` is ignored, both for matching steps and for field accuracy (sequence compares the arrays with `evidence_id` blanked).
   The gap between the two isolates citation errors (A6, U3) from content errors (linking, state, policy).
   The report includes the known ceiling (U1 misses under `omit`, the 2 U2 ids that appear nowhere) and runs with both `LINK_FALLBACK=guess` and `omit`.
2. **Invariants over all 518** (the same script with `--invariants`); any failure fails the run:
   - output shape:
     - exactly 518 rows, unique, numeric order;
     - valid JSON, `step_no` 1…n, phase order and canonical order respected;
     - operation→state and operation→phase from the allowed table;
   - steps and status:
     - `NOT_APPLICABLE` ⇔ `[]`;
     - COMPLETE has only verification steps, all cited. The single exception is a low-confidence account step logged as `guessed_account_uncited`, which may have `""`; the number of such steps is reported;
     - INCOMPLETE has ≥1 finding, every finding is cited and has a matching remediation and verification, and all remediation and verification steps have `""`;
     - every deletion case has an account step (`guess`), or a logged `link_unresolved`;
     - no retained category is ever DELETEd, and no resource gets two conflicting end states;
   - evidence and time:
     - every cited `file_id` exists with manifest ≤ `audit_at`;
     - every cited activity_id has `event_time ≤ audit_at`;
     - the dossier assertion holds: no fact with `record_at > audit_at` was used;
     - in trap families F-B4/F-D4, no late record contributed;
   - determinism: two runs give a byte-identical `output.csv`.
   Also reported, but not failing:
   - per-family status mix, steps per case, how often each flag appears;
   - **`retained_missing`**: the number of cases and records (A9), with case_ids;
   - **intent agreement**: LLM vs prefilter rate, with the case_ids that disagree;
   - analytics MA ties left unresolved, and `category_llm` overrides (when on).
3. **Custom cases** (≥3; written to `code/evaluation/custom_cases.md` with inputs and expected outputs, and run as overlays: a temp copy of `dataset/` with added rows and files, via `--dataset <dir>`):
   - **CC1 post-audit trap:** a bundle record stamped after `audit_at` shows the account present → the case stays COMPLETE and the late file is not cited.
   - **CC2 historical email in a regional replica:** a ghost row in `eu_west` keyed by an old email → INCOMPLETE with `REPORT_REMAINING_RECORD` / `DELETE_RECORD` / `VERIFY_RECORD_ABSENT`, `system_id` logical.
   - **CC3 deletion words in a non-deletion request:** "Please don't delete my account, just remove me from marketing lists" (plus a Spanish variant) → `NOT_APPLICABLE`, `[]`.
   - **CC4 tombstone leak:** a tombstone keeps `subject_reference` but its phone is still set → `REPORT_EXPOSED_PII` / `REDACT_RECORD` / `VERIFY_RECORD_REDACTED`, never DELETE.
   - **CC5 legal hold plus an active connector:** the legal-hold record must get `VERIFY_RECORD_PRESENT`; the connector gets disable-before-delete ordering.
4. **Uncovered-family spot checks:** for each family with no sample (F-A regional/reporting/data-failover, F-B1, F-B3, F-B4, F-C2, F-C3, F-D2, F-D3, F-D4, F-E syntaxes, F-F, F-G, F-J, F-K), derive one case by hand in the style of SAMPLE_DERIVATION and record the expected steps in `code/evaluation/family_checks.md`. The evaluation script diffs them. These are our own derivations, not gold.

## 9. Code layout
```
code/main.py                  CLI: --dataset, --out, --cases, --link-fallback, --category-llm, --no-llm, --cold
code/ghost/config.py          paths, env flags (LINK_FALLBACK, CATEGORY_LLM, models, prices)
code/ghost/store.py           fact store schema + helpers
code/ghost/loaders.py         CSV loaders, activity detail parser
code/ghost/identity.py        email → user_id, identifier set
code/ghost/intent.py          deterministic prefilter + LLM classifier
code/ghost/llm.py             the only SDK user: cache + usage counter
code/ghost/adapters/          replica_sql, bundle, service_export, inventory_sql, logtok, observability, trace, ledger, multimodal
code/ghost/dossier.py         two-clock cut, linking, state, citations
code/ghost/linker.py          L1 accounts (M6 → M9 → M7 → widen); MA analytics (unique only)
code/ghost/policy.py          category, rule, compliance, status
code/ghost/steps.py           templates, citations, ordering, serialization
code/ghost/diagnostics.py
code/prompts/intent.md, screenshot.md, category.md
code/evaluation/main.py, usage_report.py, custom_cases.md, family_checks.md, evaluation_report.md, usage_report.md
```

## 10. Risks
| Risk | Impact | Mitigation |
|---|---|---|
| L1 guesses are wrong (≈16 % on held-out; real U1 accuracy is unknown and likely worse) | Each wrong account costs one missing and one extra step | Flagged; `omit` mode for comparison; guesses never change status |
| Guessed account step in a COMPLETE case with no Rule O capture gets `evidence_id=""` | Departs from the contract ("COMPLETE verification must cite evidence"); probably scored as a citation miss | Logged as `guessed_account_uncited` and counted per run; switch to `LINK_FALLBACK=omit` if the count is large |
| 71 % of cases are in families with no sample | Rules learned on samples may not carry over | Family spot checks (§8.4), invariants, structure-based adapters |
| A U3 clock is misapplied in trap families (30 cases) | Wrong status and late citations | Dossier assertion + invariant + CC1 |
| Log syntax edge cases (AWS escaping, multi-line JSON) | Missed links | Tokenizer round-trip tests on every log file; count unparsed lines per file |
| Vision misreads ids in screenshots | Wrong or missing multimodal steps | Validate ids against known ids; cached, reviewed extractions |
| Category ambiguity (A7) | Wrong operation (DELETE vs ANONYMIZE/REDACT) | Content-based classifier; ID-prefix fallback is logged; optional §4.1 LLM disambiguation |
| LLM category picks are wrong or unstable (§4.1) | Wrong operation on ambiguous records | Off by default; constrained to the candidate set; cached; diff reported between `CATEGORY_LLM=on` and `off` on samples and spot checks before enabling |
| Intent LLM and prefilter disagree, or the LLM is wrong on a template the prefilter gets right | Wrong NOT_APPLICABLE | A prefilter negation/keep clause overrides the LLM; otherwise the LLM wins; every disagreement logged and reviewed; agreement rate and override count reported; prefilter is the fallback |
| Analytics MA omits on ties | Lost `VERIFY_RECORD_ANONYMIZED` steps (5/7 sample U1) | Accepted by decision; counted as `analytics_unresolved` |
| A9 (retained record missing) and the A6 case_04 Rule O ambiguity | Rare wrong citation or missed finding | `retained_missing` counted and reported per run; Rule O ambiguity logged; revisit if counts are non-trivial |
| Proof exists only in late-manifest files (unciteable in COMPLETE cases) | Dropped verification step | Logged as `unciteable` |
| Multilingual intent; negation traps | Wrong NOT_APPLICABLE | Prefilter for negation + LLM + CC3; all 10 known non-deletions checked |
| Recreation/reopen semantics guessed from samples | False INCOMPLETE | Only explicit recreation events reopen; flagged |
| Grader `case_id` sort (A1) | Possible row-order mismatch | Numeric, as in the input files; noted in the README |
| LLM nondeterminism or no API key | Run-to-run drift | Response cache (determinism comes from the cache and the prefilter override), deterministic fallback |
| Over-fitting to the 32 samples | Inflated self-score | Report the samples separately from the invariants and family checks |

## 11. Task list (one commit per task)
| # | Task | Done when |
|---|---|---|
| T1 | Scaffold: package layout, CLI flags, config (`LINK_FALLBACK`, models), `runs/` gitignored, README run section | `python3 code/main.py` writes a 518-row stub `output.csv` |
| T2 | Fact store schema, CSV loaders, activity detail parser, identity resolution | All 518 + 32 emails resolve to exactly one user; identifier chains for user_10/user_35 are correct |
| T3 | `llm.py`: cache + usage counter + `usage_report.py` skeleton | A mocked call produces a usage row and the report renders |
| T4 | Intent: Haiku on all 518 + deterministic prefilter safety net + negation override + disagreement logging (prompt in `code/prompts/intent.md`) | Cases 533–542 → NON_DELETION under both classifiers; samples all deletion; agreement rate and override count printed |
| T5 | Steps, ordering and serialization + invariant checker | Invariants pass on the stub output; the sample gold passes the invariants |
| T6 | Evaluation harness (case/step/field/sequence scoring, strict and relaxed) | Both score sets computed for the stub vs sample gold |
| T7 | Dossier: two-clock cut, linking closure, state resolution, recreation, citation candidates | Unit tests on hand-built facts; the late-record assertion is in place |
| T8 | Policy engine + status + step generation wired end to end | Runs on an empty fact store without errors |
| T9 | Adapter 1: replica-drift SQL | Samples 05, 16, 22 match gold (01, 02, 04 by omission) |
| T10 | Adapter 2: snapshot bundle | Bundle steps for samples 23–32 explained; traps excluded |
| T11 | Adapter 3: service read-back export | Samples 21, 23–30 match except L1 steps |
| T12 | Adapter 4: inventory SQL + scope | Sample 09 matches |
| T13 | Adapter 5: log tokenizer + observability (6 syntaxes) | Samples 12, 15, 17, 19 match; 0 unparsed meaningful lines across all logs |
| T14 | Adapter 6: read-back trace logs | Samples 03, 18, 20 match except U1/U2 steps |
| T15 | Adapter 7: ledgers (JSON / log / text) | Samples 11, 13, 14 match except L1 steps (done, see SPEC LG1) |
| T16 | L1 account linker + MA analytics linker (unique only) + `LINK_FALLBACK` + diagnostics file | Reproduces the LINKING_EXPERIMENT numbers (M6 151/152; MA 44/45 held-out); MA ties emit no step |
| T17 | Adapter 8: PDF + screenshot extraction (prompt in `code/prompts/screenshot.md`) | Samples 06–08 match; F-J cases produce steps |
| T18 | Custom cases CC1–CC5 + overlay runner | All 5 pass |
| T19 | Uncovered-family spot checks + fixes; optional §4.1 category LLM (`CATEGORY_LLM`, prompt `code/prompts/category.md`), evaluated on/off | `family_checks.md` diffs are clean or explained; on/off diff recorded |
| T20 | Full cold run; `evaluation_report.md`, `usage_report.md`, final README; `code.zip` dry-run listing | 518 rows, invariants green, reports written, zip has no dataset/venv/nested archives |

Every task also appends its turn to `log.txt`. Commits are made only when you approve them.
