# Inclusion Policy

## Purpose

This repo is the durable record of conversion work, not a scratch space for unvalidated rewrites.

## Allowed Immediately

- tracking notes
- checklists
- process writeups
- QA result summaries
- validation logs

## Not Allowed Until Validated

- rewritten Harbor task files
- task-local `tests/` rewrites
- staged task bundles intended to represent a fixed task

## Validation Gate For Task Files

A rewritten task may be added only after:

1. it has been converted and run in Taiga, and
2. the Taiga result shows no critical conversion-level issue, and
3. any remaining QA findings are documented as task-level or non-critical

## Commit Style

- Keep notes and validation records separate from task-file commits when possible.
- Prefer one commit per meaningful step so the repo history reflects the investigation timeline.
