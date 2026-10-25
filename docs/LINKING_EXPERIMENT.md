# Linking Experiment — can activity adjacency or reported_at offset recover U1 links?

Status: reviewed 2026-10-24. **Decision recorded as SPEC §9 L1: M6 first, then M9 for ties, always emit the account step with low-confidence flags, switchable with `LINK_FALLBACK=guess|omit`.** The recommendation below is kept for the record.
Scratch scripts are in the session scratchpad, outside `code/`: `link_truth.py`, `link_experiment.py` … `link_experiment5.py`.

## Question
Section 5 of SAMPLE_DERIVATION lists **25 U1 steps**: 18 primary accounts and 7 anonymized analytics records. For these, the gold resource is tied to the subject only by a `system_activity` row, never by evidence. The question is whether a **deterministic** rule over activity rows picks the correct candidate.

## Data
- **Candidates (accounts)**: `DELETE_RECORD` rows whose details say "submitted primary profile" (the generated template) or "Target resource: account_" (the handcrafted template). 
- **Candidates (analytics)**: `ANONYMIZE_RECORD` rows.
- **Ground truth**
  - *Samples*: gold account per case (32 cases, including the 18 U1).
  - *Held-out*: 238 evaluation cases where evidence links exactly one primary-profile account to the subject (§`link_truth.py`), with that account's row within 1 day of `reported_at`. No gold was used. Every evaluation family is held out, because all parameters are fit on the samples only.
  - *Analytics held-out*: 73 evaluation cases where evidence links exactly one analytics record that has an ANONYMIZE row.
- **Parameters fit on the samples**: the account row lands **−8 to +74 min before `reported_at`** (median 53 min). Analytics rows land −114 to +29 min.

Window size: in the held-out set, that window contains **1 candidate in only 19/238 cases**. It usually contains 2–6 (3 is the mode, 128 cases). Many cases share a `reported_at` (e.g. samples {24,25,26}, {27,28,29}, {13,18,20}, {14,31,32}).

## Methods
| Id | Rule |
|---|---|
| M1 | nearest to the learned offset (53 min before `reported_at`) |
| M2 | pick only if exactly one candidate is in the learned window |
| M4 | **activity_id adjacency**: highest-id candidate preceding the case's first evidence-linked non-account activity |
| M5 | M4 + candidate within 160 min of that activity |
| M6 | **elimination + unique**: drop candidates whose account is evidence-linked to *another* case; pick only if exactly one remains |
| M7 | elimination + nearest offset |
| M8 | elimination + activity_id adjacency |
| M9 | elimination + **rank alignment**: within a group of cases with overlapping candidates, sort cases by their first linked activity id and candidates by activity id, then pair them in order |
| MA | analytics: elimination + unique ANONYMIZE row in the learned window |

## Results (ok / wrong / abstain)
| Method | Samples (32) | Sample U1 accounts (18) | Held-out eval (238) |
|---|---|---|---|
| M1 offset nearest | 16 / 16 / 0 | 9 / 9 / 0 | 108 / 130 / 0 |
| M2 offset unique | 8 / 0 / 24 | 6 / 0 / 12 | 18 / 1 / 219 |
| M4 id adjacency | 9 / 15 / 8 | 5 / 7 / 6 | 53 / 122 / 63 |
| M5 id adjacency + time | 9 / 15 / 8 | 5 / 7 / 6 | 53 / 122 / 63 |
| **M6 elimination + unique** | **13 / 0 / 19** | **6 / 0 / 12** | **138 / 1 / 99** |
| M7 elimination + nearest | 20 / 10 / 2 | 10 / 7 / 1 | 203 / 30 / 5 |
| M8 elimination + adjacency | 14 / 9 / 9 | 7 / 5 / 6 | 113 / 57 / 68 |
| M9 elimination + rank (by first activity id) | 20 / 6 / 6 | 12 / 3 / 3 | 159 / 32 / 47 |
| MA analytics (U1 = 7) | — | **2 / 0 / 5** (case_04, case_21) | 44 / 1 / 28 |

Per held-out family:
- M6 makes no errors in 16 of 17 families. Its one error is case_326 (F-D2).
- M6 abstains on every case in F-A replica-drift (11/11), F-B1 inventory, F-C2 and F-I.
- M7 and M9 make errors in most families.

## Findings
1. **Neither activity_id adjacency (M4/M5) nor offset from `reported_at` (M1/M2) is deterministic.**
   - Adjacency is wrong more often than right (held-out: 53 ok vs 122 wrong). The case's own workflow rows are interleaved with other cases' rows, and the account row is 1–15 ids before the case's next row in a cluster of 3–6 accounts.
   - The offset is spread over ~80 min, while competing accounts are 2–20 min apart.
2. **Only M6 (elimination + unique) is precise: 151/152 correct when it fires (99.3 %, samples + held-out).** It never errs on the samples.
   - Sample U1 resolved: case_01, 02, 07, 08, 10, 30.
   - Sample U1 not resolved: case_04 (no candidate in window), 18, 20, 21, 22, 23, 25, 27, 28, 29, 31, 32. These form **symmetric ties**: groups of cases that all lack evidence links compete for the same accounts, e.g. {18, 20} for {5521, 5037} and {27, 28, 29} for {3866, 5482, 7012}. No tested signal breaks these ties: ids, requested_at order and reported_at order were all tried.
3. **The held-out score overstates real coverage.** In held-out cases, competitors usually *do* have evidence links, which is what lets elimination work. On the **270 evaluation cases with no evidence-proven account** (the true U1 population), M6 fires on only **39 (14 %)**. Post-elimination candidate counts are {0: 13, 1: 39, 2: 74, 3: 120, ≥4: 24}. The 39 picks are all distinct accounts. Their accuracy cannot be measured.
   The largest U1 populations are F-A replica (94), F-B1 inventory (46), F-C2 export (37) and F-C1 export (32).
4. **Anonymized analytics**: MA resolves 2/7 sample U1 (case_04, case_21) and is 44/45 correct when it fires on held-out cases.

## Recommendation (for review)
- Adopt **M6 (and MA for analytics)** as the only activity-based linker. Mark it `low_confidence` in diagnostics, per A5. It never overrides evidence.
- **When M6 abstains, omit the step** (no account verification) rather than guess. Expected cost:
  - **samples**: 12 of 18 U1 account steps lost (already counted as unrecoverable);
  - **evaluation**: ~230 cases lose their `VERIFY_RECORD_ABSENT account_service …` step.
  M7 or M9 would recover more steps but add wrong steps (≈13–30 % error), which likely cost more than the omission under step-level scoring.
- Open question for you: given the scoring, should ties be broken with M9 (higher recall, ~16 % wrong on held-out) instead of omitting the step? A wrong `resource_id` probably scores like a missing step plus an extra step.

## T16 re-measurement (2026-10-25)
The experiment's rules were re-run through the pipeline (scorer on the samples; strict step P / R / F1) and two defects in the plan were found. Full numbers are in SPEC L1.

| Rule | picks (samples) | right | strict F1 (baseline .8818) |
|---|---|---|---|
| M6 | 7 | 7 | **.8851** |
| M6 + M9 + M7 + widen | 19 | 12 | .8691 |
| M6 + MX | 12 | 9 | .8725 |
| MA / MA-anchor | 7 / 4 | 2 / 1 | .8725 / .8775 |

- The M9 margin over M6 is 5 right of 12 (42%): below the break-even hit rate (about F1/2, 0.44) for an added step.
- **Elimination must be clock-safe.** 14 of 39 evaluation M6 picks relied on a competing case's link that exists only after this case's `audit_at`. Rebuilding the competitor's dossier at this `audit_at` leaves 24 picks on the 508 deletion cases.
- Mixing the sample and evaluation requests into one pool gives a wrong pick (case_31), so the pool is the run's request file only.
- Decision: M6 clock-safe for accounts, available as the opt-in `LINK_FALLBACK=guess`; the default is `omit` (no guessed step). MA is diagnostics only.
