# Account-merge linkage gate — lessons learnt (SHARED; read first, append after your slice)

**Every subagent working a slice MUST read this file before starting and append a
short entry after finishing.** It is the running memory across slices so nobody
re-discovers the same trap. Keep entries terse and concrete (file:line, command,
gotcha). Newest at the bottom of each section.

## Canonical references
- Plan: [account-merge-plan.md](account-merge-plan.md) (v7).
- Test cases: [account-merge-test-cases.md](account-merge-test-cases.md).
- Slice breakdown: [account-merge-github-id-slices.md](account-merge-github-id-slices.md).
- Spec-as-tests: `backend/tests/test_github_linkage_gate.py` (xfail markers = not-yet-built).

## How to run the tests (REQUIRED setup — they don't run bare)
1. Local Postgres: `docker run -d --name oddish-db -e POSTGRES_USER=oddish -e POSTGRES_PASSWORD=oddish -e POSTGRES_DB=oddish -p 5432:5432 postgres:16-alpine` (already running if a prior slice started it; `docker ps` to check).
2. `export ODDISH_DATABASE_URL=postgresql+asyncpg://oddish:oddish@localhost:5432/oddish`
3. Build/refresh schema via **create_all**, NOT alembic — the cross-stack chain throws `Cycle is detected in revisions` when run from `oddish/` (a revision id exists in both stacks). Use the helper at `/private/tmp/.../scratchpad/setup_testdb.py` pattern: from `backend/`, `import models` + `from oddish.db.models import Base`, drop+create `public`, `Base.metadata.create_all`. **Re-run this after any model column change** or the new column won't exist in the test DB and `select(UserModel)` 500s with "column does not exist".
4. Run from `backend/`: `uv run pytest tests/test_github_linkage_gate.py -q`.
- Toolchain is `uv run` (there are `.venv`s in `oddish/` and `backend/`). `alembic`/`pytest` are not on bare PATH.

## Traps already hit (don't repeat)
- **Mapper config fails without the conftest FK shim.** `OrganizationModel.api_keys` / `UserModel.api_keys` relate to `api_keys`, whose `org_id`/`created_by_user_id` have NO model-level FK (OSS omits them; cloud adds via migration `a1b2c3d4e5f6`). `configure_mappers()` then raises `NoForeignKeysError` and every backend DB test errors. `backend/tests/conftest.py` appends those FKs (with prod `ondelete`: org_id→CASCADE, created_by_user_id→SET NULL) at import. Leave that shim in place.
- **No backend pytest job in CI.** Only migration/preview workflows touch a DB. So these tests run only locally — don't assume CI catches a DB-test break.
- **xfail(strict) etiquette.** When your slice makes an xfail test pass, you MUST remove the `@pytest.mark.xfail(...)` line, or strict mode turns the unexpected pass into an XPASS failure.
- **Fail-open must catch broadly.** `fetch_github_identity_from_clerk` does `response.json()` *inside* its `except httpx.HTTPError` guard, so a malformed-200 / JSONDecodeError escapes it. Any best-effort Clerk path must `except Exception` (log + continue), not just `httpx.HTTPError`, or it 500s instead of degrading.
- **get_session() auto-commits on clean exit** (`oddish/db/connection.py`) and autoflush makes an in-session re-`select` see pending writes — no explicit commit needed inside the `async with`.

## Design invariants to preserve
- **Predicate parity (INV2):** "connected?" (endpoint) == "resolvable owner?" (`/tasks/sweep`). Both go through the SAME exact-one helper in `api/routers/tasks.py`. If you add a github_id lookup, both sites must adopt the same precedence.
- **Exactly-one:** 0 or 2+ active matches → not-connected / no-owner / None — NEVER an error. Normalization is `@`-strip + `.strip()` + case-insensitive (`func.lower`), org-scoped, `is_active == True`.
- **Org scoping:** never resolve on a bare github_id across orgs — `UniqueConstraint(org_id, github_id)`, lookups always filter `org_id`.
- **Ownership is cosmetic** (the privilege gate is the in-Action push check, not the owner field). `_stamp_experiment_owner` must never be called with None on the exact-one→None path (it no-ops on falsy owner — keep it that way).
- **M2:** the linkage endpoint's Clerk refresh must NOT reuse `ensure_user_github_identity` / `_refresh_user_github_identity` (they early-return when a handle is already set). Use a dedicated, monkeypatchable fetch.
- **M4:** authorize by org + scope; never infer a human identity from an API key (its AuthContext has no user_id).

## Process / repo conventions
- **Codex review between every step**: `codex exec -s read-only -` with a *focused* per-slice diff piped on stdin (NOT `codex review --uncommitted` — the working tree carries unrelated WIP that would add noise/cost). Address findings before committing.
- **Commit per slice** on branch `feat/account-merge-linkage-gate`. Stage only your slice's files (the working tree has unrelated WIP — never `git add -A`). End commit messages with the `Co-Authored-By: Claude Opus 4.8` trailer. Never commit to `main`.
- **House style:** terse, minimal comments — but a one-line WHY is warranted for non-obvious correctness/infra (the user dislikes noise, not justified rationale). Reference files with markdown links in prose.
- **github_id source** = Clerk `provider_user_id` (snake_case in the raw REST JSON; a STRING). Column is TEXT. It is an OPTIONAL correctness upgrade for handle renames/recycles — NOT security-critical.

## Per-slice log (append your entry here)
- **First wave (A0, M1, exact-one, endpoint)** — committed `b619a10c`. All four Codex-reviewed. Added the conftest FK shim + this lessons doc. Suite at handoff: 18 passed / 2 skipped / 2 xfailed (the 2 github_id schema tests).
- **G1 (schema + migration)** — `UserModel.github_id` TEXT/nullable added in `backend/models.py` (~:126) with `UniqueConstraint("org_id","github_id", name="uq_users_org_github_id")` + `Index("idx_users_org_github_id","org_id","github_id")` in `__table_args__`. Migration `backend/alembic/versions/g1h2i3j4k5l6_add_user_github_id.py`: **revision `g1h2i3j4k5l6`, down_revision `s5t6u7v8w9x0`** (current backend head, confirmed via `cd backend && uv run alembic heads` — ran clean, no cross-stack cycle). Raw-SQL idempotent: `ADD COLUMN IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS` + `pg_constraint` existence guard before `ADD CONSTRAINT ... UNIQUE`; downgrade drops constraint→index→column with IF EXISTS. Flipped `test_user_model_has_github_id` (removed xfail) + added pure-metadata `test_user_model_github_id_org_scoped_unique_and_indexed` (no `@requires_db`). Rebuilt test DB via the scratchpad `setup_testdb.py` (verified column/index/constraint present in live PG via `\d users`). Suite: **20 passed / 2 skipped / 1 xfailed** (only `test_submission_carries_github_id` remains xfailed → G2). Ruff clean on the migration + test; the one `E402` ruff flags in `backend/models.py:354` is **pre-existing** (bottom-of-file `register_soft_delete_models` import on unmodified HEAD), not from this slice.
