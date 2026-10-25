# Assumptions audit

**Purpose.** Every place in the code or SPEC where we assume something instead of reading it from evidence, with the number of affected cases (E = the 518 evaluation cases, S = the 32 samples) and whether sample gold supports it.

**Date.** 2026-10-25 (measured at commit `f7b406c`, `--no-llm`, no `.env`).

**Rule.** Evidence beats assumption. If a sample gold contradicts an assumption, we change the assumption.

Counts are cases out of the 518 evaluation cases (E) and the 32 samples (S). Each variant flips one assumption and reruns the whole pipeline with `--no-llm`.

| # | Assumption | Where | E cases | Can change status? | Sample gold |
|---|---|---|---|---|---|
| 1 | Category taken from the ID prefix (`analytics_`, `support_`, `object_`) | `policy.categorize`, basis `prefix` | 3 (50, 53, 61) | **Status.** Treating prefix and system categories as unknown flips 3 cases (33, 35, 52). The 3 prefix cases are themselves `no_steps`. | S 1 (case_07), same as gold. Weak: one case. |
| 2 | Category taken from the system alone | `policy.categorize`, basis `system` | 8 (33, 35, 39, 45, 46, 52, 55, 63) | Status (same flip as #1) | S 1 (case_17), same as gold. Weak. |
| 3 | Intent: unsure means DELETION | `intent.prefilter` default | 0 default hits | None today | S 1 (case_26) goes DELETION by default, and gold agrees. |
| 4 | Intent: a negation overrides to NON_DELETION | `intent.prefilter` | 10 (533–542) | **Status**, to NOT_APPLICABLE | **Unconfirmed.** No sample is NON_DELETION. |
| 5 | Intent labels come from the prefilter only | CLI runs `--no-llm` | all 518 | Status | Samples pass on the prefilter alone. |
| 6 | U7: a PARTIAL recreation checkpoint reopens a resource | `dossier.RECREATION_RESULTS` | 0 | None. Reopen only fires when the latest state is ABSENT. | S 0. Untested. |
| 7 | Rule condition unknown, so assumed to hold (`condition_assumed`) | `policy._condition` | 420 cases, 567 events | **Status** for 46 cases, step-only for 369. | **Confirmed.** Flipping it drops sample status accuracy from 29 to 27 of 32 (case_21, case_25). |
| 8 | `pii_assumed_exposed` | `policy` | 0 | None | n/a |
| 9 | I2: `support_records` is a SUPPORT_MESSAGE (redact) | inventory SQL adapter | 13 | **Status** for 1 (case_297), step-only for 12. | **None.** S 0. |
| 10 | O-2: `attachment_pii_scan` is a SUPPORT_ATTACHMENT | observability log reader | 6 (71, 119, 263, 371, 419, 515) | Step-only. Flipping it changes steps in 7 cases. | None. S 0. |
| 11 | O-3: unseen outcomes (`index_query` miss, `restore_authorization` allow, export `available=false`) | observability log reader | 0 | None. The `result=miss` hits are `cache_lookup` noise. | None. |
| 12 | O-4: `contact_lookups` is a secondary identifier (delete) | observability log reader | 0 | None | **Confirmed once.** case_12 (`analytics_9914`), gold says DELETE. It is a single log line. |
| 13 | A-1: a ledger BACKUP marked ACTIVE means restorable | ledger readers | 3 (87, 387, 435) | **Status.** All 3 flip if it is wrong. | Confirmed by 1 sample (case_14, `backup_3870`, RESTORE_RISK in gold). |
| 14 | B1: bundle captures never prove absence (`complete=0`) | capture scope | 1 status (case_126), 9 step sets | **Status** | **Confirmed.** Flipping it drops sample accuracy from 29 to 25. |
| 15 | I1: inventory captures never prove absence (`complete=0`) | capture scope | 39 statuses, 11 step sets | **Status** | **Confirmed.** Flipping it drops sample accuracy from 29 to 28 (case_05). |
| 16 | TR2: the e61 support replica log is unclaimed | SPEC TR2 | 0 | None | S 1 (case_10). Gold has 4 steps from it that we omit. |
| 17 | C1: replica clock choice | SPEC C1 | 0 (`clock_choice_matters`=0) | None | n/a |
| 18 | A9: a retained record is missing and no operation is allowed, so no step | `policy` | 3 (34, 45, 55) | Unknown; on its own it gives no step and no status change. | None. S 0. |
| 19 | U6: emit steps for legal-hold records | `policy` | 8 (220, 261, 390, 432, 460, 473, 505, 511) | Dropping them changes no eval status. | **Contradicted.** Sample gold ignores the legal-register rows. |
| 20 | Absence by omission from a complete dump | Rule O, dossier | 9 | Status | S 1 (case_17) where gold has no step. Samples 01, 02, 04 support the citation. |
| 21 | Tombstone treatment | `policy` | 9 | Status | S 3 (17, 31, 32), same as gold. |
| 22 | `state_unknown`, so no step | `policy` | 4 (39, 46, 61, 63) | Status (these end up INCOMPLETE `[]`) | S 2 (07, 17), same as gold. |
| 23 | `no_steps`: INCOMPLETE with `[]` | `policy._status` | 9 (39, 45, 50, 53, 55, 61, 63, 126, 179) | **Status** | **Wrong in 3 of 7 samples.** Gold says COMPLETE for 01, 02, 04. |
| 24 | Unciteable evidence, so no step | `policy` | 120 cases (130 events) | Step-only, apart from 1 case that is COMPLETE | Not measured. |
| 25 | Rule O picks the latest capture when several qualify | citation rule | 58 (43 INCOMPLETE, 15 COMPLETE) | Step-only (citation) | Contradicted in case_04: gold cites e54, not the later e90. |
| 26 | L1: no account step by default (`omit`) | `linker`, `LINK_FALLBACK` | 272 get no account step | Step-only | Gold has an account step in all 32 samples. Guess mode scored 7 of 7 on the samples. |
| 27 | MA: no analytics step | `linker` | 290 records unresolved | Step-only | Rejected on the samples (F1 lower in every variant). |
| 28 | `a8_conflict`, name-only matches, `system_unknown` | dossier | 0 | None | n/a |

## What this table shows

- **Rows 7, 14, 15 and 13** (the unknown-condition default, B1, I1 and A-1) are the assumptions that move the most statuses. Samples support all four, but I1 is supported by only one case (case_05).
- **Statuses with no sample backing:**
  - **#9, I2:** case_297 flips on it.
  - **#4:** the negation override covers 533–542.
  - **#1/#2:** the prefix and system fallbacks, which flip 3 cases.
  - **#23:** `no_steps` is already wrong on 3 samples.
- **Step-only and untestable:** #10, #11, #12, #16 and #18.
- **Dead branches:** #6 (U7), #3, #8, #11, #17 and #28 affect 0 cases.

## Caveats

- The intent "unsure" count of 0 means the prefilter never fell through to the default on the 518. It does not show the default is right.
- The TR2 count of 0 is for evaluation cases. Its only effect is the 4 steps missing from sample case_10.
- #9, #10 and #18 are not covered by any sample, so only flipping the assumption could test them.
- **Rows 24, 26 and 27 were not measured against gold:** #24 has no sample comparison. #26 and #27 come from the earlier T16 numbers.

## Measured and rejected

- **Activity-link time window (2026-10-25, analysis only; relates to rows #1, #2 and #22 and SPEC MM3 e).** A window on activity links changes **0 of 518** cases and only removes the 3 `object_19` steps from sample 07. It cannot reach the `tombstone_17` step (direct match from e42) or the `account_6523` step (trace link from e67). A general "evidence older than the request does not link" rule is rejected because the F-J screenshots are captured 26 h before `audit_at` by design. Samples 07 and 17 stay as known open regressions.
- **Sample baseline after T17 (`--no-llm`, 32 samples).** `omit`: status 29/32 (.9062), step P/R/F1 .9684/.7927/.8718, sequence .3125. `guess`: .9062, .9688/.8031/.8782, .3438. The earlier T16 `omit` figure (F1 .8818, precision .9935) predates the T17 extras in cases 07 and 17.

## Open for T19

- **#19 U6:** legal-hold steps are contradicted by sample gold. Change the assumption.
- **#9 I2:** `support_records` as SUPPORT_MESSAGE flips the status of case_297 and has no sample support.
- **#24:** 120 unciteable cases, not measured against gold.
- **#4:** check the negation override (cases 533–542) after the final LLM run.
