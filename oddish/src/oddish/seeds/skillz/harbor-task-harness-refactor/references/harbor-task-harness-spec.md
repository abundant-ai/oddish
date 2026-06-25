# Harbor Task Harness Spec

## Goal

Define the minimal task contract needed to convert a Harbor task to Taiga without collapsing solve, verify, and grading into one shell script.

## Required Trust Model

- Trusted runtime stages pristine and hidden inputs, chooses which phase runs next, and writes the final canonical reward.
- Untrusted model code reads allowed task inputs and writes declared task outputs only.

## Phases

### `prepare`

Trusted.

Responsibilities:

- restore public inputs
- overlay hidden inputs when needed
- clear declared output roots
- create scratch roots for verifier and judge output

Must not:

- run candidate-owned code
- write final reward artifacts

### `solve`

Untrusted.

Responsibilities:

- run the candidate notebook, script, or command
- write only declared outputs and candidate logs

Must not:

- read hidden inputs that are not part of the solve contract
- read verifier-only assets
- write final reward artifacts

### `verify`

Trusted.

Responsibilities:

- inspect outputs from `solve`
- run deterministic checks
- emit verifier results into scratch

Must not:

- run or import model-owned code
- write final reward artifacts

### `judge`

Trusted and optional.

Responsibilities:

- run LLM or VLM evaluation over declared artifacts
- emit judge results into scratch

Must not:

- run candidate-owned code
- write final reward artifacts

### `finalize`

Trusted runtime only.

Responsibilities:

- aggregate verifier and judge outputs
- compute the final reward and metadata
- write canonical `/logs/verifier/reward.json`
- write canonical `/logs/verifier/reward.txt`

This is the only phase that may write the final reward artifacts.

## Compatibility Contract

Legacy Harbor `tests/test.sh` can still exist, but treat it as a compatibility wrapper rather than the source of reward authority.

In compatibility mode:

- candidate execution may still happen through legacy shell entrypoints
- reward writes should go to scratch or runtime-bound paths
- the trusted runtime should materialize the canonical reward afterward

## Salvage Or Rewrite

Use the converter when the task mostly fits the standard shape:

- notebook or script execution
- deterministic pytest-style verification
- optional judge step
- explicit reward scratch

Rewrite the task side when the harness:

- generates hidden fixtures inside `test.sh`
- reruns the candidate multiple times with custom snapshots
- mixes bespoke orchestration with grading logic
- depends on task-specific hashes or artifact diffs in shell
