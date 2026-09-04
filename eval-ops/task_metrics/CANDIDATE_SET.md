# Overview

This candidate set contains **267 self-contained long-horizon tasks** for evaluating agentic
coding and reasoning. Each ships as an isolated container with an `instruction.md`, an
`environment/` setup directory, held-out acceptance `tests/`, and a reference `solution/`, and
runs on a closed network. The set is the subset of the full catalogue that runs **75 steps or
more on average** under the frontier baseline. Complexity and pass rates are measured from
**Claude Opus 4.8** trajectories in the linked oddish experiments (snapshot 2026-09-04 UTC);
per-task numbers are in the companion file `task_metrics_avg_steps_ge75.csv`.

# Distribution and Complexity

Complexity is the mean steps, agent-execution runtime, and trajectory tokens across Opus 4.8
trials (every real trajectory, including runs that hit the wall-clock cap). Pass rate is the
fraction of trials reaching full reward.

| Category | Tasks | Complexity (Mean) | Pass Rate |
| :--- | :--- | :--- | :--- |
| DevOps Bug Repair | 203 | 255 steps · 1.23h · 42.8M tokens | 0.15 |
| Scientific & Research | 45 | 165 steps · 1.36h · 18.4M tokens | 0.34 |
| Software & Model Engineering | 19 | 168 steps · 2.48h · 33.0M tokens | 0.55 |
| **Total** | **267** | **238 steps · 1.32h · 38.9M tokens** | **0.20** |

*Pass rates are the fraction of Opus 4.8 trials scoring full reward. Scientific & Research and
the spreadsheet tasks use thresholded numerical-agreement scoring.*

# Task Categories

## DevOps Bug Repair
203 tasks · Pass Rate: 0.15

A real database or datastore source tree (DuckDB, SQLite, RocksDB, TimescaleDB, Qdrant, etcd,
FAISS) carries an injected defect — a silent wrong-result logic bug or a crash — that builds
clean and passes the shipped test suite. Reproduce, localize, and fix it with no regression
suite and no network, building an oracle to decide correctness without the answer.

Highlighted Examples
- **duckdb-complex_division**: quietly wrong query rows; find the C++ defect with metamorphic testing (602 steps mean).
- **rocksdb-prevent-normal-flushes-from-star**: wrong/missing values when resuming after a recoverable storage error under load (763 steps mean).
- **timescaledb-fix-cached-utility-statement**: a stored routine that recreates a hypertable each call returns wrong rows.
- **etcd-fix-race-berween-read-index-and-lea**: linearizable reads go stale on leadership change.
- **faiss-better-nan-handling-2986**: search and training with NaN/Inf vectors return wrong results.

## Scientific & Research
45 tasks · Pass Rate: 0.34

Analyze a real or synthesized measurement, instrument, or operations record and deliver a
defensible quantitative result — an inverse problem, calibration, planning table, or
paper-faithful numerical scheme — usually replacing a withdrawn first-pass answer.

Highlighted Examples
- **daem-thermogravimetric-inversion**: recover decomposition kinetics from twenty thermogravimetric runs.
- **metagenome-strain-mixture**: strain-level composition table for 232 faecal metagenomes.
- **thousand-stop-parcel-runs**: three-depot vehicle-routing re-plan over ~2,800 stops (1,307 steps mean; heaviest in the set).
- **bura-remez-err**: reproduce a paper's Remez rational-approximation error table and keep it correct off-page.

## Software & Model Engineering
19 tasks · Pass Rate: 0.55

Build-and-make-it-pass work: rebuild a stripped spreadsheet model's formula layer, post-train
a small model on one H100 to beat a held-out benchmark, implement a library or solver from
scratch, beat a baseline within a compute budget, or exploit a running service.

Highlighted Examples
- **sheet-eligibility-cascade**: rebuild the adjudication workbooks for a three-site screening study.
- **sheet-multimodal-cost**: rebuild multimodal landed-cost workbooks (road, rail, barge, shortsea, air).
- **post-train-math-qwen3-1.7b**: post-train Qwen3-1.7B-Base for competition mathematics (321 steps mean).
- **pubgrub-version-solver**: implement a PubGrub dependency solver from scratch, standard-library only, with unsatisfiability proofs.
- **optimize-linear-attention-scan**: implement an optimized GPU forward pass for a gated linear-attention recurrence.
