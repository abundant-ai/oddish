# Report Format

Write one markdown report with the following sections in this order.

## 1. Summary

Include:

- task path
- one-paragraph description of what the task is supposed to do
- overall verdict
- whether the golden solution was locally validated

## 2. Local Validation

Include:

- commands run
- build result
- golden execution result
- verifier result
- reward result
- LLMJ Agentic JSON validation result when the task uses `llm_judge.py`, `visual_judge.py`, `vlm_judge.py`, or `agentic_judge.json`
- blockers or caveats

If you could not fully validate locally, say so plainly here.

## 3. Findings By Check Group

Create these sections exactly:

- `## 1. Instruction Clarity`
- `## 2. Verifier Consistency`
- `## 3. Golden Solution`
- `## 4. Docker And Dependencies`
- `## 5. Reward Hacking`

Within each section:

- list findings from highest severity to lowest
- if there are no findings, write `No issues found.`

Use this issue template:

```markdown
### <Severity> - <Short title>

Evidence:
- `<path>:<line>` - <fact>
- `<path>:<line>` - <fact>

Why it matters:
- <grading, security, or task-quality impact>

Suggested fixes:
- <difficulty-preserving option 1>
- <difficulty-preserving option 2 if relevant>
```

## 4. Recommended Next Actions

Finish with a short flat list:

- blocking fixes that should happen before shipping
- optional follow-ups
- open questions that still need proof

## Style Rules

- Be concrete and evidence-driven.
- Keep findings scoped to the task as written.
- Do not bury a blocking exploit in a soft paragraph.
- Do not suggest changes that make the task easier or less realistic just to avoid fixing the real problem.
