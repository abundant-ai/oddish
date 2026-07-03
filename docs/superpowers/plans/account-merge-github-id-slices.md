# github_id workstream — vertical slices (subagent-ownable, TDD'd, Codex-reviewed)

Optional correctness upgrade from [account-merge-plan.md](account-merge-plan.md)
(workstreams A2/B/C-precedence/D). Each slice is an independently testable
vertical increment: one subagent owns it end to end, writes/flips its tests
first (red → green), and hands back a green suite. The orchestrator runs a Codex
review and commits **between every slice**. Read
[account-merge-lessons-learnt.md](account-merge-lessons-learnt.md) first; append
to it after.

`github_id` = Clerk `provider_user_id` (a STRING). Column is TEXT, org-scoped,
nullable. It is NOT security-critical; it survives handle renames/recycles.

## Slice dependency graph
```
G1 (schema) ──┬─→ G3 (Clerk extraction) ──┬─→ G4 (lookup precedence + endpoint actor_id)
              │                            └─→ G5 (backfill job)
              ├─→ G2 (submission + CLI transport)
              └─→ G6 (user API + FE surface)
```
Build order: **G1 → G2 → G3 → G4 → G5 → G6** (G2/G6 only need G1; kept serial so
each commit is reviewed and the shared files don't collide).

---

## G1 — Model column + migration (foundation)
**Goal.** `UserModel.github_id` exists, org-unique, indexed; prod-shippable.
**Changes.**
- `backend/models.py`: `github_id: Mapped[str | None] = mapped_column(Text, nullable=True)`; add `Index("idx_users_org_github_id", "org_id", "github_id")` and `UniqueConstraint("org_id", "github_id", name="uq_users_org_github_id")` to `__table_args__`. (NULLs are distinct in PG, so existing all-NULL rows don't collide.)
- `backend/alembic/versions/`: new migration off the current backend head adding the column + index + unique constraint, guarded (`IF NOT EXISTS` / `pg_constraint` existence check), matching the raw-SQL house style in `a1b2c3d4e5f6_add_cloud_auth_tables.py`. Determine the backend head with `cd backend && uv run alembic heads` (do NOT run `upgrade` from `oddish/` — cross-stack cycle; the migration is validated by review + create_all, not local alembic).
**Tests (`backend/tests/test_github_linkage_gate.py`).** Flip `test_user_model_has_github_id`; add a unit asserting the org-scoped unique constraint + index are present in `UserModel.__table__`.
**After.** Rebuild the local test DB via create_all (the column must exist in PG) and confirm the whole suite stays green.
**Review focus.** Migration down_revision correctness; nullable + org-unique (not global); index for the lookup; no prod-data backfill embedded in the migration.

## G2 — Submission + CLI transport
**Goal.** A caller can send `github_id` end to end; backend accepts it.
**Changes.**
- `oddish/src/oddish/schemas.py` (~:373): add `github_id: str | None` to `TaskSweepSubmission` next to `github_username`.
- `oddish/src/oddish/cli/run.py` (~:750) and `oddish/src/oddish/cli/api.py` (~:973): thread `github_id` into the payload builders alongside `github_username`.
- Backend `_apply_github_attribution` / tag plumbing in `api/routers/tasks.py`: carry `github_id` if present (no behavior change yet — G4 consumes it).
**Tests.** Flip `test_submission_carries_github_id`; add a CLI/payload test asserting `github_id` survives serialization into the sweep submission.
**Review focus.** Field is optional/back-compat; no required-field break for existing CLIs; idempotency hash still hashes the raw body.

## G3 — Clerk extraction (populate the source)
**Goal.** New/refreshed users get `github_id` from Clerk.
**Changes.**
- `backend/auth/provisioning.py`: extend `_github_account_from_clerk_payload` + `fetch_github_identity_from_clerk` to also read `account["provider_user_id"]`; return it (widen the tuple or a small dataclass — update all call sites). Set `user.github_id` in `_refresh_user_github_identity` / `get_or_create_user_in_org` when present and missing.
**Tests.** Payload parse returns provider_user_id; provisioning sets `github_id`; fail-open unchanged (Clerk error → no crash, github_id stays None).
**Review focus.** Tuple/dataclass change touches several call sites — verify none break; provider_user_id treated as a string; never overwrite a differing existing github_id without review (Q19 collision → reject + manual review).

## G4 — Lookup precedence + endpoint `actor_id`
**Goal.** Resolution prefers immutable github_id over the mutable handle; both
the sweep and the endpoint use it; endpoint accepts `?actor_id=`.
**Changes.**
- `api/routers/tasks.py`: add `_lookup_user_by_github_id` (exact-one, org-scoped); make owner/created_by resolution try github_id first, then handle. **Preserve predicate parity** — factor a shared resolver both the sweep and endpoint call.
- `api/routers/github_linkage.py`: accept optional `actor_id` query param; prefer it over `handle`; keep fail-open + exactly-one.
**Tests.** github_id match beats a stale handle; org-scoped; exact-one; endpoint `?actor_id=` linked/unlinked; parity (endpoint == sweep) for the same github_id.
**Review focus.** Precedence order; org scoping on bare github_id; no double-refresh storm; back-compat when only handle is supplied.

## G5 — Backfill job (populate existing rows)
**Goal.** Existing users with a handle get a github_id, repeatably + throttled.
**Changes.**
- New callable job (e.g. `backend/jobs/backfill_github_id.py` or a `python -m` entry): for active users with `clerk_user_id` and null `github_id`, fetch from Clerk, set github_id; batch + rate-limit/backoff; idempotent; NOT embedded in a migration (preview bootstraps via create_all + stamp-heads and skips data steps).
**Tests.** Populates null github_id; skips already-set; respects a batch limit; tolerates per-user Clerk failure (fail-open).
**Review focus.** Throttle/backoff present; idempotent; no unbounded Clerk fan-out; safe to re-run.

## G6 — User API + FE surface
**Goal.** github_id + link-status visible where the handle already is.
**Changes.**
- `backend/api/schemas.py` (~:26) `UserResponse` + `api/routers/orgs.py` (~:102): add `github_id` (and an explicit link-status only if needed).
- `frontend/src/lib/types.ts` (~:355): add `github_id` to the user type.
- Honor the **list_tasks_core `load_only`** gotcha in CLAUDE.md if any new Task/Trial/Experiment column is surfaced (N/A for a users column, but check if you touch experiment/trial responses).
**Tests.** API returns github_id; FE has no test suite — type-check (`pnpm`/`tsc`) only.
**Review focus.** No leakage of github_id cross-org; FE type matches API; minimal surface.

---

## Definition of done per slice
Red test written/flipped → impl → **full** `test_github_linkage_gate.py` green
(plus any new slice tests) → Codex review clean (or findings addressed) → lessons
doc appended → committed with the `Co-Authored-By` trailer. Final state target:
`test_github_linkage_gate.py` fully green (0 xfailed except the 2 skipped
experiments-repo Action cases).
