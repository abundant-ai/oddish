# Overview

This candidate set contains **267 self-contained long-horizon tasks** selected for
rigorous evaluation of agentic coding and reasoning capabilities. Every task ships as an
isolated container environment containing an `instruction.md` statement, an `environment/`
setup directory, held-out acceptance `tests/`, and a reference `solution/`. All tasks are
verified reproducible against a frontier baseline, with non-vacuous tests and closed-network
execution.

The set is the subset of the full catalogue whose tasks run **75 steps or more on average**
under the frontier baseline, i.e. the genuinely long-horizon tail; shorter tasks are
excluded. Complexity and pass rates are measured from **Claude Opus 4.8** evaluation
trajectories in the linked oddish experiments (snapshot 2026-09-04 UTC). Per-task numbers —
steps, agent runtime, token totals, trial counts, and the source experiment ids — are in the
companion file `task_metrics_avg_steps_ge75.csv`.

# Distribution and Complexity

The tasks span **12 categories**. Complexity is the mean steps, agent-execution runtime, and
trajectory tokens across Opus 4.8 evaluation trials (every real trajectory, including runs
that hit the wall-clock cap). Pass rate is the fraction of those trials reaching full reward.

| Category | Tasks | Complexity (Mean) | Pass Rate |
| :--- | :--- | :--- | :--- |
| DuckDB | 47 | 303 steps · 1.76h · 56.0M tokens | 0.18 |
| SQLite | 36 | 288 steps · 1.33h · 47.9M tokens | 0.20 |
| RocksDB | 23 | 285 steps · 1.33h · 57.8M tokens | 0.19 |
| TimescaleDB | 57 | 199 steps · 0.88h · 28.0M tokens | 0.11 |
| Qdrant | 14 | 209 steps · 0.70h · 33.7M tokens | 0.18 |
| etcd | 15 | 304 steps · 1.11h · 57.0M tokens | 0.00 |
| FAISS | 11 | 213 steps · 1.64h · 28.9M tokens | 0.03 |
| Scientific Data Analysis | 40 | 173 steps · 1.37h · 18.4M tokens | 0.30 |
| Numerical Methods Reproduction | 5 | 104 steps · 1.27h · 18.7M tokens | 0.67 |
| Spreadsheet Model Reconstruction | 8 | 204 steps · 2.65h · 57.6M tokens | 0.39 |
| Model Post-Training | 8 | 158 steps · 2.89h · 15.6M tokens | 0.71 |
| Software & Systems Engineering | 3 | 105 steps · 0.97h · 16.3M tokens | 0.56 |
| **Total** | **267** | **238 steps · 1.32h · 38.9M tokens** | **0.20** |

*Note: Pass rates reflect aggregate performance across Opus 4.8 evaluation trials (fraction
scoring reward 1.0). The seven database families are one methodology applied to seven
different systems; they are broken out as separate categories because the system under
repair — SQL engine, storage engine, time-series extension, vector store, consensus store —
dominates what the task actually demands. Scientific Data Analysis and Spreadsheet Model
Reconstruction use thresholded numerical-agreement scoring rather than a single pass/fail
assertion.*

# Task Categories

## Database & Datastore Bug Repair

**203 tasks across 7 systems.** A complete source tree of a real database or datastore carries
an injected defect — either a process-killing crash or a silent wrong-result logic bug with
no crash, error, or localization signal. The build is clean and the shipped test suite passes,
which is exactly how the defect slipped through. The agent must reproduce the fault, localize
it, fix it in the real source, and iterate build → check → fix against a **closed network with
no shipped regression suite**. The defining difficulty is deciding that a result is wrong
*without already knowing the right answer*: the established approach is to build an oracle —
metamorphic query rewrites (TLP, NoREC) for the SQL engines, brute-force ground truth and
round-trip / invariance properties for the stores.

### DuckDB
47 tasks · Pass Rate: 0.18 — C++ SQL analytics engine; silent wrong-rows and crash defects.
- **complex_division**: queries return quietly wrong rows on some inputs; find the C++ defect with metamorphic testing (no upstream to diff against).
- **cte_unused_columns**: optimizer column-pruning for `WITH` clauses referenced from several places drops the wrong columns.
- **lateral_left_join**: correlated subqueries joined on complex predicates instead of simple column equality return wrong rows.
- **test_delete_indexed**: `DELETE` on tables carrying unique indexes deletes the wrong rows.
- **index_fetch**: index-based single-row lookups read `VARIANT` column values incorrectly.

### SQLite
36 tasks · Pass Rate: 0.20 — C SQL engine; logic bugs (wrong result set) and crash bugs.
- **real-from-clause**: a logic bug in handling real-valued `FROM`-clause values; build a metamorphic oracle and fix it.
- **collate-rhs-in**: collation on the right-hand side of an `IN` comparison returns the wrong rows.
- **parse-cursor-alloc**: a parser cursor-allocation crash bug (588 steps mean; unsolved across 8 trials).
- **rightjoin-indexed-expr-scope**: a `RIGHT JOIN` with an indexed expression crashes on scope resolution.
- **cast-negzero**: a `CAST` logic bug around negative-zero handling.

### RocksDB
23 tasks · Pass Rate: 0.19 — C++ key-value store; wrong reads/writes and aborts.
- **prevent-normal-flushes-from-star**: resuming a database after a recoverable storage error under ongoing write traffic returns missing or wrong values (763 steps mean; the heaviest DB task).
- **fix-blob-file-path-misidentifica**: completed blob files produced by compaction are reported incorrectly to file-tracking listeners.
- **fix-the-handling-of-wide-column**: merge writes against wide-column entities go wrong when successive merges combine on insert.
- **fix-range-tombstone-entry-accoun**: range-deletion handling during memtable flush miscounts entries.
- **bug-fix-reject-empty-string-as-a**: column-family creation aborts on the names it should accept.

### TimescaleDB
57 tasks · Pass Rate: 0.11 — C PostgreSQL time-series extension; wrong rows over hypertables.
- **fix-cached-utility-statement**: a stored routine that drops and recreates a hypertable, invoked repeatedly in one session, returns wrong rows.
- **fix-merge-behaviour-with-upd**: `MERGE` statements on hypertables mishandle matched-row updates.
- **support-cagg-invalidations-f**: precomputed continuous-aggregate views go wrong under high-throughput ingest and mass removal.
- **fix-decoding-of-uuid-v7-time**: converting UUID v7 values back into timestamps loses sub-millisecond precision.
- **fix-use-after-free-in-alter**: adding a row-validation rule to a hypertable with compressed chunks corrupts results.

### Qdrant
14 tasks · Pass Rate: 0.18 — Rust vector database; wrong search results and validation gaps.
- **fix-oob-heap-read-crash-with-mali**: restoring compressed-embedding search data from an untrusted snapshot file reads out of bounds.
- **fix-bool-index-reload-as-immutabl**: boolean payload filtering on appendable segments returns wrong hits after a restart.
- **fix-tq-quantized-vector-layout-al**: ternary quantization mislays vectors whose dimension count is not a multiple of 32.
- **fix-clear-joint-consensus-fields**: a node stopped mid membership-change and restarted resolves cluster state incorrectly.

### etcd
15 tasks · Pass Rate: 0.00 — Go distributed key-value store; consistency and consensus defects.
- **fix-race-berween-read-index-and-lea**: linearizable reads return stale values when cluster leadership changes.
- **clientv3-fix-the-design-implementat**: the double-barrier coordination recipe breaks as participants join and leave.
- **server-ignore-raft-messages-if-memb**: incoming raft messages from cluster peers are mishandled.
- **etcdserver-fix-nil-pointer-panic-fo**: slow-request warning logging for read-only transactions panics.

### FAISS
11 tasks · Pass Rate: 0.03 — C++ vector-search library; wrong search/index results.
- **better-nan-handling-2986**: search and training with vectors containing non-finite (NaN/Inf) entries return wrong results.
- **io-support-for-indexnndescent-2493**: saving, loading and copying NN-descent graph indexes round-trips incorrectly.
- **fix-ivf-quantizer-centroid-shardin**: splitting an IVF index's coarse centroids into shard files corrupts search.
- **scan-exactly-max-codes-elements-26**: the scanned-vector budget applied during IVF search is enforced wrongly.

## Scientific Data Analysis
40 tasks · Pass Rate: 0.30 — Analyze a real or synthesized measurement, instrument, or
operations record and deliver a defensible quantitative result — an inverse problem,
calibration, or planning table — typically replacing a withdrawn first-pass answer whose
naive reading was wrong. Everything is in a read-only `/app/data`; the agent decides the
method and produces the numbers with honest intervals.
- **daem-thermogravimetric-inversion**: recover the solid-state decomposition kinetics behind a precursor from twenty thermogravimetric runs.
- **metagenome-strain-mixture**: settle the strain-level composition table for 232 faecal metagenomes over a seven-strain panel.
- **gnss-velocity-budget**: rebuild a defensible station-velocity budget from six years of daily positions across 22 GNSS stations.
- **nurse-roster-yearlong**: build 26- and 52-week rosters for four hospital wards, then keep improving them (560 steps mean).
- **thousand-stop-parcel-runs**: a three-depot vehicle-routing re-plan qualification over ~2,800 daily stops (1,307 steps mean; the heaviest task in the set).

## Numerical Methods Reproduction
5 tasks · Pass Rate: 0.67 — Reproduce a numerical method from a supplied paper: implement the
scheme behind `/app/run.py` (JSON in, JSON out) so it recovers the paper's printed error
tables and stays correct on other grids, sample points, and report modes — not only the rows
on the page.
- **bura-remez-err**: recover the printed best-uniform rational-approximation (Remez) error table.
- **sdc-runge-kutta-framework**: reproduce a spectral-deferred-correction Runge-Kutta study.
- **compact-rosenau-rrlw**: implement a conservative fourth-order compact scheme for the Rosenau-RLW equation.
- **cfdm6-mp-bvp**: implement a high-order compact finite-difference scheme for a boundary-value problem.

## Spreadsheet Model Reconstruction
8 tasks · Pass Rate: 0.39 — A multi-sheet workbook came back from a system migration with its
formula layer stripped: the inputs, labels, and rosters survive, but not the formulas or the
numbers they produced. Rebuild the formula layer across independently seeded cases of the same
model, which must all agree.
- **sheet-eligibility-cascade**: rebuild the adjudication workbooks for a three-site, eleven-criterion screening study.
- **sheet-multimodal-cost**: rebuild multimodal landed-cost workbooks spanning road, rail, barge, shortsea, and an air hop.
- **sheet-abx9-demurrage-calc**: rebuild the berth-laytime and demurrage workbooks for a single-berth terminal.
- **sheet-toll-distance-matrix**: rebuild the road-tolling back-office workbooks (zone distances, band table, gantry passages).
- **sheet-composite-endpoint**: rebuild the clinical outcome-comparison workbooks walking each treated/control pair down an ordered endpoint.

## Model Post-Training
8 tasks · Pass Rate: 0.71 — Post-train a small base model on one H100 with no internet so it
scores as high as possible on a held-out benchmark split; the grader loads `/app/final_model`
and measures pass@1 (or accuracy) on data absent from the container.
- **post-train-math-qwen3-1.7b**: post-train Qwen3-1.7B-Base for competition mathematics (MATH), 321 steps mean.
- **post-train-kodcode-qwen2.5-1.5b**: post-train Qwen2.5-1.5B for KodCode function-level code generation.
- **post-train-knights-knaves-qwen2.5-1.5b**: post-train Qwen2.5-1.5B for Knights-and-Knaves logical-deduction puzzles.
- **post-train-synlogic-qwen2.5-1.5b**: post-train Qwen2.5-1.5B for SynLogic rule-verifiable logic puzzles (unsolved across 3 trials).

## Software & Systems Engineering
3 tasks · Pass Rate: 0.56 — Build-and-make-it-pass engineering: implement a library or solver
from scratch with external dependencies banned, beat a frozen baseline within a compute
budget, or exploit a running service.
- **pubgrub-version-solver**: implement a PubGrub-style dependency version solver from scratch, standard-library Python only, with precise proofs of unsatisfiability.
- **optimize-linear-attention-scan**: implement an optimized GPU forward pass for a gated linear-attention recurrence and beat the reference within a per-instance compute budget.
- **ctf-text-sender-einherjar-01**: overlap the heap to hijack `__free_hook` in a no-PIE network service (binary exploitation).
