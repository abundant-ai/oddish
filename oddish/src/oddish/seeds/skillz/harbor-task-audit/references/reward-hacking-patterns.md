# Reward-Hacking Patterns

Use this as a threat-model checklist. The goal is to find ways a malicious candidate can get a passing score without satisfying the real task.

## Trusted Versus Untrusted Boundary

Treat candidate code as untrusted. Treat verifier logic, runtime-owned reward handling, hidden fixtures, and judge aggregation as trusted.

Flag any design where trusted code:

- imports candidate modules
- executes candidate callbacks or notebooks directly in the verify phase
- reads candidate-produced metadata as the source of truth
- accepts candidate-created reward structures without a strict integrity check

## Common Exploit Surfaces

### Reward File Forgery

Look for:

- candidate-writable `/logs/verifier/reward.txt` or `reward.json`
- verifier trusting structured `reward.json` without a required run nonce
- harnesses that unlink or recreate runtime-owned reward paths
- simulation or notebook code that can call file I/O into verifier-owned locations

### Candidate-Controlled Side Channels

Look for:

- verifier trusting stdout summaries written by candidate code
- verifier trusting JSON, CSV, or logs created by the candidate when the verifier could compute the result directly
- hidden testbenches and candidate code writing to the same shared results file

### Static Scanner Bypass

Look for language-specific bypasses, including:

- preprocessor macros and token pasting
- indirection through helper functions or alternate APIs
- string-concatenation or alias-based bypasses for blocked tokens
- deny-lists applied before preprocessing or normalization

### Runtime Permission Mistakes

Look for:

- candidate can write `/grader`, `/data`, `/logs/verifier`, or hidden fixture roots
- runtime-owned directories are group-writable or recreated by task code
- test harness assumes root-like permissions that the real runtime should not grant

### Failure-Path Abuse

Look for:

- verifier wrapper converts infra failures into a normal zero score, hiding broken grading
- timeouts, internal pytest errors, or missing tests appear identical to candidate failure
- harnesses ignore verifier exit codes and write a success reward anyway

## Language-Specific Clues

SystemVerilog or HDL tasks:

- `$fopen`, `$fwrite`, `$display`, `$finish`, `$system`, DPI, `force`, `release`, `bind`, preprocessor macros, hierarchical testbench references, plusargs
- shared simulation stdout or results files treated as authoritative

Python tasks:

- `subprocess`, `os.system`, `importlib`, `runpy`, dynamic imports from `/app`, writing verifier artifacts, monkeypatching imported verifier modules

Notebook tasks:

- cells that overwrite final results artifacts directly
- verifier only checking that a file exists rather than recomputing content
- stale output cells making a notebook appear correct without execution

General file-based tasks:

- verifier validates filenames or superficial schema only
- output hashes or sentinel files can be copied from starter artifacts
- hidden data leaks into candidate-visible locations

## What Counts As A Stronger Fix

Prefer fixes that restore a clean trust boundary:

- verifier computes truth from task outputs rather than trusting candidate summaries
- runtime owns canonical reward writing
- trusted paths are not writable by the candidate
- integrity checks require exact nonce match when structured reward is accepted
- language-specific enforcement happens after preprocessing or via authoritative tooling when possible

Prefer not to recommend weaker "spot patches" when a boundary fix is available.
