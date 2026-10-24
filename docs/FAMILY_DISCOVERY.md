# Family Discovery — all 518 evaluation requests

Status: draft for review (2026-10-24). Analysis only: no solution code. The scratch scripts live in the session scratchpad, outside `code/` (`family_scan.py`, `late_inner.py`, `family_sig.py`, `family_comp.py`).

## Method
For every request (518 evaluation + 32 samples as a control):
1. Resolve the subject (SPEC §3). Build the identifier set: every current and historical email, every email_hash, and the `user_id`.
2. Find every evidence file whose text contains one of those identifiers. PDFs are read via `pdftotext`, PNGs via tesseract (OCR is lossy; see the caveats).
3. **Anchor file** = the latest file with manifest `captured_at ≤ audit_at` that contains a subject identifier. **Gap** = `audit_at − anchor.captured_at`.
4. **Companion files** = hit files whose manifest time is after `audit_at`. Each is classified by the U3 two-clock rule:
   - `+early`: at least one subject record inside the file carries its own timestamp ≤ `audit_at`, so it is usable for linking and state but not citable;
   - `+late_only`: every subject record is after `audit_at`, so it is a trap and must be ignored.
5. **Family** = (anchor file type, set of companion types). File type is decided by file structure (name pattern + format), not by `file_id`.

Control check on the samples:
- 29 of 32 anchors equal the file the gold actually cites.
- The other 3 (case_01, 02, 04) have no anchor. Their gold relies on absence by omission in `e54` (see SAMPLE_DERIVATION §2).

**Anchors are very close to `audit_at`:**

| Anchor type | Gap |
|---|---|
| replica SQL, ledgers, bundle | 0 h (captured exactly at `audit_at`) |
| observability logs | ≤ 2 min |
| read-back trace logs, service exports, inventories | 15–56 min |
| screenshots and PDFs (multimodal block) | 26 h |

502 of 518 evaluation cases have **exactly one** file with subject identifiers captured at or before `audit_at`.

## Families (evaluation counts; samples in brackets)
Families are generated in **blocks of 10 consecutive case ids**, except the mixed block `case_33–63`.

| # | Anchor type (files) | Companion(s) | Eval | Samples | Eval case ids | Covered by a sample? |
|---|---|---|---|---|---|---|
| F-A | Replica-drift SQL (e05, e10, e15, e20, e24, e29, e33, e38, e43, e47, e52) | — | **105** | 3 (05, 16, 22) | 54, 56–60, 62, 103–112, 153–162, 203–212, 253–262, 303–312, 353–362, 403–412, 453–462, 503–512, 543–550 | **Partly**: see sub-families |
| F-B1 | Inventory SQL (e63, e66, e69, e75, e78) | — | **50** | 0 | 243–252, 293–302, 393–402, 443–452, 493–502 | **No** (closest: case_09) |
| F-B2 | Inventory SQL (e81, e93) | observability log +early (e06, e21) | 19 | 1 (09) | 64–72, 213–222 | Yes (single sample) |
| F-B3 | Inventory SQL (e84, e87) | snapshot bundle +early (e12, e17) | 20 | 0 | 123–132, 173–182 | **No** |
| F-B4 | Inventory SQL (e72) | snapshot bundle +late_only (e31) | 10 | 0 | 333–342 | **No** (post-audit trap) |
| F-C1 | Service read-back export (e62, e68, e77, e80, e92) | snapshot bundle +early (e02, e22, e26, e40, e49) | 40 | 9 (21, 23–30) | 223–232, 273–282, 423–432, 523–532 | Yes |
| F-C2 | Service read-back export (e71, e83, e86, e89) | — | **40** | 0 | 93–102, 143–152, 183–192, 343–352 | Structurally (same file schema as C1, no companion) |
| F-C3 | Service read-back export (e65, e74) | observability log +early (e30, e44) | 20 | 0 | 313–322, 463–472 | **No** |
| F-D1 | Read-back trace log (e76, e82, e94) | — | 26 | 3 (03, 18, 20) | 43–44, 47–49, 51, 193–202, 283–292 | Yes |
| F-D2 | Read-back trace log (e70, e73, e91) | snapshot bundle +early (e07, e31, e35) | **30** | 0 | 73–82, 323–332, 373–382 | **No** |
| F-D3 | Read-back trace log (e85, e88) | observability log +early (e11, e16) | 20 | 0 | 113–122, 163–172 | **No** |
| F-D4 | Read-back trace log (e64, e79) | snapshot bundle +late_only (e22, e45) | 20 | 0 | 233–242, 483–492 | **No** (post-audit trap) |
| F-E | Observability log (e25 cloud_runtime / AWS-style; e34 structured JSON; e39 syslog; e48 pipe-delimited) | — | 40 | 4 (12, 15, 17, 19) | 263–272, 363–372, 413–422, 513–522 | **Semantics yes, syntax no**: samples use only logfmt (e01); each evaluation block uses a different syntax |
| F-F | Ledger JSON (e13 "connector_configurations", e36 "customer_export_manifest", e50 security_audit_ledger) | — | 21 | 0 | 133–142, 383–392, 533 | **No**, but same row schema as the F-H handover ledger |
| F-G | Ledger lines in logs (e08 search_index_diagnostic, e41 notification_delivery) | — | 20 | 0 | 83–92, 433–442 | **No**, same row schema as F-H |
| F-H | Handover text ledger (e03) | — | 6 | 3 (11, 13, 14) | 36–38, 40–42 | Yes |
| F-I | Snapshot bundle (e45) | — | 10 | 2 (31, 32 via e51) | 473–482 | Yes (tombstones) |
| F-J | **Multimodal**: screenshot / PDF + one `LINK_RESOURCE_IDENTITY` activity | various late companions | **11** (+ case_46 in F-D1) | 0 | 33, 34, 35, 39, 45, 50, 52, 53, 55, 61, 63 (+46) | **No**. Closest are the F1 handcrafted samples 06–08 |
| F-K | **Non-deletion requests** | e50 +late_only | **10** (9 with no anchor + case_533 anchored on e50) | 0 | 533–542 | **No**: the samples contain no NOT_APPLICABLE case |

Totals: 105 + 50 + 19 + 20 + 10 + 40 + 40 + 20 + 26 + 30 + 20 + 20 + 40 + 21 + 20 + 6 + 10 + 11 + 9 = **518** (case_533 is counted in F-F, case_46 in F-D1).

### F-A sub-families (replica topology)

| Topology | Eval | Sample | Notes |
|---|---|---|---|
| `customer_data_read_replica` | 47 | 16, 22 | covered |
| `customer_data_regional_replicas` (2 replicas: ap_south, eu_west) | 30 | — | **no sample**; a ghost row can sit in either replica |
| `customer_data_reporting_replica` | 20 | — | **no sample**; includes `retained_order_records` (legal holds) |
| `customer_data_failover_replica` (e52, `deleted_customer_references` + security only) | 8 | — | **no sample**; the tombstone variant of drift |
| `customer_accounts_failover_replica` (e54) | 0 | 05 (+01, 02, 04 by omission) | sample-only |

## Families with no sample (to be validated by custom cases)

| Family | Eval cases | Main risk |
|---|---|---|
| F-B1 inventory, no companion | 50 | Typed SQL tables; snapshot_scope / omission rules |
| F-C2 service export, no companion | 40 | Same schema as C1, but there are no e02-style links for audit records, so expect more U1-type gaps |
| F-D2 trace log + early bundle | 30 | Two-clock linking |
| F-A regional + reporting + data-failover | 58 | Drift detection across 2 replicas; legal-hold records; tombstones in replicas |
| F-E four new log syntaxes | 40 | Parser coverage (AWS escaped kv, pipe, syslog, JSON-per-line) |
| F-F + F-G ledgers | 41 | `record_type`/`result` vocabulary mapped directly; filenames are misleading |
| F-B3, F-C3, F-D3 two-clock companions | 60 | Inner-timestamp filtering |
| **F-B4, F-D4 post-audit traps** | **30** | **Must ignore companion records after `audit_at`**. Sample analog: only case_27's e02 L524 |
| F-J multimodal | 12 | OCR/vision; catalog-vs-screen conflicts (marketing_001); objects (object_19); legal-hold PDF |
| F-K non-deletion | 10 | Intent classification; output `[]` |

**Uncovered total: 369 / 518 evaluation cases (71 %)** fall into families or sub-families with no sample of the same anchor and companion shape:

58 (F-A) + 50 (F-B1) + 20 (F-B3) + 10 (F-B4) + 40 (F-C2) + 20 (F-C3) + 30 (F-D2) + 20 (F-D3) + 20 (F-D4) + 40 (F-E) + 21 (F-F) + 20 (F-G) + 11 (F-J) + 9 (F-K) = 369.

case_533 is counted once, in F-F. case_46 is counted in F-D1 and not here. If F-E is treated as covered (same meaning, different log syntax), the total is **329 (64 %)**.

## Resource composition (resource types linked to the subject in the anchor file)
Counts are per family, summed over cases. A resource type is counted once per case where it is linked.

| Family | Composition |
|---|---|
| F-A | audit_record 107, search 51, support 31, data_flow 23, analytics 13, account 13, backup 11, **tombstone 8, legal_hold 4**, export 4 |
| F-B1 | search 27, audit 15, support 13, data_flow 12, analytics 9, account 4, export 4, backup 4 |
| F-C1 | search 24, support 21, audit 18, data_flow 17, backup 8, analytics 8, account 6, export 4 |
| F-C2 | search 24, support 16, data_flow 12, audit 11, analytics 7, backup 5, export 4, account 3 |
| F-D2 | account 17, audit 15, support 12, data_flow 11, search 10, backup 7, analytics 3 |
| F-E | account 44, audit 44, search 30, support 20, data_flow 15, analytics 10, backup 4, export 2 |
| F-F | account 21, audit 21, search 14, support 8, data_flow 7, analytics 5, backup 2, export 1, legal_hold 1 |
| F-G | account 20, audit 20, search 11, data_flow 7, support 7, analytics 6, backup 2 |

## Caveats
- **Screenshots**: OCR corrupts identifiers, so image hits are under-counted. Three F-J cases have **no anchor at all**:
  - case_55: the backup console OCR reads `areedt433` for `areed1433`;
  - case_61: the object-storage screen shows no identifier;
  - case_63: the marketing console shows a different person (A8).
  These need vision extraction plus the `LINK_RESOURCE_IDENTITY` activity.
- Companion classification uses line timestamps (logs) and inner snapshot `captured_at` (bundles). SQL and JSON exports with only a manifest time are treated as file-level.
- Families are defined by evidence shape only. The composition of resources and violations inside a family still varies per case.
