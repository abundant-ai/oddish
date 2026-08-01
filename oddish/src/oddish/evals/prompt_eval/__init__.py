"""Golden-based regression evals for the QA prompt files.

Compares a changed prompt against the version it replaces by running both over
frozen golden cases (``evals/prompt-goldens/`` at the repo root) with a
configurable set of analysis agents, then grading each run against the case's
expected labels. CI drives this through ``python -m oddish.evals.prompt_eval``
and posts the comparison as a PR comment; see
``.github/workflows/prompt-eval.yml``.
"""
