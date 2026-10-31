# Ghost Data Audit Agent

*When someone asks for their data to be deleted, how do you know it's really gone? This project tries to answer that with evidence, not a confident guess.*

![HackerRank Orchestrate certificate: Rounak Agrawal, rank 127 of 4,094](docs/certificate.png)

I built this on my own for **HackerRank Orchestrate, October 2026**, for the challenge "Ghost Data Still Exists". I finished **#127 out of 4,094 participants (top 3.1%)**.

---

## Why this problem is hard

A deletion system can say "Complete" and still leave personal data behind. A copy might be sitting in a database replica, a support attachment, an export, a search index or a backup. Or a sync job might quietly restore a record after it was deleted.

The challenge gave me 518 deletion requests and a messy evidence pack: SQL snapshots, JSON exports, several kinds of logs, ledgers, screenshots and PDFs. For each request, the agent had to decide whether deletion was really asked for, work out what still existed at the audit time, apply the right policy, and write an ordered plan with evidence behind every finding.

I didn't want a chatbot that sounds sure of itself. I wanted something closer to an auditor who shows their working: every conclusion points to a record, a timestamp and a rule. And when the evidence isn't enough, the agent should say less instead of guessing.

## At a glance

| | |
|---|---|
| Challenge | HackerRank Orchestrate, October 2026 |
| Result | **#127 / 4,094 (top 3.1%)** |
| Requests processed | **518** |
| Evidence readers | **8 adapters** |
| Possible outcomes | `COMPLETE`, `INCOMPLETE`, `NOT_APPLICABLE` |
| Language | Python 3 (standard library for the core pipeline) |
| Optional model provider | Anthropic API |
| Output | `output.csv`, one row per request |

## What it does for each request

1. **Checks that deletion was really requested.** Asking to update an email, download data or change marketing preferences isn't a deletion request.
2. **Works out who the person is.** It uses current and past emails, phone numbers, email hashes and user IDs.
3. **Reads the evidence and links records to that person.** A name alone is never enough.
4. **Rebuilds the state of each resource at that request's `audit_at` time.**
5. **Applies the rule from `deletion_rules.csv`.** For example: delete, redact, anonymise, retain, block restore, or disable a data flow.
6. **Writes findings, fixes and checks in the required order,** with evidence references wherever a finding can be backed up.

There are three possible statuses:

- **`COMPLETE`**: the required end states are confirmed by evidence the agent is allowed to use.
- **`INCOMPLETE`**: the agent found a policy violation it can cite, a deletion requirement it couldn't verify, or no solid evidence that deletion finished.
- **`NOT_APPLICABLE`**: the request isn't a deletion request, so `steps_json` is `[]`.

## How it works

I split the pipeline into small stages. The evidence is cleaned up and stored once, and each request is then checked against that shared set of facts.

```text
dataset/
   │
   ▼
loaders ──► 8 evidence adapters ──► in-memory SQLite fact store
                                          │
request ──► intent ──► identity resolution │
                         │                │
                         ▼                ▼
                  time-aware dossier at audit_at
                              │
                              ▼
                       policy engine
                              │
                              ▼
                findings + remediation + verification
                              │
                              ▼
                          output.csv
                              │
                              ▼
                    evaluation / invariants
```

### 1. Read different evidence formats the same way

Each adapter turns one kind of source into a common set of observations. They recognise files by their structure, not just by file name.

| Adapter | Evidence it handles |
|---|---|
| `replica_sql` | SQL replica snapshots |
| `snapshot_bundle` | Snapshot bundles |
| `service_export` | Service exports and JSON bundles |
| `inventory_sql` | Inventory SQL records |
| `observability_log` | Observability logs |
| `trace_log` | Trace and request logs |
| Ledger readers | JSON, log and plain-text ledgers |
| `multimodal` | Screenshots and PDFs |

An adapter pulls out things like the resource, its state, the timestamp on the record and any identifiers that might connect it to a person. It writes those facts into a shared in-memory SQLite store. It never makes the final deletion decision.

### 2. Match people to records using evidence

The email in a deletion request is checked against the current identity snapshot and the history of identity changes. That gives the agent a set of current and former identifiers to look for in older systems.

Matching uses exact identifiers and supported joins. A name on its own can't link a resource to someone. If the evidence doesn't support an account link, the default is to leave the account step out instead of making up a connection.

### 3. Keep track of two clocks

This is the design decision I care about most. There are two different times to think about:

- The **timestamp on the record** (`record_at`) decides whether an observation can be used to rebuild the state and link a resource at `audit_at`.
- The **capture time of the evidence file** in the manifest decides whether that file can be cited for the case.

The difference matters when a file was collected later but contains an older record. The older record can still help rebuild what was true back then, but a file captured after `audit_at` can't be cited for that audit. And a later workflow status is never treated as proof of what was true earlier.

### 4. Apply policy in plain code

For each linked resource, the policy engine finds its category and the required end state from `deletion_rules.csv`. Depending on the rule, that could mean deleting a record, redacting identifiers, anonymising it, keeping it, keeping a permitted tombstone, blocking a restore or disabling an active data flow.

Activity logs and reported results are treated as claims, not proof of the current state. Explicit recreation events are a special case. Otherwise, the latest usable observation of a resource is compared with the required state. A complete snapshot can prove something is absent because it's missing from the snapshot, but a partial export or an inventory can't, so those are handled separately.

### 5. Write steps someone can audit

The step writer produces three phases, in this order:

1. `FINDING`: what the evidence shows.
2. `REMEDIATION`: what needs to change.
3. `VERIFICATION`: what to check afterwards.

Findings cite their evidence. Fixes and future checks in an `INCOMPLETE` case use an empty `evidence_id`, because they haven't happened yet. Steps follow the order the challenge requires, and the numbering starts at 1 with no gaps.

## Where the LLM is used, and where it isn't

I kept the model away from every decision that has to be auditable.

- **Request intent:** the final run used `claude-haiku-5-5` to help decide whether a request was really asking for deletion. A simple word-based prefilter catches explicit "do not delete" wording, and that always wins. If the model's reply is missing or fails validation, the prefilter's answer is used.
- **Screenshots and PDFs:** I read these once during development with a multimodal model. The agreed facts (IDs and hashes) are saved in `code/extractions/`, so a normal run reuses them and makes no image calls.
- **Everything else:** identity linking, resource state, policy, status, step building, ordering and citations are all code. The model never decides whether deletion is complete, and it never makes up a citation.

There's an optional category-disambiguation mode behind `CATEGORY_LLM`, but it's **off by default**. The pipeline also runs with no API key, or with `--no-llm`, and then intent comes from the prefilter alone.

## Results

I kept the public samples, the synthetic tests and the full-dataset checks separate, because each one answers a different question.

### The 32 public samples

Scored in deterministic mode (`--no-llm`, with the default `LINK_FALLBACK=omit`).

| Metric | Result |
|---|---:|
| Deletion-status accuracy | **29 / 32 (90.62%)** |
| Step precision | **0.9684** |
| Step recall | **0.7927** |
| Step F1 | **0.8718** |
| Field accuracy on matched steps | **1.0000** for every field measured |
| Exact sequence match | **10 / 32 (31.25%)** |

The gaps are written up in the evaluation report instead of being hidden. Most of the missing steps come from links or evidence the pipeline chooses not to guess, or from file formats it doesn't fully cover yet. The three status mismatches are in the report as well.

### My own test cases

I wrote six custom scenarios (eight output rows) from small synthetic datasets and ran them through the real command line. **All 8 rows matched what I expected**, with no invariant violations. They're good regression checks, but they don't tell you how the agent performs on the challenge's full data.

### The full 518-request run

| Outcome | Requests |
|---|---:|
| `COMPLETE` | 152 |
| `INCOMPLETE` | 356 |
| `NOT_APPLICABLE` | 10 |
| **Total** | **518** |

Every output check passed: 518 unique requests in case order, the right header, valid step numbering and ordering, and citations that respect the audit-time rules.

A cold run, a rerun with a warm cache and a `--no-llm` run all gave byte-identical `output.csv` files. The final model run didn't change a single status or step compared with the deterministic run.

For methods, error analysis and assumptions, see [`code/evaluation/evaluation_report.md`](code/evaluation/evaluation_report.md).

## LLM usage and cost

These numbers come from the usage report in the repo. I counted development experiments and the final run separately.

| Run | Model usage | Recorded cost |
|---|---|---:|
| Final 518-request run | `claude-haiku-5-5`: 381 calls and 137 cache hits; 181,661 input tokens, 19,972 output tokens | **$0.0282** |
| Development experiments | Haiku intent tests, plus Opus and Sonnet screenshot and PDF comparison runs | **$0.9438** |
| **Total recorded** | Final run + development | **$0.9720** |

For the final run that works out to about **389.3 tokens** and **$0.000054** per request. The final run made no screenshot or PDF calls, because the extracted facts were already in `code/extractions/`.

These are estimates from my run logs and the prices recorded in [`code/evaluation/usage_report.md`](code/evaluation/usage_report.md). A future run won't necessarily cost exactly the same.

## Run it yourself

### What you need

- Python 3. The standard library is enough for the core pipeline.
- To make live model calls: an Anthropic API key and the pinned SDK from `requirements.txt`.
- `pdftotext` (from Poppler), only if you want to re-run PDF text extraction. A normal run reuses the saved extractions.

### 1. Clone it

```bash
git clone https://github.com/Ragrawal2004/hackerrank-orchestrate-october26.git
cd hackerrank-orchestrate-october26
```

### 2. Run it without an API key

This uses the prefilter for intent and makes no LLM calls:

```bash
python3 code/main.py --no-llm
```

It reads the files in `dataset/` and writes `output.csv` in the repository root.

### 3. Run it with Anthropic intent classification (optional)

Set up a virtual environment and install the pinned SDK:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

Then set your key in the shell, or in a local `.env` file that you never commit:

```bash
export ANTHROPIC_API_KEY="your_api_key"
python3 code/main.py
```

Never commit `.env`, API keys or other credentials. `.env.example` shows the setting names.

### Handy commands

```bash
# Full dataset, prefilter only
python3 code/main.py --no-llm

# Full dataset, with the LLM intent classifier
python3 code/main.py

# A few cases, written to a separate file
python3 code/main.py --out /tmp/selected.csv --cases case_33,case_34

# Unit and integration tests
python3 -m unittest discover -s code/tests -t code -v

# Check the output format and the evidence-time rules
python3 code/evaluation/main.py --invariants --pred output.csv

# Run the six custom scenarios
python3 code/evaluation/main.py --custom
```

To score the 32 public samples, copy the dataset to a temporary folder, swap in `sample_deletion_requests.csv`, run the pipeline on that folder, and compare against the sample answers:

```bash
mkdir -p /tmp/ghost-samples
cp -R dataset/. /tmp/ghost-samples/
cp dataset/sample_deletion_requests.csv /tmp/ghost-samples/deletion_requests.csv
python3 code/main.py --dataset /tmp/ghost-samples \
  --out /tmp/ghost-samples/predictions.csv --no-llm
python3 code/evaluation/main.py \
  --pred /tmp/ghost-samples/predictions.csv \
  --gold dataset/sample_output.csv
```

## Configuration

Command-line flags win over environment variables.

| Setting | What it does | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Turns on live Anthropic API calls | Not set |
| `LINK_FALLBACK` | Leave out or guess low-confidence account links | `omit` |
| `CATEGORY_LLM` | Optional LLM category disambiguation | `off` |
| `GHOST_INTENT_MODEL` | Model for request intent | `claude-haiku-5-5` |
| `GHOST_CATEGORY_MODEL` | Model for the optional category step | `claude-haiku-5-5` |
| `GHOST_SCREENSHOT_MODEL` | Model for screenshot and PDF extraction | `claude-opus-5-5` |
| `GHOST_RUNS_DIR` | Where diagnostics, the usage log and the response cache go | `runs/` |
| `GHOST_EXTRACTIONS_DIR` | Where the saved screenshot and PDF facts live | `code/extractions/` |

Useful flags:

- `--dataset DIR` to use a different dataset folder.
- `--out FILE` to write the output somewhere else.
- `--cases IDS` to run a comma-separated list of case IDs.
- `--link-fallback omit|guess` to change how uncertain account links are handled.
- `--category-llm off|on` to switch the optional category step.
- `--no-llm` to turn off live model calls.
- `--cold` to ignore cached LLM responses.

## Repository layout

```text
.
├── code/
│   ├── ghost/                 # Pipeline, adapters, identity, policy, LLM gateway
│   ├── tests/                 # Unit and end-to-end tests
│   ├── evaluation/            # Scoring, invariants, custom cases, reports
│   ├── extractions/           # Agreed facts from screenshots and PDFs
│   └── prompts/               # Prompts for intent and multimodal extraction
├── dataset/                   # Challenge inputs and evidence files
├── docs/
│   ├── SPEC.md                # How I read the challenge contract
│   ├── DESIGN.md              # Design decisions and data model
│   └── ASSUMPTIONS.md         # Assumptions, their impact, open questions
├── ARCHITECTURE.md            # Short architecture and module map
├── AGENTS.md                  # Instructions for AI coding agents
├── CLAUDE.md                  # Claude Code project notes
├── problem_statement.md       # The challenge statement
├── requirements.txt           # Pinned optional Anthropic SDK
└── README.md
```

## Known limitations

I chose cautious linking and evidence rules, and the price of that is some gaps. The main ones are written up in [`docs/ASSUMPTIONS.md`](docs/ASSUMPTIONS.md) and the evaluation report:

- With the default `LINK_FALLBACK=omit`, **272 cases get no account verification step** when the evidence doesn't give a safe account link. That avoids guessing, but it means a step can be missing.
- Four text evidence files (`evidence_56`, `evidence_57`, `evidence_59` and `evidence_61`) aren't read by the adapters yet. They affect a small number of the public sample cases.
- In `evidence_58`, `support_001` shows up with no supporting identifier anywhere else in the participant files, so the known-ID rule doesn't link it. The sample error analysis covers this.
- Some policy and category choices rest on documented assumptions, because the evidence is incomplete. The assumptions file says which ones the samples support and which are still uncertain.

I left these gaps visible on purpose. If an audit system is unsure, you should be able to see where.

<details>
<summary>Challenge submission notes</summary>

The challenge submission and this repository do slightly different jobs:

- `output.csv` is written to the repository root by `python3 code/main.py`, and was uploaded separately to the challenge.
- `log.txt` is the AI-agent chat transcript the challenge asked for. I kept it local and uploaded it separately, so it isn't part of this public repo.
- `code.zip` holds the runnable solution, prompts, configuration, this README and the evaluation reports. It leaves out the dataset, virtual environments, caches, credentials and nested archives.

From the repository root, the package was built with:

```bash
zip -r code.zip README.md ARCHITECTURE.md problem_statement.md docs code \
  requirements.txt .env.example \
  -x "*/__pycache__/*" "*/.venv/*" "*/venv/*" \
     "*/node_modules/*" "*/.git/*" "*/.DS_Store" \
     "*.zip" "*.tar" "*.gz" "*.tgz" "*.7z" "*.rar"
```

Before submitting, check that `output.csv` has the exact header `case_id,deletion_status,steps_json`, has a row for every request and passes the invariant checker. Also make sure no API keys, credentials or personal data ended up in the repository or the submission files.

</details>

## Where to start reading

If you'd like to understand the project without opening every module:

1. [`ARCHITECTURE.md`](ARCHITECTURE.md) for the pipeline and module map.
2. [`docs/DESIGN.md`](docs/DESIGN.md) for why the data model and evidence rules look the way they do.
3. [`docs/SPEC.md`](docs/SPEC.md) for how I read the challenge contract.
4. [`docs/ASSUMPTIONS.md`](docs/ASSUMPTIONS.md) for where the evidence is incomplete and the trade-offs are spelled out.
5. [`code/evaluation/evaluation_report.md`](code/evaluation/evaluation_report.md) and [`code/evaluation/usage_report.md`](code/evaluation/usage_report.md) for the measured results and model usage.

Thanks for taking a look. I built this in a 24-hour challenge, but the lesson I want to keep for future AI systems is simple: **a good agent should be able to show why it reached an answer, not just give one.**
