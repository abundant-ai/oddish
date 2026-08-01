# Prompt goldens

Frozen regression cases for the QA prompt files. When a PR changes one of the
prompts below, the `Prompt Eval` workflow
(`.github/workflows/prompt-eval.yml`) runs **only the changed prompts** over
these goldens — both the merge-base (previous) version and the PR (current)
version, with every agent listed in `config.yaml` — and posts the comparison
as a PR comment.

| Kind | Prompt file | Golden needs |
|---|---|---|
| `QA_PRE_TRIAL` | `oddish/src/oddish/analyze/prompts/pre_trial_qa.txt` | `case.yaml` + `task/` |
| `QA_POST_TRIAL` | `oddish/src/oddish/analyze/classify_prompt.txt` | `case.yaml` + `task/` + `trial/` |
| `VERDICT` | `oddish/src/oddish/analyze/verdict_prompt.txt` | `case.yaml` only |
| `TRAJECTORY_SUMMARY` | `oddish/src/oddish/analyze/prompts/trajectory_summary.txt` | not scored yet |

The harness (`oddish.evals.prompt_eval`) runs each kind through the same code
path production uses (`TrialClassifier`, `PreTrialBlock`, the verdict
template), so results reflect real worker behavior.

## Layout

```
evals/prompt-goldens/
├── config.yaml                # agents (judge models), n_trials, timeouts
└── <KIND>/cases/<case-name>/
    ├── case.yaml              # expected labels (below)
    ├── task/                  # task source snapshot (instruction, tests, solution)
    └── trial/                 # trial artifacts (result.json, agent/trajectory.json,
                               # verifier/) — QA_POST_TRIAL only
```

To add a golden from a real run: pull the trial's task and log directories
(`oddish pull` / S3), strip anything large or sensitive, drop them under a new
case directory, and write `case.yaml` with the labels a correct analysis must
produce. Keep cases small — every case runs against every agent, twice, on
each prompt-change PR.

## case.yaml

Common fields: `description` (free text), `agent` (the trial agent that
produced the golden trajectory, e.g. `claude-code` — QA_POST_TRIAL only; use
`oracle`/`nop` to trigger the baseline-specific prompt context).

### QA_POST_TRIAL

```yaml
description: Agent solved the task fairly; verifier passed.
agent: claude-code
expected:
  classification: GOOD_SUCCESS      # or classification_in: [GOOD_FAILURE, BAD_FAILURE]
  subtype_in: [Correct Solution]    # optional
```

### QA_PRE_TRIAL

Each matcher under `expected.items` must match at least one returned finding;
`forbidden` matchers must match none. Matcher fields: `dimension`
(`verifier` | `oracle` | `info_leakage`), `tier_in`, `file_contains`
(case-insensitive substring of the finding's `file`).

```yaml
description: Expected output ships in the agent workspace.
expected:
  items:
    - dimension: info_leakage
      tier_in: [must_fix, should_fix]
      file_contains: expected_output
  forbidden: []
```

### VERDICT

`inputs` are the template's fields, exactly as `build_verdict_prompt` renders
them (see `oddish/src/oddish/analyze/classifier.py`).

```yaml
description: All trials good; baseline passed.
inputs:
  num_trials: 2
  baseline_summary: "✓ Passed (nop failed as expected, oracle passed as expected)"
  quality_check_summary: "✓ Passed"
  trial_classifications: |
    Trial 1: ...
expected:
  is_good: true
  confidence_in: [high, medium]     # optional
```

## Running locally

```bash
cd oddish && uv sync --frozen
# needs `claude` on PATH and ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN
uv run python -m oddish.evals.prompt_eval ci \
  --repo-root .. \
  --base-sha "$(git merge-base origin/main HEAD)" \
  --out-dir /tmp/prompt-eval --comment-file /tmp/prompt-eval/comment.md
```

The `sample-*` cases are synthetic seeds that prove the pipeline end to end —
replace or outnumber them with goldens from real runs.
