# Delivery QA meaning: implementation and validation

- [x] Confirm the referenced checkout is an isolated, clean worktree.
- [x] Trace source review, execution review, findings, version selection, reruns, delivery checks, and experiment filters before modifying them.
- [x] Build representative records: defective task, fair agent failure, invalid execution, failed review, missing review, running review, stale review, and completed review awaiting sign-off.
- [x] Distinguish task defects, execution outcomes, review progress, and version-specific human sign-off without changing blocking severity policy.
- [x] Show a concrete blocking finding, affected version, and next action in collapsed delivery rows; reuse finding and rerun controls.
- [x] Preserve experiment verdict filters and exact finding/version/file/location in shareable URLs and browser history; explain unavailable historical evidence.
- [x] Run focused backend and frontend regression checks, including combinations of representative records and paid-work boundaries.
- [x] Exercise blocker navigation, URL restoration, and unavailable evidence in the inline Codex browser.
- [x] Inspect the final diff and document observed validation and automatic rerun follow-up.
- [ ] Open a draft PR against staging.
