# Migration Examples

Use these examples as pattern matches while migrating old LLM/VLM judges. They are not a second spec; if anything conflicts, follow `new-agentic-judge-format.md`.

## Python Artifact Map -> agentic_judge.json

Legacy pattern from PR 495:

```python
GOLD_DIR = Path("/tests/gold_results")

PLOTS = {
    "eeg_visual_presentation": (
        Path("/app/pred_results/eeg2eeg_vis_pred.png"),
        GOLD_DIR / "eeg2eeg_vis_gold.png",
    ),
    "reconstruction_error_plot": (
        Path("/app/pred_results/reconstruction_error.png"),
        GOLD_DIR / "reconstruction_error_gold.png",
    ),
}
```

Target shape:

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
    },
    {
      "name": "reconstruction_error_plot",
      "predicted_path": "/app/pred_results/reconstruction_error.png",
      "reference_path": "/tests/gold_results/reconstruction_error_gold.png",
      "rubric_key": "reconstruction_error_plot",
      "media_type": "image/png",
      "description": "Pointwise generated-minus-real reconstruction error plot."
    }
  ]
}
```

Keep the old `llm_judge.py` runnable, but refactor it to read `agentic_judge.json` and `agentic_rubric.json` instead of owning `PLOTS`.

## Single Visual Judge -> One Artifact

Legacy pattern from PR 889:

```python
pred_path = Path("/app/pred_results/hca_cell_type_pca.png")
gold_path = Path("/tests/gold_results/hca_cell_type_pca_gold.png")
requirements = data.get("visual_requirements", [])
```

Target `agentic_judge.json`:

```json
{
  "version": 1,
  "mode": "visual_artifact_comparison",
  "scoring_mode": "rubric",
  "artifacts": [
    {
      "name": "hca_cell_type_pca",
      "predicted_path": "/app/pred_results/hca_cell_type_pca.png",
      "reference_path": "/tests/gold_results/hca_cell_type_pca_gold.png",
      "rubric_key": "hca_cell_type_pca",
      "media_type": "image/png",
      "description": "Single-cell UMAP/PCA visualization colored by cell type."
    }
  ]
}
```

Target `agentic_rubric.json`:

```json
{
  "hca_cell_type_pca": [
    "The predicted figure is a UMAP scatter plot (not a different chart type).",
    "The plot shows many single-cell points distributed into multiple clusters."
  ]
}
```

Rename generic keys such as `visual_requirements` or `requirements` to the artifact key unless multiple artifacts intentionally share that rubric section.

## Prompt Nuance -> Description Or Runner Prompt

Legacy pattern:

```python
prompt = (
    "Treat orientation, rotation, reflection, and translation differences as acceptable. "
    "Focus on chart type, visible structure, label/legend quality, and artifact-label handling."
)
```

Target placement:

- Put short artifact context in `artifact.description`.
- Keep task-specific judge instructions in the Harbor compatibility runner prompt if local Harbor judging still needs them.
- Put only AgenticGrader scoring criteria in `agentic_rubric.json`.
- Put deterministic MCP-only thresholds or verifier rubric data in `mcp_rubric.json`.

Do not turn runtime instructions, model names, API behavior, or artifact paths into rubric criteria.

## Blended test.sh Score -> Separated Phases

Legacy pattern:

```bash
python3 /tests/test_outputs.py
DET_EXIT=$?
python3 /tests/visual_judge.py
JUDGE_EXIT=$?
FINAL_SCORE=$(python3 - "$DET_RATIO" "$VISUAL_SCORE" ...)
```

Target shape:

```bash
if [ $STATUS -eq 0 ]; then
  python3 /tests/test_outputs.py || STATUS=1
fi

if [ $STATUS -eq 0 ]; then
  python3 /tests/llm_judge.py || STATUS=1
fi
```

Preserve Harbor behavior when necessary, but keep candidate execution, deterministic verification, and LLM/VLM judging visibly separable. The converter should be able to bypass only the judge phase for MCP grading.

## Quick Mapping Rules

- `PLOTS`, `IMAGES`, `ARTIFACTS`, or similar dict entries usually become one artifact per key.
- A lone `pred_path` plus `gold_path` pair usually becomes one artifact named after the predicted file stem.
- `GOLD_DIR / "file.png"` maps to `/tests/gold_results/file.png`.
- Predicted artifacts should stay under `/app`, `/repo`, `/workspace`, or `/workdir`.
- Trusted references should stay under `/tests`.
- Deterministic reports, numeric checks, hidden oracles, and anti-cheat probes stay out of AgenticGrader rubric criteria.
