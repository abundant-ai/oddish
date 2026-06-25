# New Agentic Judge Format

Use this as the task-side contract for Harbor LLM/VLM judges that should convert to Taiga native `agentic_grader` grading.

## Directory Shape

```text
tests/
  test.sh
  verify_outputs.py              # optional deterministic verifier
  llm_judge.py                   # Harbor compatibility runner
  agentic_judge.json             # artifact/judge wiring
  agentic_rubric.json            # AgenticGrader criteria only
  mcp_rubric.json                # optional deterministic/MCP-only rubric or thresholds
  gold_results/                  # optional reference artifacts
```

`agentic_judge.json` is the source of truth for what artifacts are judged. `agentic_rubric.json` is the source of truth for AgenticGrader criteria. `mcp_rubric.json`, when present, is reserved for deterministic MCP checks. `llm_judge.py` remains runnable in Harbor, but should read those declarative files instead of hard-coding task wiring.

## agentic_judge.json

```json
{
  "version": 1,
  "mode": "visual_artifact_comparison",
  "scoring_mode": "rubric",
  "artifacts": [
    {
      "name": "eeg_visual_presentation",
      "predicted_path": "/app/pred_results/eeg2eeg_vis_pred.png",
      "reference_path": "/tests/gold_results/eeg2eeg_vis_gold.png",
      "rubric_key": "eeg_visual_presentation",
      "media_type": "image/png",
      "description": "Primary real-vs-generated EEG waveform plot."
    }
  ]
}
```

Required top-level fields:

- `version`: currently `1`.
- `mode`: currently use `visual_artifact_comparison` for image/reference comparison tasks.
- `scoring_mode`: `rubric` for migrated Harbor tasks unless the converter explicitly supports another mode.
- `artifacts`: non-empty list.

Required artifact fields:

- `name`: stable artifact identifier.
- `predicted_path`: absolute path to model output.
- `rubric_key`: key in `agentic_rubric.json`.
- `media_type`: MIME type, such as `image/png`, `text/markdown`, `application/json`, or `text/csv`.

Optional artifact fields:

- `reference_path`: absolute trusted reference artifact, normally under `/tests`.
- `description`: short context for the grader.
- `required`: boolean, default `true`.

Path rules:

- Predicted paths should be explicit and under `/app`, `/repo`, `/workspace`, or `/workdir`.
- Reference paths should be explicit and under `/tests`.
- Avoid globs in v1.

## agentic_rubric.json

Use artifact keys with criteria arrays:

```json
{
  "eeg_visual_presentation": [
    "The output is a single-panel line chart with exactly two continuous EEG waveforms.",
    "The two waveform lines are distinguishable by a visible legend or equivalent labels."
  ]
}
```

Object entries with explicit relative weights are also acceptable:

```json
{
  "eeg_visual_presentation": [
    {
      "criterion": "The output is a single-panel line chart with exactly two continuous EEG waveforms.",
      "weight": 2
    }
  ]
}
```

Do not put predicted paths, reference paths, judge prompts, API keys, or model names in `agentic_rubric.json`.

New structured LLM-judge tasks should use `tests/agentic_rubric.json`. `tests/rubric.json` is accepted only as a backward-compatible alias during migration.

## mcp_rubric.json

Use this optional file only for deterministic MCP checks, such as fixed thresholds or structured data read by `verify_*.py` or `test.sh`.

If present, the converter keeps `mcp_rubric.json` in the MCP tests payload and materializes it as `/grader/tests/rubric.json` for MCP-side compatibility. It is not uploaded to AgenticGrader.

## test.sh Contract

Keep deterministic and LLM judge phases separate:

```bash
if [ $STATUS -eq 0 ]; then
  python3 /tests/verify_outputs.py || STATUS=1
fi

if [ $STATUS -eq 0 ]; then
  python3 /tests/llm_judge.py || STATUS=1
fi
```

The converter should keep candidate execution and deterministic checks in MCP grading, bypass only the legacy LLM/VLM judge phase, and let Taiga AgenticGrader evaluate the former judge portion.

## Taiga Conversion Expectations

The converter defaults detected Harbor LLM/VLM judge tasks to the AgenticGrader path. Those tasks must provide `tests/agentic_judge.json` plus `tests/agentic_rubric.json`, with `tests/rubric.json` accepted only as a migration fallback for the AgenticGrader rubric. The legacy MCP-hosted LLM judge path is no longer supported for detected judge tasks.

The converted problem should use both graders:

```json
[
  {"type": "mcp", "weight": 1.0},
  {
    "type": "agentic_grader",
    "weight": 1.0,
    "grading_model": "claude-sonnet-4-6",
    "agentic_grader_params": {
      "container": "reuse_existing",
      "scoring_mode": "rubric",
      "grader_extra_tools": ["bash", "python_exec"],
      "grader_guidance": "...",
      "include_task_prompt": true,
      "include_task_transcript": true,
      "include_input_files": true
    }
  }
]
```

For `scoring_mode: "rubric"`, the converter flattens `agentic_rubric.json` criteria into problem-level rubric items and uploads it as `grader_files/rubric.json` plus trusted reference artifacts as AgenticGrader files. Deterministic MCP grader files stay in the generated MCP tests payload.

In `llm_judge_grading=agentic` mode, the generated MCP tests payload should be deterministic-only. It removes `llm_judge.py`, `vlm_judge.py`, `check_llm_scores.py`, `agentic_judge.json`, `agentic_rubric.json`, and compatibility `rubric.json`. If the source task has `tests/mcp_rubric.json`, the converter materializes it as `/grader/tests/rubric.json`.

If the structured AgenticGrader inputs are missing, conversion should fail closed instead of trying to infer artifact paths from old Python-only judge code. Do not use `--llm-judge-grading mcp` as a fallback for detected LLM/VLM judge tasks.

## AgenticGrader Guidance Requirements

Generated guidance should tell the grader to:

- Evaluate only the former Harbor LLM/VLM judge portion.
- Do not run the legacy `/tests/llm_judge.py` path as the source of truth.
- Inspect each predicted artifact and any reference artifact.
- Apply the rubric criteria associated with each artifact key.
- Treat deterministic verifier checks as MCP-owned, not AgenticGrader-owned.
