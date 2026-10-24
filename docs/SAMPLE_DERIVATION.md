# Sample Derivation — tracing every gold step to evidence

Status: draft for review (2026-10-24). Scope: all 32 rows of `dataset/sample_output.csv` (193 steps).
Method: each gold step was matched against every participant file and every `system_activity` row. Proof is accepted only from data with a timestamp **at or before the case's `audit_at`**.
Line numbers refer to the raw file. For PDFs they refer to `pdftotext -layout` output. No sample cites a screenshot.
File shorthand: `eNN` = `evidence_NN`. Captured-at times are in brackets.

Proof terms used below:
- **state proof**: the line that shows the resource's condition (present, absent, enabled, nulls…).
- **link proof**: the line that ties the resource to the subject (email, hash, `user_id`, or a trace join).
- *trace join*: a `*_started` line carrying the subject's identifier, joined by `trace_id` to a `*_completed` line carrying the `resource_id`.

---

## 1. Global findings

1. **Status rule holds for all 32 cases.** A case is INCOMPLETE if and only if at least one linked resource violates policy at `audit_at`, according to the latest evidence. Reported status and activity results never decide it:
   - case_25: DELETE FAILED, yet the account is ABSENT;
   - case_24: DELETE SUCCESS, yet the account is PRESENT;
   - case_26: an older snapshot shows the account ACTIVE, but a newer one shows it gone.
2. **Ordering**: 0 deviations from phase → canonical operation order → `system_id` → `resource_id`.
3. **Remediation** = one step per finding (REPORT_REMAINING→DELETE, REPORT_EXPOSED_PII→REDACT/ANONYMIZE by category, ACTIVE_DATA_FLOW→DISABLE, RESTORE_RISK→BLOCK).
   **Verification** = one step per linked resource in its required end state. That set is every remediated resource **plus** the primary account (ABSENT) **plus** retained `audit_record`s (PRESENT), when they are known.
4. **Every INCOMPLETE verification step uses `""`**, including checks of records that were already compliant.
5. **Replica ghosts use the logical system and the same id**, which confirms A10:
   - `account_005` appears only in table `customer_profiles_failover_replica` (e54 L13) → `account_service/account_005`;
   - `account_994` appears only in table `customer_data_read_replica` (e05 L52) → `account_service/account_994`.
6. **Cited ids are file_ids only** (75 citations), which confirms A2.
7. **Gold ignores legal-register rows.** e27 (legal_retention_register, 05-29) lists open retention for user_08, user_12, user_17, user_21, user_24 and user_29, all of which are sample subjects. **No sample gold contains a LEGAL_HOLD step.** Either those holds are scoped to orders and not to identity records, or they are decoys for samples. This is an open policy question (see §5, U6).

## 2. A6 resolved — which evidence file is cited

**Rule E (explicit observation): cite the latest file with `captured_at ≤ audit_at` that explicitly mentions the resource.**
Checked against every cited step that names its resource in the cited file: **71 of 71 match.** Later files that would otherwise qualify are skipped because they are after `audit_at`. Examples:
- `e02` (09-17) for cases 21–30 (audit 09-15);
- `e06` (09-18) for case_09 (audit 09-16).

The equality boundary is inclusive: `e03` and `e51` are cited where `captured_at == audit_at`.

**Rule O (absence by omission): cite a whole-store capture of the system in which the resource does not appear.** 4 steps:

| Case | Step | Cited | Why | Fits "latest"? |
|---|---|---|---|---|
| case_01 | VERIFY_RECORD_ABSENT account_001 | e54 [09-02T23:00] | `customer_profiles_primary` (L17–24) and `_failover_replica` (L8–16) both lack it | yes (e90 is after audit) |
| case_02 | VERIFY_RECORD_ABSENT account_002 | e54 | same | yes |
| case_04 | VERIFY_RECORD_ABSENT account_004 | e54 | same | **no**: e90 [09-05T07:18] has `snapshot_scope=account_service` and an empty `customer_profiles`, and is later than e54 but ≤ audit 09-07 |
| case_31 | VERIFY_RECORD_ABSENT account_4857 | e51 [09-15T12:00] | L6–9: `account_service/customer_profiles` `records: []` | yes |

**Resolution (proposed):**
1. Use Rule E whenever any file at or before `audit_at` names the resource.
2. Otherwise use Rule O with the latest whole-store capture of that system. A primary/replica table dump such as e54 is preferred over an inventory, because an inventory may be a filtered extract (U4).
3. A COMPLETE case may cite different files for different steps (case_04: e54 + e56). Citation is chosen per step, not per case.

## 3. Families

| Family | Cases | Cited file type | Status pattern |
|---|---|---|---|
| F1 Handcrafted narrative | 01, 02, 04, 05, 06, 07, 08, 10 | one targeted file per case (SQL failover e54, search SQL e57, PDF e58, export JSON e59, analytics JSON e56, replica log e61) | 01/02/04 COMPLETE; others INCOMPLETE |
| F2 Read-back trace log | 03, 18, 20 | `resource_lookup_trace` e94 (logfmt, explicit `resource_id` + `lookup_key`) | 03 COMPLETE; 18, 20 INCOMPLETE |
| F3 Observability log with trace joins | 12, 15, 17, 19 | e01 `service_events_ap_south` | all INCOMPLETE |
| F4 Shift-handover ledger | 11, 13, 14 | e03 `customer_account_handover.txt` (pipe ledger, `result` column) | all INCOMPLETE |
| F5 Replica drift SQL | 16, 22 | e05 `customer_data_read_replica` | both INCOMPLETE |
| F6 Inventory SQL | 09 | e93 `customer_data_inventory` | INCOMPLETE |
| F7 Service read-back export | 21, 23–30 | e92 `service_resource_export` (+ e02 bundle, file timestamped after audit) | 21, 25–28 COMPLETE; 23, 24, 29, 30 INCOMPLETE |
| F8 Tombstone | 31, 32 | e51 snapshot bundle | 31 COMPLETE; 32 INCOMPLETE |

Common rule (F2–F8): the request text is boilerplate, and the gold set is every resource that the case's "audit snapshot" file ties to the subject. That file is captured shortly before `audit_at` (case_09: 22 min; case_03: 21 min; F7: 15 min).

### F1 — Handcrafted narrative (01, 02, 04, 05, 06, 07, 08, 10)
**Rule:**
- The request text describes the issue in plain words.
- Resource ids use 3-digit suffixes (`account_00N`, `search_001`, `support_001`, `export_001`), and N equals the user number (user_0N).
- The workflow claim is a single activity row in the "Target resource: X." template.
- One file planted before `audit_at` shows the true state.
- No audit-record verification is included.
- **Link proofs for `account_00N` exist only by numbering, never in evidence (see §5, U1).**

| Case | Step | Proof (state) | Link proof |
|---|---|---|---|
| 01 | V ABSENT account_service/account_001 [e54] | e54 L17–24: primary table lacks account_001; L8–16 replica lacks it too | none in evidence; activity_3342 [08-20] names account_001 only |
| 02 | V ABSENT account_002 [e54] | e54 L17–24 (omission). activity_3376 PARTIAL is overridden | none; activity_3376 |
| 04 | V ABSENT account_004 [e54] | e54 L17–24 (omission) | none; numbering |
| 04 | V ANONYMIZED analytics_901 [e56] | e56 L7–11: `user_id/email/phone/email_hash` all null | **none**: the record is anonymized; only activity_3463 links it |
| 05 | F REMAINING account_005 [e54] | e54 L13: replica row `account_005, user_05, Leila Haddad, leila.haddad@yahoo.com`, ACTIVE; primary L17–24 lacks it | e54 L13 (user_05 + email). Cause: activity_3483 SYNC to `account_failover_replica` 8 s after DELETE |
| 05 | R DELETE / V ABSENT account_005 | derived from the finding | — |
| 06 | F REMAINING search_service/search_001 [e57] | e57 L9: `search_001,'Jonas Berg','jonas.berg@outlook.com','account_006'` | e57 L9 email. Decoys rejected: L10 Jonas Berglund, L11 Joana Berg |
| 06 | V ABSENT account_006 | e54 L17–24 (omission, 09-02 ≤ 09-10) | e57 L9 `source_resource_id=account_006` |
| 06 | R DELETE / V ABSENT search_001 | from the finding | — |
| 07 | F REMAINING support_platform/support_001 [e58] | e58 L2/L9 `CS-4827 / support_001`; L13–17 name, email, phone; p.2 "Attachment properties…uploader customer" ⇒ SUPPORT_ATTACHMENT ⇒ DELETE | e58 L15 email |
| 07 | V ABSENT account_007; R/V support_001 | account_007: e54 omission | account_007: none (numbering) |
| 08 | F REMAINING export_service/export_001 [e59] | e59 L7–12: `owner_email gabriel.costa@proton.me`, `download_available: true` | e59 L8 |
| 08 | V ABSENT account_008; R/V export_001 | account_008: e54 omission | account_008: none (numbering) |
| 10 | F REMAINING support_platform/support_002 [e61] | e61 L6: `deletion_marker_check resource_id=support_002 marker_found=false` | e61 L4 `lookup_key=nadia.ionescu@yahoo.com` (historical email, audit_121) joined by `trace_id=tr-rp-002` to L5. Decoys: L2 customer4104, L11 nadia.popescu |
| 10 | V ABSENT account_010; R/V support_002 | account_010: e54 omission | account_010: none (numbering) |

### F2 — Read-back trace log e94 [09-26T10:38:56] (03, 18, 20; audit 09-26T11:00)
**Rule:** each resource appears as a `*_read_started resource_id=X lookup_key=<subject>` + `*_completed` pair. The state comes from the completed line: `rows=0/404` ⇒ absent, `enabled=true` ⇒ active, identifier fields non-null ⇒ exposed. All findings and COMPLETE verifications cite e94 (Rule E).

| Case | Step | State proof | Link proof |
|---|---|---|---|
| 03 | V ABSENT account_7566 [e94] | L19 `rows=0 http_status=404` | L18 `lookup_key=l.fontaine46@yahoo.com` |
| 03 | V PRESENT audit_record_2016 [e94] | L35 `rows=1 … retention_until=2034-09-30` | L34/L35 `user_43` |
| 03 | V ANONYMIZED analytics_9406 [e94] | L43 all identifiers null | **none**: L42 `lookup_key=null` (U1) |
| 18 | F REMAINING search_4013 [e94] | L53 `result=hit email=mori.ren@gmail.com` | L52 |
| 18 | F EXPOSED_PII analytics_037 [e94] | L23 `email=mori.ren@gmail.com` | L22 |
| 18 | F EXPOSED_PII support_5569 [e94] | L11 `requester_email=mori.ren@gmail.com` ⇒ SUPPORT_MESSAGE ⇒ REDACT | L10 |
| 18 | F ACTIVE_DATA_FLOW data_flow_4890 [e94] | L25 `enabled=true last_delivery_status=delivered` | L24 |
| 18 | R ×4, V for the 4 above | from the findings | — |
| 18 | V ABSENT account_5521 | **no state proof** in any file | **none**: activity_3732 only (U1) |
| 18 | V PRESENT audit_record_6645 | **id exists nowhere in participant data** (U2) | — |
| 20 | F REMAINING search_4262 [e94] | L9 `result=hit` | L8 |
| 20 | F ACTIVE_DATA_FLOW data_flow_1770 [e94] | L41 `enabled=true` | L40 |
| 20 | V ABSENT account_5037 | no state proof | none: activity_3734 only (U1) |
| 20 | V PRESENT audit_record_5569 | **id exists nowhere** (U2) | — |

### F3 — Observability log e01 [09-15T11:59:39] (12, 15, 17, 19; audit 09-15T12:00)
**Rule:** links require **trace joins**. The subject identifier is on a `*_started` line, as `lookup_key`, `subject_key` or `subject_ref`, or directly as `correlation_key` (a hash or `user_N`). The `resource_id` is on the matching `*_completed`, heartbeat or scan line with the same `trace_id`.

| Case | Step | State proof | Link proof |
|---|---|---|---|
| 12 | F REMAINING analytics_9914 [e01] → DELETE | L92 `contact_lookup_completed rows=1 email_opt_in=true` | L92 `correlation_key=44a651…` = **old** email hash (audit_283). Category DELETE, not ANONYMIZE (see U5) |
| 12 | V ABSENT account_9797 | L76 `rows=0 http_status=404` | L73 `lookup_key="minjun.kim@gmail.com"`, `tr-8001359` |
| 12 | V PRESENT audit_record_2945 | L63 `rows=1 integrity_check=passed` | L63 `correlation_key="user_12"` |
| 15 | F ACTIVE_DATA_FLOW data_flow_9846 [e01] | L88 `connector_heartbeat enabled=true scheduler_registered=true` | L85 `subject_key="lucia-torres@gmail.com"`, `tr-9750867`. Corroborated by e14 screenshot (08-07), "data_flow_9846 … lucia-torres" |
| 15 | V ABSENT account_4745 | L22 `rows=0 404` | L22 `correlation_key` email |
| 15 | V PRESENT audit_record_1596 | L103 `rows=1` | L101 `subject_ref="user_15"`, `tr-4314388` |
| 17 | F RESTORE_RISK backup_3245 [e01] | L53 `restore_candidate_evaluated eligible=true deletion_block=false` | L43 `subject_key="leonie.g9352@outlook.com"`, `tr-8210651` |
| 17 | V ABSENT account_4092 | L21 `rows=0 404` | L16 lookup_key, `tr-4708586` |
| 17 | V PRESENT audit_record_1797 | L33 `rows=1` | L33 `correlation_key="user_17"` |
| 19 | F REMAINING search_6083 [e01] | L36 `result=hit` | L36 `correlation_key="e.varga90@yahoo.com"` |
| 19 | F EXPOSED_PII support_9553 [e01] → REDACT | L15 `message_pii_scan matched_value="e.varga90@yahoo.com" detector=direct_identifier` | L11 `subject_key`, `tr-8896806`. REDACT, not DELETE: the request text says the conversation involves another customer (rule_05) |
| 19 | V ABSENT account_9102 | L72 `rows=0 404` | L70, `tr-5115879` |
| 19 | V PRESENT audit_record_6793 | L44 `rows=1` | L37 `subject_ref="user_19"`, `tr-6935637` |

Remediation and verification steps for the findings in F3 follow directly from the findings.

### F4 — Shift-handover ledger e03 [10-01T12:00] (11, 13, 14; audit = 10-01T12:00)
**Rule:** one pipe-delimited line per resource: `system | resource | category | result | subject`. Mapping:

| `result` | Category | Step |
|---|---|---|
| FOUND | DELETE category | REMAINING |
| IDENTIFIABLE | — | EXPOSED_PII |
| ACTIVE | DATA_FLOW | ACTIVE_DATA_FLOW |
| ACTIVE | BACKUP | RESTORE_RISK |
| NOT_FOUND | — | V ABSENT |
| FOUND | SECURITY_AUDIT_LOG | V PRESENT |

The category column also selects REDACT (SUPPORT_MESSAGE) or DELETE (SUPPORT_ATTACHMENT).

| Case | Steps → line in e03 |
|---|---|
| 11 (novak.tomasz) | account_4248 NOT_FOUND L11; analytics_2034 IDENTIFIABLE L16; data_flow_9522 ACTIVE L25; search_052 FOUND L26; audit_record_9394 FOUND user_32 L40; support_864 SUPPORT_MESSAGE IDENTIFIABLE L46 |
| 13 (noor-khalil) | account_9335 NOT_FOUND L15; search_1308 FOUND L27; audit_record_4816 user_33 L34; support_4218 **SUPPORT_ATTACHMENT** FOUND L43 → DELETE |
| 14 (y.tanaka32) | account_3677 NOT_FOUND L9; backup_3870 BACKUP_ARCHIVE ACTIVE L19; audit_record_987 user_37 L41 |

Every step in F4 is fully explained. Decoys in the same file are excluded by subject: e.g. `abena.o189491` (user_35's oldest email), `backup_706`/`data_flow_2901` (user_39).

### F5 — Replica-drift SQL e05 [09-21T10:00] (16, 22; audit 09-21T10:00)
**Rule:** load the SQL, parse `record_json`, and link by `customer_reference`/`user_id`/`email`. A row in the replica but missing from the primary is a REMAINING record of the logical system.

| Case | Step | State proof | Link proof |
|---|---|---|---|
| 16 | F REMAINING account_service/account_994 | L52 replica-only row, ACTIVE (primary has none) | L52 `customer_reference user_53` |
| 16 | F REMAINING search_1007 | L37 primary (+ L75 replica) | L37 user_53 |
| 16 | V PRESENT audit_record_9528 | L29 (+ L67) | L29 user_53 |
| 22 | F REMAINING search_8911 | L41 (+ L79) | user_51 |
| 22 | F EXPOSED_PII analytics_4440 → ANONYMIZE | L17 `user_id, email, phone, email_hash` populated | L17 user_51 |
| 22 | V PRESENT audit_record_3482 | L21 (+ L59) | user_51 |
| 22 | V ABSENT account_4990 | absent from e05 (whole-store dump, in scope) | **none**: activity_3573 only (U1) |

### F6 — Inventory SQL e93 [09-16T08:37] (09; audit 09-16T09:00)

| Step | State proof | Link proof |
|---|---|---|
| F RESTORE_RISK backup_4978 [e93] | L24 `backup_restore_entries … restore_allowed '1'` | L24 `customer_email k.appiah88@gmail.com` |
| V ABSENT account_3960 | **only** in e06 [09-18, after audit] L86 `rows=0 404` | **none ≤ audit**: activity_3703 only (U3) |
| V PRESENT audit_record_4902 | **only** in e06 [after audit] L28 | **none ≤ audit** (U3) |

### F7 — Service read-back export e92 [09-15T11:44:55] (21, 23–30; audit 09-15T12:00)
**Rule:** e92 holds per-service lists. The state comes from `status_code`, `enabled`, `restore_allowed`, and null vs. populated identifier fields. Links come from `customer_reference`/`subject_reference`/`query`/email fields in **e92**, plus the **e02** bundle. e02's file timestamp is 09-17, but its inner snapshots are stamped **09-15T12:00 = audit_at** (e02 L38–470), and they hold the only links for several audit records (U3).

| Case | Steps (all citing e92 for findings/COMPLETE verifications) |
|---|---|
| 21 COMPLETE | V ABSENT account_6440: L46–47 404, link **none** (U1). V PRESENT audit_record_3737: L288–290 user_21. V ANONYMIZED analytics_5335: L100–105 nulls, link **none** (U1) |
| 23 INCOMPLETE | F REMAINING export_7364: L149–154 owner_email. F REMAINING search_898: L249–261 email/query. V ABSENT account_410: **no state or link** (U1). V PRESENT audit_record_4128: link only in e02 L412 (U3) |
| 24 INCOMPLETE | F REMAINING account_5334: L22–29 `status_code 200` + email keiko. F REMAINING search_5577: L231–243. V PRESENT audit_record_7095: e02 L419 only (U3) |
| 25 COMPLETE | V ABSENT account_3136: L10–11 404, link none (U1). V PRESENT audit_record_451: L302–304 user_25. V ANONYMIZED analytics_1895: L55–60 nulls, link none (U1) |
| 26 COMPLETE | V ABSENT account_5684: L40–41 404. Link only via e02 L11 (inner 09-09 snapshot showed it ACTIVE, superseded by e92) (U3). V PRESENT audit_record_5416: L309–311. V REDACTED support_4715: L343–349 user_26, requester fields null. V ANONYMIZED analytics_4566: L73–78 nulls, link none (U1). V DISABLED data_flow_6244: L189–194 user_26 `enabled false`. V BLOCKED backup_3120: L122–126 user_26 `restore_allowed false` |
| 27 COMPLETE | V ABSENT account_3866: L16–17 404. The only link is e02 L524 (inner snapshot **09-17**, ACTIVE): a trap after audit_at that must be ignored. Link ≤ audit: none (U1). Others: audit_record_3830 L295–297, support_4707 L334–340, analytics_4574 L82–87 (link none, U1), data_flow_2295 L162–167, backup_5970 L130–134 |
| 28 COMPLETE | V ABSENT account_5482: L34–35 404, link none (U1). audit_record_9265 L316–318, support_2351 L325–331, analytics_6077 L109–114 (link none, U1), data_flow_2337 L171–176, backup_838 L138–142. Decoy: e02 L22–30 `smartinez6461.jr@gmail.com` search doc (as the request text warns) |
| 29 INCOMPLETE | F REMAINING search_1993 L213–225. F EXPOSED_PII analytics_5208 L91–96. F EXPOSED_PII support_5281 L352–358. F ACTIVE data_flow_3827 L180–185 `enabled true`. All four are linked by `noah.f438085@outlook.com`, a **historical** email (audit_284; the current email is `noah.f4380@`). The request also uses this historical email. V ABSENT account_7012: no state/link (U1). V PRESENT audit_record_3954: e02 L454 only (U3) |
| 30 INCOMPLETE | F REMAINING search_9359 L267–279. F EXPOSED_PII analytics_3697 L64–69. F EXPOSED_PII support_6477 L361–367. F ACTIVE data_flow_8524 L198–203. All are linked by the historical email `camille.bernard86@` (audit_280; the current email is `camille.bernard@`). V ABSENT account_4163: no state/link (U1). V PRESENT audit_record_1707: e02 L461 only (U3) |

### F8 — Tombstone bundle e51 [09-15T12:00] (31, 32; audit 09-15T12:00)

| Case | Step | State proof | Link proof |
|---|---|---|---|
| 31 | V ABSENT account_4857 [e51] | L6–9 `account_service` `records: []` (Rule O) | **none**: activity_3682 only (U1) |
| 31 | V PRESENT audit_record_6909 [e51] | L42 | L43 user_541 |
| 31 | V REDACTED identity_service/tombstone_440 [e51] | L17–23: subject_reference kept; name/email/phone/email_hash null; DELETED | L18 user_541 |
| 32 | F EXPOSED_PII tombstone_9178 [e51] → REDACT | L26–32: name, email, phone, email_hash still populated | L27 user_542 |
| 32 | V PRESENT audit_record_7068 | L49 | L50 user_542 |
| 32 | V ABSENT account_8858 | L6–9 (omission) | **none**: activity_3685 only (U1) |

## 4. Coverage summary
- 193 gold steps in total.
- **State fully explained by data at or before `audit_at`: 180.** The other 13 are U2 (2), U3 (6, excluding account_5684 whose state is in e92), and 5 accounts with no state anywhere (5521, 5037, 410, 7012, 4163).
- **Subject link fully explained by data at or before `audit_at`: 159.** The other 34 are U1 (25), U2 (2) and U3 (7).

## 5. Steps I cannot explain

**U1 — the resource's link to the subject is not in any evidence at or before `audit_at`.** The only signal is an activity row, or the 3-digit numbering in F1. 25 steps (each resource appears in exactly one step):

| Resource kind | Cases and resources |
|---|---|
| Primary accounts (18 steps) | 01 account_001; 02 account_002; 04 account_004; 07 account_007; 08 account_008; 10 account_010; 18 account_5521; 20 account_5037; 21 account_6440; 22 account_4990; 23 account_410; 25 account_3136; 27 account_3866; 28 account_5482; 29 account_7012; 30 account_4163; 31 account_4857; 32 account_8858 |
| Anonymized analytics (7 steps) | 03 analytics_9406; 04 analytics_901; 21 analytics_5335; 25 analytics_1895; 26 analytics_4566; 27 analytics_4574; 28 analytics_6077 |

Anonymized analytics are unlinkable by construction: once anonymized, nothing ties them to the user.

A time-cluster signal exists: the "submitted primary profile account_X" row lands 45–75 min before `reported_at`. It is **not unique**. Typical windows contain 3–6 candidates, and cases with the same `reported_at` share them: {24, 25, 26}, {27, 28, 29}, {13, 18, 20}, {14, 31, 32}, {16, 22}, {21, 23}, {15, 17}. Per A5, this signal is a fallback only.

**U2 — the id is absent from all participant data (2 steps):**
- case_18 V PRESENT `audit_record_6645`;
- case_20 V PRESENT `audit_record_5569`.
These cannot be recovered and are accepted as lost (A3).

**U3 — the link or state exists only in data timestamped after `audit_at`, or in a file whose manifest time is after `audit_at`. 7 steps:**
- case_09 `account_3960` and `audit_record_4902`: only in e06 [09-18 > 09-16].
- case_23 `audit_record_4128`, case_24 `audit_record_7095`, case_29 `audit_record_3954`, case_30 `audit_record_1707`: only in e02. The file is 09-17, but the record's inner snapshot is 09-15T12:00 = audit_at.
- case_26 `account_5684` link: e02 L11, inner snapshot 09-09.

The e02 pattern suggests the generator treats the **inner snapshot `captured_at`**, not the manifest `captured_at`, as the cutoff for *linking*. Citations still never use e02. **Decision needed:** may records inside a later file be used for linking when their own timestamp is ≤ `audit_at`?

**U4 — citation choice:** case_04 cites e54 rather than the later in-scope empty inventory e90 (§2).

**U5 — category choice:** case_12 `analytics_9914` gets DELETE. It is a `contact_lookup` with `email_opt_in=true` keyed by the old email hash, so it is a contact/marketing-like secondary identifier and not an ANONYMIZE analytics profile. This is consistent with A7 (content over prefix), but the exact rule mapping (SECONDARY_IDENTIFIER vs MARKETING_PROFILE) is inferred.

**U6 — evidence that gold does not use:**
- e27 legal-hold rows for user_08, user_12, user_17, user_21, user_24 and user_29 (no LEGAL_HOLD steps in gold);
- the e42 screenshot "retained reference user_17" (no tombstone step for case_17);
- the e14 screenshot (case_15 corroboration only).

## 6. Implications for the solver (for review, not yet implemented)
1. Building links from evidence alone recovers 159/193 links. To reach gold on primary accounts and anonymized analytics, the solver needs the low-confidence activity-cluster fallback (A5), flagged in output diagnostics.
2. Add per-record timestamps to the cutoff rule for linking (pending the U3 decision). Keep manifest time for citations.
3. Citation: Rule E, then Rule O (§2).
