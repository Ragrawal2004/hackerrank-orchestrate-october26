# Ghost Data Still Exists — Working Specification

Status: draft for review (2026-10-24). Sources: `AGENTS.md`, `README.md`, `problem_statement.md`, and a read-only profile of every file under `dataset/`. Organizer-only files are not used.

## 1. Contract

### 1.1 Inputs (all under `dataset/`)
| File | Rows | Notes |
|---|---|---|
| `deletion_requests.csv` | 518 | `case_33`…`case_550`. No overlap with sample IDs. File order is **numeric**, not lexicographic. |
| `sample_deletion_requests.csv` / `sample_output.csv` | 32 / 32 | `case_01`…`case_32`. Gold labels: 22 INCOMPLETE and 10 COMPLETE. **No NOT_APPLICABLE example.** |
| `user_identities.csv` | 850 | `user_id,name,email,phone,email_hash,updated_at`. Emails are unique. `email_hash = sha256(email)`, verified for 850/850 users (all stored emails are already lowercase). Includes users whose accounts were already deleted. |
| `user_audit_log.csv` | 306 | Fields: `email` (107), `email_hash` (107), `phone` (92). Email and email_hash changes come in pairs with the same timestamp. |
| `system_activity.csv` | 4,011 | Global stream covering 17 event types. Rows are **not strictly time-ordered by id** (e.g. `activity_4000…4011` are back-dated). |
| `deletion_rules.csv` | 15 | Category → condition → action (§5). |
| `evidence_files.csv` | 94 | `file_id,file_path,origin,captured_at`. `file_path` is relative to `dataset/`. |
| `evidence/` | 94 files | 28 json, 1 txt, 26 log, 24 sql, 4 pdf, 11 png. |

Request columns: `case_id,email,requested_at,reported_status,reported_at,audit_at,request_text`. Reported status counts in the evaluation set: COMPLETE 254, PARTIALLY_COMPLETE 152, NOT_STARTED 112. Reported status is a **claim**, never proof.

### 1.2 Output (`output.csv` at the repo root)
- Header: `case_id,deletion_status,steps_json`. Exactly 518 rows, sorted by `case_id`. See A1 for whether the sort is numeric or lexicographic.
- `deletion_status` ∈ {`COMPLETE`, `INCOMPLETE`, `NOT_APPLICABLE`}. `NOT_APPLICABLE` ⇒ `[]`.
- Step object: `{step_no, phase, system_id, resource_id, operation, state, evidence_id}`. Steps are numbered from 1 with no gaps. Key order and whitespace don't matter; array order does.
- **Ordering rule** (problem_statement §Rules): sort by phase (FINDING → REMEDIATION → VERIFICATION), then by operation in the canonical order of the operations table, then by `system_id` ascending, then by `resource_id` ascending. Checked against all 32 sample answers: there are 0 deviations.
  Canonical operation order:
  `REPORT_REMAINING_RECORD, REPORT_EXPOSED_PII, REPORT_ACTIVE_DATA_FLOW, REPORT_RESTORE_RISK, DISABLE_DATA_FLOW, BLOCK_RESTORE, DELETE_RECORD, REDACT_RECORD, ANONYMIZE_RECORD, VERIFY_RECORD_ABSENT, VERIFY_RECORD_PRESENT, VERIFY_RECORD_REDACTED, VERIFY_RECORD_ANONYMIZED, VERIFY_DATA_FLOW_DISABLED, VERIFY_RESTORE_BLOCKED`.
  Following this order automatically satisfies "disable flows and block restores before changing records".
- `resource_id` sorting is plain string order. In the samples, `account_4163 < search_…` and `audit_record_…` sort correctly, but numeric suffixes compare as strings (e.g. `search_1308 < search_4218`).
- Operation → state is fixed (REPORT_* findings → PRESENT/ACTIVE, DELETE → ABSENT, etc.). See the AGENTS.md §6.4 table.

### 1.3 Evidence-citation conventions (observed in the samples)
- FINDING steps always cite an `evidence_id`. In all 32 samples, citations are **`file_id`s only**. No `activity_id` or `audit_id` is ever cited, even though both are allowed.
- COMPLETE ⇒ only VERIFICATION steps, each citing the evidence that proves the state at `audit_at`. One case usually cites a single file, the latest qualifying one (e.g. case_21–28 cite `evidence_92`@09-15 and not `evidence_02`@09-17, which is after `audit_at`).
- INCOMPLETE ⇒ FINDINGs (with evidence), then REMEDIATION (`""`), then VERIFICATION (`""`). **Every** INCOMPLETE verification step uses `""`, including checks of records that were already compliant.
- INCOMPLETE verification covers the **full required end state**, not only the remediated items. It includes the primary account (`VERIFY_RECORD_ABSENT account_service account_X`) and retained security records (`VERIFY_RECORD_PRESENT security_service audit_record_Y`), even when neither appears among the findings.
- Absence can be proven by **omission**: case_01 cites `evidence_54` for `account_001` ABSENT, and `account_001` appears nowhere in that file. That file's primary table simply lacks it.

### 1.4 Deliverables (beyond output.csv)
`code.zip` (runnable code, README, prompts, `evaluation/evaluation_report.md`, `evaluation/usage_report.md`), the root `log.txt` as the chat transcript, and the AI interview. See A12 for where the evaluation reports should live. `code/main.py` and `code/evaluation/main.py` currently exist but are empty (0 bytes).

## 2. Step 0 — does the request authorize deletion?
- Evaluation request texts come from a small template set: 27 distinct opening sentences and 67 distinct sentences overall. There are 12 English deletion openers plus Portuguese, Spanish, French, Korean and Japanese deletion openers (12–14 cases each). Follow-up sentences are boilerplate.
- **10 non-deletion requests** use one-off phrasings: phone update, data download/portability, login reset, surname correction, "remove me from marketing lists only", "what information do you hold", invoice copy, email change, and a copy of held information. Each pairs a request with an explicit keep or negation clause ("Do not close the account", "I am not asking for deletion"…). They are clustered around `case_533`–`case_542`, and `system_activity` contains 10 `CLASSIFY_REQUEST … as non-deletion` rows.
- Traps: "remove me from marketing lists" and "don't delete" contain deletion vocabulary. Classify on intent, and give negation clauses priority over keywords.

## 3. Email → user identity resolution
1. Normalize by trimming and lowercasing. Evidence contains UPPERCASE variants (e.g. `OBI.ADAEZE@ICLOUD.COM`, `CHOI.JIHOON94@GMAIL.COM`).
2. Look up the email in `user_identities.email`. That gives `user_id` and the current name, phone and email_hash. The evaluation set has 417 current-email matches.
3. Otherwise, look it up as `user_audit_log.old_value` where `field_name='email'`. The evaluation set has 100 historical-only matches plus 1 email that is current and also appears in the audit log as a `new_value`. **All 518 evaluation and 32 sample emails resolve to exactly one user. No email maps to two users.**
4. Build the subject's **full identifier set** by walking the audit chain for that `user_id`:
   - all emails (current + every old/new value), all phones, all email_hashes (current + audit-log hashes; also compute sha256 for each email);
   - name; `user_id` itself (used as `customer_reference` / `subject_reference` / `correlation_key` in evidence).
   Chains can be longer than one step: `user_10` has nadia.ionescu → nadia.petrescu → n.petrescu, and `user_35` has abena.o189491 → …466 → …1894. Evidence may reference any link in the chain (e.g. handover file `evidence_03` uses `abena.o189491@zoho.com`).
5. Hash-only links: some evidence carries only a hash, e.g. `correlation_key="44a651…"` for user_12's old email hash in `evidence_01`.
6. Timing: every historical email change happened before `requested_at`. No identity change or snapshot `updated_at` falls after any case's `audit_at`, so the identity data needs no time filtering (a guard is still cheap).
7. **Decoys** to reject. Matching must be exact on identifiers, never fuzzy:
   - near-miss emails: `g.silva76.jr@gmail.com` vs `g.silva76@gmail.com`, `smartinez6461.jr@…`, `nadia.popescu` vs `nadia.ionescu`, `customer4104@yahoo.com`;
   - near-miss names: Jonas Berg / Jonas Berglund / Joana Berg; Amina vs Karim El-Masri; and 3 duplicated full names in identities (Mateo Ríos, Thandiwe Ndlovu, Gabriel Costa).
8. One user can have several cases at different times: `user_18` (case_33 and case_52), `user_17` (case_46 and sample 17), `user_07` (case_61 and sample 07). Each case must be evaluated independently at its own `audit_at`.

## 4. Linking resources to the subject
`system_activity` almost never names the person: only 12 rows contain an email or user_id. Ownership must come from evidence content:
- **Direct fields**: `email`, `owner_email`, `customer_email`, `requester_email`, `subject_reference`, `customer_reference`, `user_id`, `lookup_key`, `match_key`, `correlation_key`, `subject_key`, `query`.
- **Trace correlation in logs**: a `*_started` line carries `lookup_key=<email>`, and its matching `*_completed` line (same `trace_id` / `requestId` / `[tr-…]`) carries `resource_id`. The two lines must be joined.
- **Activity details** naming a second resource: `Active connector repopulated downstream resource search_2818`, `Restore job repopulated downstream resource search_4882`, and `LINK_RESOURCE_IDENTITY` rows (12, all linked to user_03…user_22-style multimodal cases, e.g. `account_5762 → haruka.s1838@zoho.com, user_11`).
- **Implicit links** (weakest; see A5): in INCOMPLETE samples the subject's primary `account_*` sometimes appears *only* in a `DELETE_RECORD … submitted primary profile account_X` activity row clustered with the case's other workflow rows shortly after `requested_at`. Some gold `audit_record_*` IDs (e.g. `audit_record_6645`, `audit_record_5569` for samples 18/20) appear in **no** participant file.

## 5. Policy (`deletion_rules.csv`) and resource-type mapping
| Prefix / system (observed) | Category (proposed) | Required action |
|---|---|---|
| `account_*` / account_service (primary + any replica table) | USER_PROFILE | DELETE |
| replica / secondary copies, contact lookups keyed by an old email or hash | SECONDARY_IDENTIFIER (?) | DELETE |
| `search_*` / search_service | SEARCH_INDEX_ENTRY | DELETE |
| `cache_*` / cache_service | CACHE_ENTRY | DELETE |
| `support_*` conversation with shared context | SUPPORT_MESSAGE | REDACT |
| `support_*` / `object_*` attachment owned only by the subject | SUPPORT_ATTACHMENT | DELETE |
| `analytics_*` profile still attributable | ANALYTICS_PROFILE | ANONYMIZE |
| (telemetry) | PRODUCT_TELEMETRY | ANONYMIZE |
| `export_*` / export_service, object_store | USER_EXPORT | DELETE |
| `marketing_*` / marketing_platform | MARKETING_PROFILE | DELETE |
| `email_delivery_*` | EMAIL_DELIVERY_LOG | REDACT if it contains the email |
| `audit_record_*` / security_service | SECURITY_AUDIT_LOG | RETAIN (→ VERIFY_RECORD_PRESENT) while `retention_until` > audit_at |
| `legal_hold_*` / legal_service, retained order records | LEGAL_HOLD_RECORD | RETAIN while the hold is active |
| `backup_*` / backup_service | BACKUP_ARCHIVE | RETAIN, but restore must be BLOCKED |
| `tombstone_*` / identity_service | ACCOUNT_TOMBSTONE | REDACT: keep `subject_reference`, null name/email/phone/email_hash |
| `data_flow_*` / integration_service | (process, not data) | must be DISABLED if it targets the subject |

Decision per resource (state at `audit_at`): required action satisfied → verification only. Violated → finding + remediation + verification:
- DELETE-category record present → `REPORT_REMAINING_RECORD` → `DELETE_RECORD` → `VERIFY_RECORD_ABSENT`.
- REDACT/ANONYMIZE-category record still exposing identifiers → `REPORT_EXPOSED_PII` → `REDACT_RECORD`/`ANONYMIZE_RECORD` → `VERIFY_RECORD_REDACTED`/`_ANONYMIZED`.
- Connector enabled for the subject → `REPORT_ACTIVE_DATA_FLOW` → `DISABLE_DATA_FLOW` → `VERIFY_DATA_FLOW_DISABLED`.
- Backup `restore_allowed=true` → `REPORT_RESTORE_RISK` → `BLOCK_RESTORE` → `VERIFY_RESTORE_BLOCKED`.
- Retained record present → `VERIFY_RECORD_PRESENT`. If it is missing, that is a violation with no allowed operation; see A9.

## 6. Evidence types — what each contains
All files carry a manifest `captured_at`, and internal timestamps never exceed it (checked for all logs and snapshot bundles).

### 6.1 SQL snapshots (24, `databases/`) — all load cleanly into in-memory SQLite
- Header comment: `Captured at`, `Database topology` (primary_only / read_replica / regional replicas / reporting_replica / failover_replica).
- **`customer_data_{read,reporting,failover}_replica_*`, `customer_data_regional_replicas_*`** (10 files): `customer_data_primary` + 1–2 replica tables, `resource_id, origin_system, collection_name, record_json, …`. `record_json` holds full objects (analytics reports, security audit events, backup catalog, connector configs, legal retained orders, search docs, support conversations, ticket attachments, export jobs, deleted_customer_references). **Every replica holds 1–2 rows absent from primary** (the planted drift/ghost rows). No differing content was observed for shared IDs.
- **`customer_accounts_failover_replica_20260902`** (`evidence_54`): `customer_profiles_primary` vs `_failover_replica`. account_005 exists only in the replica, which is the ghost in sample case_05.
- **`customer_data_inventory_*`** (11 files): typed tables `customer_profiles, customer_search_documents, customer_analytics_profiles, support_records, customer_exports, connector_subscriptions(enabled), backup_restore_entries(restore_allowed), security_audit_records(retention_until)`, plus `snapshot_scope(origin_system, captured_at)`. The scope says which systems were captured. A system **not in scope** proves nothing about absence. Example: `inventory_20260905` scopes only account_service and has 0 rows.
- **`customer_search_primary_20260910`**: `search_documents_primary(display_name,email,source_resource_id)`, which links a search doc to its account.

### 6.2 JSON records (28, `records/`)
- **`customer_data_snapshot_bundle_*`** (12): `snapshots[]` of `{captured_at, data_store, collection, records[]}`. There are 14 collection types (customer_profiles, customer_search_documents, customer_analytics_reports, backup_catalog, connector_configurations, security_audit_events, support_conversations, ticket_attachments, customer_export_jobs, retained_order_records, deleted_customer_references, and `operational_record_history` from support_storage / backup_restore / privacy_treatment services). Each inner snapshot has its own `captured_at` ≤ the manifest time. An empty `records: []` for a store means that store was empty at that time.
- **`service_resource_export_*`** (11): per-service read-backs (`profile_lookups` with `status_code` 404/`profile:null`, `analytics.profiles`, `restore_entries.restore_allowed`, `exports`, `connectors.enabled`, `index_queries.hits`, `audit_records.retention_until`, `support.tickets`), each with `observed_at`.
- **Ledger-style lists** (`connector_configurations_20260812`, `customer_export_manifest_20260409`, `security_audit_ledger_20261001`): `{recorded_at, system, resource_id, record_type (=rule category), result, subject_reference}`. Result vocabulary: FOUND / NOT_FOUND / IDENTIFIABLE / ANONYMIZED / REDACTED / ACTIVE / DISABLED / BLOCKED. **Filenames are misleading**: "connector_configurations" and "export_manifest" contain mixed-system ledgers.
- `customer_analytics_report_20260907`, `customer_export_jobs_20260914`: single collections. Analytics with all-null identifiers means anonymized. An export with `download_available` true means present.

### 6.3 Text (`records/customer_account_handover_20261001.txt`, evidence_03)
Pipe-delimited ledger lines: `time | system | resource_id | category | result | subject_reference`. Captured exactly at 2026-10-01T12:00:00Z, which equals the maximum `audit_at`.

### 6.4 Logs (26, `logs/`) — six syntaxes
logfmt (`level=… service=… event=…`), bracketed (`[DEBUG] [svc] [tr-…]`), pipe-delimited, RFC5424 syslog with embedded kv, JSON-per-line after the timestamp, and AWS-style `requestId=… message="k=\"v\" …"` (escaped). The parser must normalize all of these into `(ts, service, trace_id, event, kv)`.
- `resource_lookup_trace_*` (12, `service_readback_logs`): clean read-backs per resource. `profile_lookup_completed rows=0 http_status=404` ⇒ absent. `connector_config_read_completed enabled=true` ⇒ active flow. Analytics and support reads list identifier fields (null ⇒ anonymized/redacted).
- Observability exports (`service_events_*`, `structured_*`, `syslog_*`, `operations_stream_*`, `application_trace_*`, `cloud_runtime_*`): noisy. Lookup start/complete pairs, `message_pii_scan`, `attachment_pii_scan`, `analytics_identity_scan`, `restore_policy_read_completed`, `restore_candidate_evaluated`, `connector_heartbeat`, plus noise (`dns_cache`, `disk_latency`, `certificate_checked`, `dependency_retry`…).
- `notification_delivery_20260315`, `search_index_diagnostic_20260906`: `record_lookup` ledger lines (same schema as §6.2 ledgers) despite their names.
- `support_replica.log` (evidence_61): `deletion_marker_check marker_found=false` for support_002 under the subject's old email, plus decoys `customer4104@…` and `nadia.popescu@…`.

### 6.5 PDFs (4, `documents/`) — text-extractable with `pdftotext -layout`
- `account_history_report_20260718` (evidence_18): customer statement for user_18 / ingrid.dahl, order ORD-20261260, payment and address. Linked to `export_1842` (statement NST-2026-0718-1842) by activity_4009.
- `delivery_correction_attachment` (evidence_58): support attachment `support_001`, Priya Raman, email and phone. This is a subject-only attachment that must be deleted (sample case_07).
- `legal_retention_register_20260529` (evidence_27): disputed-order register `ORD | user_XX | reason | date | amount | Retain until 2033`, i.e. legal holds.
- `security_retention_extract` (evidence_55): `audit_record_003`, user_03, retain until 2027-08-25.

### 6.6 Screenshots (11, `images/`, 1500–1800 px wide)
Internal admin consoles: account admin, analytics profile, backup restore, connector management, export console, deleted-account reference registry, marketing profile, object-storage browser, search inspector, security audit explorer, support ticket. Each shows a list plus a detail pane: IDs, emails, user refs, enabled/Ready/Restore state, timestamps. Each image pairs with one `LINK_RESOURCE_IDENTITY` activity (multimodal cases ≈ `case_33`–`case_63`). Local `tesseract` OCR works but **corrupts key tokens** (`user13`, `g.silva76,jr`, `data_flow.9846`), so a vision model, or OCR plus strict regex repair and validation against known IDs, is needed. Some screens include several decoy rows (e.g. backup console: 4 archives with only `backup_9287` belonging to the subject).

### 6.7 `system_activity.csv`
Event types: DELETE_RECORD 890, UPDATE_RECORD 672, SYNC_RECORD 447, INDEX_RECORD 400, SEND_EMAIL 368, REFRESH_CACHE 284, RETAIN_RECORD 206, ANONYMIZE_RECORD 204, REDACT_RECORD 173, CREATE_EXPORT 155, CHECK_DATA_FLOW 89, DISABLE_DATA_FLOW 36, BLOCK_RESTORE 36, CHECK_RESTORE_ACCESS 28, LINK_RESOURCE_IDENTITY 12, CLASSIFY_REQUEST 10, RESTORE_BACKUP 1. Results: SUCCESS 2914 / FAILED 721 / PARTIAL 376.
- These are claims. DELETE SUCCESS followed by `SYNC_RECORD … final checkpoint` to a replica ⇒ a replica copy probably survives (case_05). `CHECK_DATA_FLOW … remains enabled` / `CHECK_RESTORE_ACCESS … can still be restored` are useful risk signals. `SYNC_RECORD … repopulated downstream resource X` and `RESTORE_BACKUP … repopulated …` mean recreation after deletion.
- Samples show that a FAILED action does not imply a finding (case_25: account DELETE FAILED, yet gold says ABSENT per evidence_92), and a SUCCESS does not imply compliance (case_24: DELETE SUCCESS, yet the account is present in evidence_92). **Evidence state wins over activity results.**

## 7. Time-cutoff rule (`audit_at`)
- Use an evidence file only if its manifest `captured_at ≤ audit_at`. Use an activity row only if `event_time ≤ audit_at`. Inside a file, use only rows or snapshots whose own timestamp is ≤ `audit_at` (needed for multi-day logs and bundles).
- **Latest observation wins** per `(system_id, resource_id)`. Later evidence before the cutoff overrides earlier evidence. A later activity that *recreates* data (sync/restore/repopulate) re-opens a resource that earlier evidence showed absent. A later "SUCCESS" action alone does **not** close it (a claim).
- Evidence after `audit_at` is ignored even if it is more informative. Example: `evidence_02`@09-17 vs `audit_at` 09-15 in case_21–30. The `LINK_RESOURCE_IDENTITY` for `export_1842` (07-18) applies to case_33 (audit 07-19) but **not** case_52 (audit 06-19).
- Boundary: `evidence_03` and `evidence_50` are captured exactly at 2026-10-01T12:00:00Z, which equals the latest `audit_at`. Treat ≤ as inclusive, since samples 11/13/14 cite evidence_03 with `audit_at` = 2026-10-01T12:00:00Z.
- The cited `evidence_id` for COMPLETE verification is the latest qualifying file that shows the state (tie-break: see A6).

## 8. Ambiguities / open questions
- **A1 Sort order of `case_id`.** The input is numeric (`case_99, case_100`); "sorted by case_id" could mean lexicographic. *Proposal:* numeric, matching the input and sample files. Confirm if the grader is strict.
- **A2 Which files to cite.** The rules allow `activity_id`/`audit_id`, but the samples cite only `file_id`s. *Proposal:* cite file_ids, and fall back to an activity_id only when no file proves the state.
- **A3 Identity of resources only implied.** Gold verification in samples 18/20 includes `account_*` known only from a `DELETE_RECORD primary profile` activity, and `audit_record_*` IDs found in no participant file. *Proposal:* include the primary account when it can be linked (evidence, or a workflow cluster near `requested_at`). Accept that some gold verification steps are unrecoverable.
- **A4 Scope of verification in INCOMPLETE cases.** The samples include the account and audit-record checks even when they are not findings. *Proposal:* verify every subject-linked resource in its required end state.
- **A5 Workflow clustering as a link.** Activity rows for one case's resources are interleaved with other cases' rows. Time proximity is suggestive, not proof. How far to trust it is open.
- **A6 Multiple qualifying files.** When several files before `audit_at` show the same state, which one is canonical? Samples suggest the latest. Case_03 and case_21 cite one file for all steps, even where another file also qualifies.
- **A7 Category of ambiguous records.** Sample 12 has an `analytics_9914` contact lookup (email-hash key, `email_opt_in`) whose gold action is DELETE, not ANONYMIZE, so the prefix does not determine the category. Support: MESSAGE (redact) vs ATTACHMENT (delete) depends on content. `object_19` (object_storage) vs `object_store` naming differs between activity and images.
- **A8 Conflicting evidence.** The marketing console screenshot shows *Soo-jin Park / sj.park@naver.com* for `marketing_001`, while activity_4008 links `marketing_001` to kwame-mensah@proton.me / user_09. Which source wins? *Proposal:* direct identifier match wins over a catalog claim; flag it in the report.
- **A9 Retained record missing.** If a SECURITY_AUDIT_LOG or legal-hold record is gone, there is no restore operation. Does that make the case INCOMPLETE with only a finding? No example exists.
- **A10 Replica ghosts.** Is a replica-only row the same `resource_id` as the primary (sample: yes, `account_005`), with `system_id` = the logical system (`account_service`), not the replica name? *Proposal:* yes.
- **A11 Snapshot scope and omission.** Absence-by-omission is valid only when the system is in scope (`snapshot_scope`, bundle `data_store`, or a full-table dump). How should partial captures be handled?
- **A12 Report location.** AGENTS.md says `evaluation/…` while README says `code/evaluation/…`. *Proposal:* put them in `code/evaluation/` (bundled by the README zip command) and mention it in the README.
- **A13 LLM vs deterministic.** The dataset is highly templated, so most cases can be solved with deterministic parsing. LLM or vision is mainly needed for request intent (10 non-deletion cases + multilingual), screenshots, and maybe PDFs. `usage_report.md` still needs real token counts. Choosing the provider/model is open.
- **A14 Challenge deadline** is not configured in the repo; none is assumed.
- **U7 PARTIAL final checkpoints (open assumption).** A recreation activity reopens a resource unless its result is FAILED, so the 25 `PARTIAL` "final checkpoint" syncs reopen just like the 31 `SUCCESS` ones (`dossier.RECREATION_RESULTS`; there are no FAILED recreation rows). The samples don't settle this. No sample touches a PARTIAL checkpoint, and the only 3 recreation rows on sample gold resources (case_05 activity_3483, case_16 activity_3577, case_24 activity_3608) are SUCCESS, already backed by explicit PRESENT evidence (e54 L13, e05 L52, e92). All 25 PARTIAL checkpoints follow a DELETE of the same resource. Each PARTIAL reopen is logged as `recreation_partial`. **To confirm in T9:** samples 05, 16 and 22 test only the SUCCESS path, so instead check whether the replica SQL shows each PARTIAL resource explicitly (a replica-only row, an absence, or no capture). If every reopened resource also has explicit PRESENT evidence, the rule changes no status. Otherwise, list the case_ids that depend on it.

## 9. Decisions (2026-10-24, user review)
| # | Decision |
|---|---|
| A1 | Sort output rows by the **numeric** suffix of `case_id` (`case_33 … case_99, case_100 … case_550`). |
| A2 | Cite **file_ids** (`evidence_NN`) only. |
| A3 / A4 | INCOMPLETE verification includes the subject's primary account (`VERIFY_RECORD_ABSENT`) and retained audit records (`VERIFY_RECORD_PRESENT`) **when they are linkable**. Gold steps that cannot be recovered from participant data are accepted as lost. |
| A5 | Time clustering of activity rows is only a **flagged, low-confidence fallback**. It never overrides evidence. |
| A6 | **Approved.** Citation rule: **Rule E**: cite the latest file with manifest `captured_at ≤ audit_at` that explicitly names the resource (71/71 sample citations). Otherwise **Rule O**: cite the latest whole-store capture of that system, in scope, that lacks the resource. **Flagged exception:** case_04 gold cites `evidence_54`, although the later in-scope empty inventory `evidence_90` also qualifies. Log it when Rule O has more than one candidate. |
| A7 | The data category comes from the record's **content and type** (collection, record_type, fields), not from its ID prefix. |
| A8 | A direct identifier match wins over a catalog or activity claim. The conflict is logged. |
| A10 | `system_id` is the **logical system** (e.g. `account_service`), never the replica table name. The `resource_id` stays the same. |
| A11 | Absence by omission counts only when that system is **in scope** for the capture. |
| A12 | Evaluation reports go in `code/evaluation/`. |
| A13 | The core is deterministic. An LLM is used only for request intent and for screenshot/PDF extraction, plus optional A7 category disambiguation (off by default, deterministic fallback; DESIGN §4.1). |
| U3 | **Two clocks.** (1) Each **record's own timestamp** (log line time, inner snapshot `captured_at`, `observed_at`, ledger `recorded_at`) decides whether that record may be used for **linking and for state** at `audit_at`, even inside a file whose manifest time is later. (2) The **manifest `captured_at`** decides **citations only**: never cite a file whose manifest time is after `audit_at`. If the only proof of a state is in such a file, the step still exists but cites the best qualifying file, or is unciteable (a COMPLETE-case verification then needs another qualifying file). Check: `evidence_06` (manifest 09-18) holds lines stamped 09-16T08:52–08:57, before case_09's `audit_at` 09-16T09:00. These recover `audit_record_4902` (L24 `subject_ref=user_61` → L28 by `tr-9028396`) and `account_3960` (L82 `lookup_key=k.appiah88@gmail.com` → L86 `rows=0 404` by `tr-1463969`). The gold finding still cites `evidence_93`, as the rule predicts. `evidence_02` inner snapshots likewise recover the case_23/24/29/30 audit records and the case_26 account link. |
| L1 | **Account linking fallback** (see `docs/LINKING_EXPERIMENT.md`). It applies only when evidence links no primary account to the subject. **(1) M6:** remove `DELETE_RECORD` primary-profile candidates in the learned window (−8 … +74 min before `reported_at`) whose account evidence links to *another* case. Pick the account if exactly one candidate is left. **(2) M9 for ties:** cases that compete for the same candidates are paired by rank, sorting cases by their first evidence-linked activity_id and candidates by activity_id. If a group has more cases than candidates, the unpaired cases get the nearest offset (M7). If no candidate is left after elimination, take the nearest pre-elimination candidate, then widen the window to ±1 day. If there is still none, omit the step and log `link_unresolved`. **(3) Always emit the account step** (`VERIFY_RECORD_ABSENT account_service <id>`) for every deletion case. A guessed account only ever produces a verification step, never a finding or remediation, so a guess cannot change `deletion_status`. In a COMPLETE case it cites the latest in-scope Rule O capture of `account_service` (manifest ≤ `audit_at`) if one exists; otherwise `evidence_id=""`, logged as `guessed_account_uncited`. **(4)** Every M6, M9, M7 or widened-window pick is written to the diagnostics file with `confidence=low`, the method, the candidate set and its competitors. **(5)** Config flag `LINK_FALLBACK=guess\|omit` (env var, CLI `--link-fallback` overrides, default `guess`). `omit` turns off steps (1)–(2) entirely and emits the account step only when evidence links the account, so the effect can be measured. Analytics U1 records use MA only (elimination + unique match, flagged low-confidence) in both modes, and are omitted on ties (no guess). |
