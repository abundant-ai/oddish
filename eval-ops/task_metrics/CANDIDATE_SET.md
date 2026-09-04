# Overview

This candidate set contains **267 self-contained long-horizon tasks** selected for
rigorous evaluation of agentic coding and reasoning capabilities. Every task ships as an
isolated container environment containing an `instruction.md` statement, an `environment/`
setup directory, held-out acceptance `tests/`, and a reference `solution/`. All tasks are
verified reproducible against a frontier baseline, with non-vacuous tests and closed-network
execution.

The set is the subset of the full catalogue whose tasks run **75 steps or more on average**
under the frontier baseline — the genuinely long-horizon tail. Complexity and pass rates are
measured from **Claude Opus 4.8** evaluation trajectories in the linked oddish experiments
(snapshot 2026-09-04 UTC). Per-task numbers — steps, agent runtime, token totals, trial
counts, and the source experiment ids — are in the companion file
`task_metrics_avg_steps_ge75.csv`.

# Distribution and Complexity

The tasks span **4 categories**. Complexity is the mean steps, agent-execution runtime, and
trajectory tokens across Opus 4.8 evaluation trials (every real trajectory, including runs
that hit the wall-clock cap). Pass rate is the fraction of those trials reaching full reward.

| Category | Tasks | Complexity (Mean) | Pass Rate |
| :--- | :--- | :--- | :--- |
| Database & Datastore Bug Repair | 203 | 255 steps · 1.23h · 42.8M tokens | 0.15 |
| Scientific & Numerical Computing | 45 | 165 steps · 1.36h · 18.4M tokens | 0.34 |
| Spreadsheet Model Reconstruction | 8 | 204 steps · 2.65h · 57.6M tokens | 0.39 |
| Software & Model Engineering | 11 | 143 steps · 2.36h · 15.8M tokens | 0.67 |
| **Total** | **267** | **238 steps · 1.32h · 38.9M tokens** | **0.20** |

*Note: Pass rates reflect aggregate performance across Opus 4.8 evaluation trials (fraction
scoring reward 1.0). Scientific & Numerical Computing and Spreadsheet Model Reconstruction
use thresholded numerical-agreement scoring rather than a single pass/fail assertion.*

# Task Categories

## Database & Datastore Bug Repair
203 tasks · Pass Rate: 0.15

A complete source tree of a real database or datastore carries an injected defect — either a
process-killing crash or a silent wrong-result logic bug with no crash, error, or
localization signal. The build is clean and the shipped test suite passes, which is exactly
how the defect slipped through. The agent must reproduce the fault, localize it, fix it in
the real source, and iterate build → check → fix against a **closed network with no shipped
regression suite**. The defining difficulty is deciding that a result is wrong *without
already knowing the right answer*: the established approach is to build an oracle —
metamorphic query rewrites (TLP, NoREC) for the SQL engines, brute-force ground truth and
round-trip / invariance properties for the stores.

The 203 tasks cover seven systems, tracked in the `db_system` column of the CSV:

| System | Tasks | Complexity (Mean) | Pass Rate |
| :--- | :--- | :--- | :--- |
| TimescaleDB | 57 | 199 steps · 0.88h · 28.0M tokens | 0.11 |
| DuckDB | 47 | 303 steps · 1.76h · 56.0M tokens | 0.18 |
| SQLite | 36 | 288 steps · 1.33h · 47.9M tokens | 0.20 |
| RocksDB | 23 | 285 steps · 1.33h · 57.8M tokens | 0.19 |
| etcd | 15 | 304 steps · 1.11h · 57.0M tokens | 0.00 |
| Qdrant | 14 | 209 steps · 0.70h · 33.7M tokens | 0.18 |
| FAISS | 11 | 213 steps · 1.64h · 28.9M tokens | 0.03 |

Highlighted Examples
- **duckdb-complex_division**: a DuckDB build returns quietly wrong rows on some inputs; find the C++ defect with metamorphic testing and no upstream to diff against (602 steps mean).
- **sqlite-parse-cursor-alloc**: a SQLite parser cursor-allocation crash bug (588 steps mean; unsolved across 8 trials).
- **rocksdb-prevent-normal-flushes-from-star**: resuming a RocksDB database after a recoverable storage error under ongoing writes returns missing or wrong values (763 steps mean; the heaviest DB task).
- **timescaledb-fix-cached-utility-statement**: a stored routine that drops and recreates a hypertable, invoked repeatedly in one session, returns wrong rows.
- **etcd-fix-race-berween-read-index-and-lea**: linearizable reads in etcd return stale values when cluster leadership changes.
- **qdrant-fix-oob-heap-read-crash-with-mali**: restoring compressed-embedding search data from an untrusted snapshot file reads out of bounds.
- **faiss-better-nan-handling-2986**: FAISS search and training with vectors containing non-finite (NaN/Inf) entries return wrong results.

## Scientific & Numerical Computing
45 tasks · Pass Rate: 0.34

Analyze a real or synthesized measurement, instrument, or operations record and deliver a
defensible quantitative result — an inverse problem, calibration, planning table, or
paper-faithful numerical scheme — typically replacing a withdrawn first-pass answer whose
naive reading was wrong. Data is read-only under `/app/data`; the agent chooses the method
and produces numbers with honest intervals, and for the reproduction tasks must recover a
paper's printed error tables while staying correct on other grids and inputs.

Highlighted Examples
- **daem-thermogravimetric-inversion**: recover the solid-state decomposition kinetics behind a precursor from twenty thermogravimetric runs.
- **metagenome-strain-mixture**: settle the strain-level composition table for 232 faecal metagenomes over a seven-strain panel.
- **thousand-stop-parcel-runs**: a three-depot vehicle-routing re-plan qualification over ~2,800 daily stops (1,307 steps mean; the heaviest task in the set).
- **nurse-roster-yearlong**: build 26- and 52-week rosters for four hospital wards, then keep improving them (560 steps mean).
- **bura-remez-err**: reproduce a paper's best-uniform rational-approximation (Remez) error table exactly, and keep it correct off-page.
- **sdc-runge-kutta-framework**: reproduce a spectral-deferred-correction Runge-Kutta study behind a JSON-in/JSON-out kernel.

## Spreadsheet Model Reconstruction
8 tasks · Pass Rate: 0.39

A multi-sheet workbook came back from a system migration with its formula layer stripped: the
inputs, labels, and rosters survive, but not the formulas or the numbers they produced.
Rebuild the formula layer across independently seeded cases of the same model, which must all
agree.

Highlighted Examples
- **sheet-eligibility-cascade**: rebuild the adjudication workbooks for a three-site, eleven-criterion screening study.
- **sheet-multimodal-cost**: rebuild multimodal landed-cost workbooks spanning road, rail, barge, shortsea, and an air hop.
- **sheet-abx9-demurrage-calc**: rebuild the berth-laytime and demurrage workbooks for a single-berth terminal.
- **sheet-toll-distance-matrix**: rebuild the road-tolling back-office workbooks (zone distances, band table, gantry passages).
- **sheet-composite-endpoint**: rebuild the clinical outcome-comparison workbooks, walking each treated/control pair down an ordered endpoint.

## Software & Model Engineering
11 tasks · Pass Rate: 0.67

Build-and-make-it-pass engineering: post-train a small base model on a single H100 with no
internet to beat a held-out benchmark, implement a library or solver from scratch with
external dependencies banned, beat a frozen baseline within a compute budget, or exploit a
running service.

Highlighted Examples
- **post-train-math-qwen3-1.7b**: post-train Qwen3-1.7B-Base for competition mathematics (MATH), 321 steps mean.
- **post-train-kodcode-qwen2.5-1.5b**: post-train Qwen2.5-1.5B for KodCode function-level code generation.
- **post-train-synlogic-qwen2.5-1.5b**: post-train Qwen2.5-1.5B for SynLogic rule-verifiable logic puzzles (unsolved across 3 trials).
- **pubgrub-version-solver**: implement a PubGrub-style dependency version solver from scratch, standard-library Python only, with precise proofs of unsatisfiability.
- **optimize-linear-attention-scan**: implement an optimized GPU forward pass for a gated linear-attention recurrence and beat the reference within a per-instance compute budget.
- **ctf-text-sender-einherjar-01**: overlap the heap to hijack `__free_hook` in a no-PIE network service (binary exploitation).
