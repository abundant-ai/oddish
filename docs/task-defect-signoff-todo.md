# Task defect sign-off

- [x] Create isolated worktree `codex/task-defect-signoff` from `fe3381b50`.
- [x] Trace generation, validation, storage, verdicts, delivery checks, acknowledgments, and history.
- [x] Document compatibility for historical findings and finalized snapshots.
- [x] Require `must_fix` for new findings without weakening evidence or execution causality.
- [x] Enforce fixing or individually acknowledging every recorded defect on active deliveries.
- [x] Preserve finding provenance and person/version-specific acknowledgment records.
- [x] Add backend regression coverage, including direct sign-off bypass attempts.
- [x] Validate Task A v7 acknowledgment and v8 reset through the inline Codex browser.
- [x] Review the final diff and run relevant checks (248 backend tests, 97 frontend tests, TypeScript, ESLint, focused Ruff checks).
- [x] Commit and open [draft PR #1569](https://github.com/abundant-ai/oddish/pull/1569) against `staging`.
