# Evaluation report

Status: **draft written at T18** (2026-10-25). Sections marked `T20` are filled by the final cold run. Nothing in this file is estimated: every number was measured by the command given next to it, and a section says so when a number is not yet measured.

## 1. Scope and method

- **Pipeline under test:** `python3 code/main.py`, deterministic mode (`--no-llm`, link fallback `omit`, the default). Intent comes from the deterministic prefilter; screenshots and PDFs use the committed extractions in `code/extractions/`. The final run with the LLM intent classifier is a T20 item.
- **Three evidence sources, kept apart so that one cannot inflate another:**
  1. the 32 public samples scored against `dataset/sample_output.csv` (section 2);
  2. six hand-built custom scenarios run through the real CLI against a synthetic dataset (section 3);
  3. invariant checks over all 518 outputs (section 7).
- **Scoring** is `code/evaluation/scoring.py`. *Strict* compares all six step fields (`phase, system_id, resource_id, operation, state, evidence_id`). *Relaxed* ignores `evidence_id`. Steps match as a multiset ignoring `step_no`. Field accuracy is measured over steps aligned by `(phase, operation, resource_id)`, so those three fields are 1.0 by construction. Sequence accuracy is an exact match of the whole array.
- **Rule used throughout:** evidence beats assumption. If sample gold contradicts an assumption, the assumption changes.

## 2. Accuracy on the 32 public samples

Command: samples copied into a dataset dir as `deletion_requests.csv`, then `python3 code/main.py --dataset <dir> --no-llm --link-fallback <mode>` and `python3 code/evaluation/main.py --pred <out>`. Measured 2026-10-25 after T17.

| Metric (strict = relaxed on every row) | `omit` (default) | `guess` (opt-in) |
|---|---|---|
| Case status accuracy | 29/32 = **0.9062** | 29/32 = 0.9062 |
| Step precision | 0.9684 | 0.9688 |
| Step recall | 0.7927 | 0.8031 |
| Step F1 | **0.8718** | 0.8782 |
| Field accuracy: phase, system_id, resource_id, operation, state, evidence_id | 1.0000 each | 1.0000 each |
| Sequence accuracy | **0.3125** (10/32 cases exact) | 0.3438 (11/32) |
| Steps: gold / predicted / matched | 193 / 158 / 153 | 193 / 160 / 155 |
| Status confusion (gold→pred) | COMPLETE→COMPLETE 7, COMPLETE→INCOMPLETE 3, INCOMPLETE→INCOMPLETE 22 | same |
| Cases with any difference | 22 | 21 |

Reading the table:

- **Field accuracy is 1.0 on every aligned step, so strict equals relaxed.** Every error is a *missing* step (40 of the 193 gold steps) or an *extra* step (5), never a wrong field on a step we emit. In particular no citation (`evidence_id`) we emit is wrong on the samples.
- **Only three statuses are wrong**: cases 01, 02 and 04 (gold COMPLETE, predicted INCOMPLETE). There is no case where we say COMPLETE and gold says INCOMPLETE.
- **Sequence accuracy is low because one missing account step breaks the exact match**, not because order is wrong: ordering follows the gold in every case where the steps are present (the ordering rule had 0 deviations on the 32 answers).

Where the 22 differing cases (`omit`) come from. The 40 missing steps are 19 account checks, 7 analytics checks and 14 others; the 5 extra steps are in cases 07 and 17:

| Cause | Cases | Effect |
|---|---|---|
| No account link in evidence and L1 `omit` (default). `guess` makes 7 picks, all 7 right, but only case 30 gains a scored step here; 21, 22, 23, 25, 27, 28, 29, 31, 32 stay unresolved or tied | 21, 22, 23, 25, 27, 28, 29, 30, 31, 32 | 1 missing `VERIFY_RECORD_ABSENT account_*` each (10 steps) |
| Analytics step not emitted (MA is diagnostics only; as a step it lowered F1 in every variant) | 03, 04, 21, 25, 26, 27, 28 | 1 missing `VERIFY_RECORD_ANONYMIZED` each (7 steps) |
| `no_steps` gives INCOMPLETE with `[]` where gold is COMPLETE (assumption row 23) | 01, 02, 04 | **wrong status**; the account check is also missing (3 steps). A guess is not added to a `no_steps` case |
| Handcrafted text files not read (e56, e57, e59, e61; TR2/MM2) | 06, 08, 10 | a finding, a delete and a verify each, plus the account check (12 steps) |
| `support_001` printed only in `evidence_58`, which no other file names, so the known-id rule drops it (MM3 c) | 07 | 3 steps plus the account check; also **3 extra** `object_19` steps |
| Ids that appear in no participant file | 18, 20 | the account and `audit_record_*` checks (4 steps) |
| T17 over-linking | 17 | **2 extra** (`account_6523`, `tombstone_17`) |

## 3. Custom evaluation cases (CC1 to CC6)

Run: `python3 code/evaluation/main.py --custom`  ·  tests: `python3 -m unittest tests.test_custom_cases` (from `code/`).

**How they are built.** `code/evaluation/custom_cases/` generates a complete synthetic dataset directory (users, audit log, activity, rules, manifest, evidence, requests) and the expected answers from code: `cases.py` (data and expected literals), `render.py` (log/JSON renderers), `build.py` (writer), `runner.py` (CLI run and scoring). The dataset is self-contained, so the cases run from `code.zip` without `dataset/`. Nothing is copied from `dataset/` (a test searches the real files for every id, email and sentence), no organizer file is read, and the generator is deterministic (a test builds twice and compares bytes).

**How the expected answers were made.** Each expected step list is a literal in `cases.py`, derived from AGENTS.md section 6 (operation→state table 6.4, phase and operation order 6.3/6.5, citation rules 6.6) and the ordering all 32 sample answers follow, and written **before the first run**. They are never produced by the pipeline; a test confirms they are themselves valid under the output invariants. Scoring uses the same scorer as section 2, strict.

**Negative controls.** For each case with evidence, the generator can remove the one decisive line. The same pipeline must then give the *other* answer, and the test suite checks that. This is what shows a case is sensitive to the behaviour it names rather than passing by accident.

| Case | Row | Behaviour and why it was picked | Expected | Actual (first run) | Control (hazard removed) |
|---|---|---|---|---|---|
| **CC1** | case_901 | A read that *looks* like absence but is a failure: `rows=0 http_status=503` after a real `rows=1 200`, plus a `_failed` event, a "deletion SUCCESS" activity claim, reported status COMPLETE, and a request made under the **old** email. Strongest area, and the commonest trap in the trace-log families. | INCOMPLETE: `REPORT_REMAINING_RECORD` account_9101 (evidence_901), `DELETE_RECORD`, `VERIFY_RECORD_ABSENT` | match | 503 replaced by a real 404 → COMPLETE |
| **CC2a** | case_902 | One trace whose started line names subject A and whose completed line names subject B (A8: a direct identifier beats a trace join). A precision trap: SPEC TR1 notes no such trace exists in the data, so only a synthetic case exercises it. | COMPLETE: only `VERIFY_RECORD_ABSENT` account_9202 (evidence_902); nothing for B's record. Must log `a8_conflict` | match | completed line names A → A's record is linked, case becomes INCOMPLETE |
| **CC2b** | case_903 | Same file, subject B. | INCOMPLETE: account_9201 finding (evidence_902), delete, verify | match | (shares CC2's control) |
| **CC3a** | case_904 | Clock gate (U3): a bundle captured **after** `audit_at` shows the account present again. It must neither change state nor be cited. Biggest blast radius if wrong (30 trap-family cases). | COMPLETE: `VERIFY_RECORD_ABSENT` account_9301 and `VERIFY_RECORD_PRESENT` audit_record_9302, both citing only evidence_904. Dossier logs `late_records_excluded = 1` | match | bundle moved before `audit_at` → INCOMPLETE |
| **CC3b** | case_905 | The **same user** at a later `audit_at`, where the bundle now counts. Shows the clock really moves the answer. | INCOMPLETE: finding cites evidence_905; delete; verify absent; verify audit record present | match | n/a |
| **CC4** | case_906 | A resource recreated after it was seen deleted, with a trap: the file with the **later manifest** (evidence_906, ABSENT) holds the **older** read (07:30), and the earlier file (evidence_907) holds the newer read (08:50, account present). "Latest file wins" gives COMPLETE; "latest record wins" gives INCOMPLETE. | INCOMPLETE: finding cites evidence_907 (the file that proves the state), delete, verify absent | match | newer read shows 404 → COMPLETE |
| **CC5** | case_907 | Retention and ordering: an audit record inside `retention_until` must get `VERIFY_RECORD_PRESENT`, never a delete; an active connector must be disabled before records are deleted. A destructive wrong answer is possible here. | INCOMPLETE, 10 steps: 3 findings (account, search, `REPORT_ACTIVE_DATA_FLOW`); `DISABLE_DATA_FLOW` before the two `DELETE_RECORD`; verification absent ×2, `VERIFY_RECORD_PRESENT` audit_record_9503, `VERIFY_DATA_FLOW_DISABLED` | match | retention already expired → no audit step (rest unchanged) |
| **CC6** | case_908 | A request full of deletion words that is not a deletion ("keep my account … do not delete anything"). The only status no public sample proves (assumption row 4). | NOT_APPLICABLE, `[]` | match | n/a (status only) |

Result of the first run: **8 of 8 rows match strictly, 0 invariant violations, exit 0**, and no expected answer had to be changed after seeing the output. Per case, strict status accuracy and sequence accuracy are 1.0.

What these cases do **not** show, and why:

- They found **no new pipeline failure**. They confirm that the behaviours listed hold on synthetic inputs, and the controls confirm each is sensitive to its trigger. They do not measure accuracy on the real distribution.
- **Recreation known only from an activity** (a `final checkpoint` or `repopulated` row with no file showing the record back) is deliberately **not** a scored case. AGENTS.md 6.6 calls activity rows claims, while SPEC §7 and DESIGN §2.5 let an explicit recreation event reopen a resource (assumption U7, no sample gold backs it). Encoding either expectation as truth would be a guess, so CC4 uses a recreation that *evidence* establishes, and the activity row is only corroboration.
- CC2b assumes A8's rule (a direct identifier match beats a join) as stated in SPEC; it is a design decision, not sample gold.
- The scenarios use trace logs and one snapshot bundle. They do not exercise replica SQL, ledgers or multimodal inputs; those adapters are covered by the 32 samples and by `tests/`.

## 4. Failures found and why

Known, open, and all reproducible from the commands above.

| # | Failure | Cause | Status |
|---|---|---|---|
| F1 | Cases 01, 02, 04: predicted INCOMPLETE, gold COMPLETE (the three status errors) | These cases have no linked resource other than an absence proven by omission; the pipeline's `no_steps` rule yields INCOMPLETE `[]`, while gold says COMPLETE | Open; assumption row 23, T19 |
| F2 | Sample 07: 3 extra `object_19` steps; `support_001` steps missing | `evidence_58` prints `support_001`, which no activity, identity or other file names, so the known-id rule (MM1) drops it; the `object_19` screenshot (e46) was captured months before the request and still links | Open regression from T17; SPEC MM3 (c), (e) |
| F3 | Sample 17: extra `VERIFY_RECORD_ABSENT account_6523` and `VERIFY_RECORD_REDACTED tombstone_17` | `account_6523` arrives by a trace link in e67 (an earlier case of the same user); `tombstone_17` by a direct identifier match in e42 | Open regression from T17 |
| F4 | Samples 06, 08, 10: whole finding chains missing | The four handcrafted text files (e56, e57, e59, e61) are not read; none of the 518 evaluation cases links to them | Deferred (SPEC MM2, TR2) |
| F5 | Samples 18, 20: account and `audit_record_*` steps missing | The ids appear in no participant file | Accepted loss (SPEC A3/A4) |
| F6 | 19 account steps missing (one in each of 19 cases; 13 of those cases also have another cause, 6 are account-only) and 7 analytics `VERIFY` steps missing | L1 `omit` and MA diagnostics-only, chosen because guessed steps cannot be verified on the 518 | Decision recorded. `guess` mode makes 7 picks, all right, but only 2 add a scored step (cases 07 and 30), because the other 5 sit in `no_steps` or deferred-file cases; F1 .8718 → .8782 |

**Measured and rejected fix for F2/F3 (2026-10-25).** An activity-link time window changes **0 of 518** cases and only removes the 3 `object_19` steps. It cannot reach `tombstone_17` or `account_6523`. A general "evidence older than the request does not link" rule is rejected, because the F-J screenshots are captured 26 hours before `audit_at` by design. Samples 07 and 17 stay as known open regressions.

## 5. Improvements made, with the measurement behind each

| Change | Measurement |
|---|---|
| **M6 clock-safe account linker (L1)** replacing the rank/offset chain | Samples: 7 picks, 7 right, 0 wrong (the earlier chain: 19 picks, 12 right, 7 wrong). 518: the first M6 made 39 picks, 14 of which eliminated a competitor with a link that exists only after that case's `audit_at`; clock-safe elimination keeps all 7 sample picks and gives 24 picks (the rank chain would have added 264 steps). `omit` is the default because the 24 cannot be verified. |
| **MA analytics as a step: rejected** | 7 unique picks, 2 right, 5 wrong; step F1 fell to .8725 (baseline .8818 at that time). Kept as diagnostics only. |
| **MX (unique anchor candidate): rejected** | 12 picks, 9 right, 3 wrong; two accounts each picked for two cases; F1 .8725. |
| **Bundle (B1) and inventory (I1) captures never prove absence (`complete=0`)** | Flipping B1 back drops sample status accuracy 29 → 25; flipping I1 drops it 29 → 28 (case_05). |
| **Failed lookups set no state** (`_failed`, `http_status` ≥ 500, `result` error/timeout) | Rules O1/TR1; the 97 evaluation cases anchored on trace logs were re-run when the adapter landed; CC1 now guards it. |
| **Ledger readers (T15)** | Samples 11, 13, 14 match except L1 steps; 46 evaluation cases gain steps (9 INCOMPLETE→COMPLETE, none the other way). |
| **Screenshot model: Opus over Sonnet (T17 bake-off, MM3)** | 15 files, two reads each: Opus $0.67, 11/15 agreed before the agreement rule and 15/15 after; Sonnet $0.25 but wrong or disagreeing on 4 files (e19, e37, e58, e60). |
| **Condition-unknown defaults (row 7)** | Flipping drops sample status accuracy 29 → 27 (cases 21, 25): confirms the default. |

## 6. Assumptions table, summarised

Full audit: 28 rows, `docs/ASSUMPTIONS.md` (measured at commit `f7b406c`, `--no-llm`). Counts are evaluation cases (E, of 518) and samples (S, of 32).

| Group | Rows | Reading |
|---|---|---|
| **Move statuses, supported by sample gold** | 7 (condition unknown → assumed to hold: 46 E), 13 (A-1 ledger backup `ACTIVE` = restorable: 3 E), 14 (B1: 1 E status), 15 (I1: 39 E statuses) | Flipping any one costs 1–4 sample statuses |
| **Move statuses, no sample support** | 4 (negation override: 10 E, cases 533–542), 9 (I2 `support_records`: 1 E status, 12 step-only), 1/2 (prefix/system category: 3 E) | Weakest evidence; CC6 covers row 4 on a synthetic input only |
| **Contradicted by sample gold** | 19 (U6 legal-hold steps: 8 E), 23 (`no_steps`: 9 E, wrong on 3 of 7 samples), 25 (Rule O latest, contradicted by case_04) | Listed for T19 |
| **Step-only, untestable** | 10 (O-2), 11, 12, 16, 18 | No sample touches them |
| **Dead branches (0 evaluation cases)** | 3, 6 (U7), 8, 11, 17, 28 | No effect today |
| **Not measured against gold** | 24 (unciteable: 120 E), 26, 27 | |

## 7. Invariants and determinism (T20)

To be filled by the final cold run: 518 rows, unique and in numeric order; `python3 code/evaluation/main.py --invariants` result; byte-identical `output.csv` across two runs; counts of `no_steps`, `retained_missing`, `link_unresolved`; intent agreement rate between the LLM and the prefilter. `<!-- T20 -->`

## 8. Cost summary (T20)

T18 made **no LLM calls**. The cost, token and call tables come from `code/evaluation/usage_report.md` after the final `--cold` run: model providers and names, call counts, input and output tokens, total and average tokens per request (÷ 518), estimated total and per-request cost, per model and overall. `<!-- T20: paste from usage_report.md -->`
