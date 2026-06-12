# Per-User Default Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an individual user opt into having `run_probe` turned on by default for every task sweep they submit, set via a self-serve toggle in the dashboard.

**Architecture:** Store the preference in a new `settings` JSONB column on the cloud `users` table (mirroring the existing unused `OrganizationModel.settings`). At sweep-submit time, the cloud route resolves the submitting user and OR-ins `run_probe` if their preference is on. A thin `GET`/`PATCH /me/settings` pair (self-serve, `require_auth`) reads/writes the preference; the dashboard settings page renders a checkbox wired through a Next.js BFF route.

**Tech Stack:** SQLAlchemy + Alembic (backend tree), FastAPI, Pydantic, Next.js (App Router) + SWR + shadcn/ui, Clerk auth.

---

## Key semantics (read before starting)

- **OR-in only.** The per-user default can turn `run_probe` *on* but never force it *off*. This mirrors the existing append-path flip in `create_task_sweep_core` (`oddish/src/oddish/core/endpoints.py:2615`), which only flips `False → True`. A `bool` cannot distinguish "user left it unset" from "user explicitly set False", so explicit per-run disable is out of scope.
- **Works for API-key submissions too.** The read path uses the existing `_resolve_actor_user` helper (`backend/api/routers/tasks.py:135`), which resolves an API key to its `created_by_user_id`. So a sweep submitted via API key inherits the key-creator's default.
- **Scope is per-user, not per-org.** This is a deliberate choice (see conversation). There is no org-level default in this plan.
- **Dependency:** end-to-end the feature is a no-op unless the `general-probe` preset row exists (Task 5). Auto-probe looks the preset up by the hardcoded id `"general-probe"` and silently skips if absent (`oddish/src/oddish/core/auto_probe.py:71-86`).

## File structure

- `backend/models.py` — add `settings` JSONB column to `UserModel`.
- `backend/alembic/versions/o1p2q3r4s5t6_add_user_settings.py` — **new** migration (backend tree, head is `n0p1q2r3s4t5`).
- `backend/api/routers/tasks.py` — add `_apply_user_default_run_probe` helper + call it in `create_task_sweep`.
- `backend/api/routers/orgs.py` — add `GET`/`PATCH /me/settings` endpoints + `_merge_user_settings` helper.
- `backend/api/schemas.py` — add `UserSettingsResponse` + `UpdateUserSettingsRequest`.
- `backend/tests/test_user_default_run_probe.py` — **new** unit tests for the two pure helpers.
- `frontend/src/app/api/settings/probe/route.ts` — **new** BFF GET/PATCH proxy.
- `frontend/src/app/(app)/settings/page.tsx` — add the toggle UI.
- `oddish/src/oddish/scripts/seed_probe_presets.py` (or a one-off migration) — ensure the `general-probe` row exists (Task 5).

---

### Task 1: Add `settings` JSONB column to `UserModel`

**Files:**
- Modify: `backend/models.py:140` (UserModel, next to `attribution_cache`)
- Create: `backend/alembic/versions/o1p2q3r4s5t6_add_user_settings.py`

- [ ] **Step 1: Add the column to the model**

In `backend/models.py`, inside `UserModel`, add directly after the `attribution_cache` line (currently line 140):

```python
    # Per-user preferences blob (e.g. {"default_run_probe": true}). Mirrors
    # OrganizationModel.settings. Reassign a new dict on write so SQLAlchemy
    # detects the JSONB mutation.
    settings: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
```

`JSONB` and `mapped_column` are already imported in this file (used by `attribution_cache` / `OrganizationModel.settings`).

- [ ] **Step 2: Confirm the backend alembic head**

Run: `cd backend && set -a && source .env && set +a && uv run alembic heads`
Expected: a single head `n0p1q2r3s4t5 (head)`. If it differs, use the reported id as `down_revision` in Step 3.

- [ ] **Step 3: Create the migration**

Create `backend/alembic/versions/o1p2q3r4s5t6_add_user_settings.py`:

```python
"""add user settings

Revision ID: o1p2q3r4s5t6
Revises: n0p1q2r3s4t5
Create Date: 2026-06-12 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "o1p2q3r4s5t6"
down_revision: Union[str, Sequence[str], None] = "n0p1q2r3s4t5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS settings JSONB "
        "NOT NULL DEFAULT '{}'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS settings")
```

- [ ] **Step 4: Apply and verify the migration**

Run: `cd backend && set -a && source .env && set +a && uv run alembic upgrade head`
Expected: completes without error; `uv run alembic heads` now reports `o1p2q3r4s5t6 (head)`.

- [ ] **Step 5: Commit**

```bash
git add backend/models.py backend/alembic/versions/o1p2q3r4s5t6_add_user_settings.py
git commit -m "feat(backend): add settings JSONB column to users"
```

---

### Task 2: Apply the per-user default at sweep submit

**Files:**
- Modify: `backend/api/routers/tasks.py` (add helper near `_apply_github_attribution` ~line 129; call it in `create_task_sweep` ~line 400)
- Test: `backend/tests/test_user_default_run_probe.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_user_default_run_probe.py`:

```python
"""Unit tests for per-user default-probe helpers (pure, no DB)."""

from __future__ import annotations

from types import SimpleNamespace

from api.routers.tasks import _apply_user_default_run_probe
from oddish.schemas import TaskSweepSubmission


def _submission(run_probe: bool) -> TaskSweepSubmission:
    return TaskSweepSubmission(task_id="t1", name="t1", run_probe=run_probe)


def test_default_on_enables_run_probe():
    sub = _submission(run_probe=False)
    user = SimpleNamespace(settings={"default_run_probe": True})
    _apply_user_default_run_probe(sub, user)
    assert sub.run_probe is True


def test_default_off_leaves_run_probe_false():
    sub = _submission(run_probe=False)
    user = SimpleNamespace(settings={"default_run_probe": False})
    _apply_user_default_run_probe(sub, user)
    assert sub.run_probe is False


def test_default_never_disables_explicit_true():
    sub = _submission(run_probe=True)
    user = SimpleNamespace(settings={})
    _apply_user_default_run_probe(sub, user)
    assert sub.run_probe is True


def test_none_user_is_noop():
    sub = _submission(run_probe=False)
    _apply_user_default_run_probe(sub, None)
    assert sub.run_probe is False


def test_missing_settings_is_noop():
    sub = _submission(run_probe=False)
    user = SimpleNamespace(settings=None)
    _apply_user_default_run_probe(sub, user)
    assert sub.run_probe is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && set -a && source .env && set +a && uv run pytest tests/test_user_default_run_probe.py -v`
Expected: FAIL with `ImportError: cannot import name '_apply_user_default_run_probe'`.

- [ ] **Step 3: Write the helper**

In `backend/api/routers/tasks.py`, add after `_apply_github_attribution` (ends ~line 188):

```python
def _apply_user_default_run_probe(
    submission: TaskSweepSubmission, user: UserModel | None
) -> None:
    """OR-in run_probe when the submitting user opted into default probes.

    Enables run_probe but never disables a per-submission True — mirrors the
    flip-only semantics in create_task_sweep_core.
    """
    if user is None:
        return
    settings = getattr(user, "settings", None) or {}
    if settings.get("default_run_probe") and not submission.run_probe:
        submission.run_probe = True
```

`UserModel` and `TaskSweepSubmission` are already imported in this module (`from models import APIKeyModel, UserModel` at line 51; `TaskSweepSubmission` via the schemas import used by the route signature).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && set -a && source .env && set +a && uv run pytest tests/test_user_default_run_probe.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Wire the helper into the submit route**

In `backend/api/routers/tasks.py`, in `create_task_sweep`, immediately after the existing `_apply_github_attribution(submission)` call (line 401) and before `create_task_sweep_core(...)` (line 403):

```python
        actor = await _resolve_actor_user(session, auth)
        _apply_user_default_run_probe(submission, actor)
```

`_resolve_actor_user` is defined in this same file at line 135.

- [ ] **Step 6: Run the full helper test again + the existing tasks tests**

Run: `cd backend && set -a && source .env && set +a && uv run pytest tests/test_user_default_run_probe.py -v`
Expected: PASS. (The route change has no unit test here; it is verified end-to-end in Task 4's manual check.)

- [ ] **Step 7: Commit**

```bash
git add backend/api/routers/tasks.py backend/tests/test_user_default_run_probe.py
git commit -m "feat(backend): apply per-user default run_probe at sweep submit"
```

---

### Task 3: `GET`/`PATCH /me/settings` endpoints

**Files:**
- Modify: `backend/api/schemas.py` (add two models near `UserResponse` ~line 26)
- Modify: `backend/api/routers/orgs.py` (add helper + two endpoints)
- Test: `backend/tests/test_user_default_run_probe.py` (extend)

- [ ] **Step 1: Write the failing test for the merge helper**

Append to `backend/tests/test_user_default_run_probe.py`:

```python
from api.routers.orgs import _merge_user_settings


def test_merge_sets_flag_on_empty():
    assert _merge_user_settings(None, default_run_probe=True) == {
        "default_run_probe": True
    }


def test_merge_preserves_other_keys():
    merged = _merge_user_settings(
        {"other": 1, "default_run_probe": False}, default_run_probe=True
    )
    assert merged == {"other": 1, "default_run_probe": True}


def test_merge_returns_new_dict():
    original = {"default_run_probe": False}
    merged = _merge_user_settings(original, default_run_probe=True)
    assert merged is not original  # must be a fresh dict so SQLAlchemy sees the change
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && set -a && source .env && set +a && uv run pytest tests/test_user_default_run_probe.py -k merge -v`
Expected: FAIL with `ImportError: cannot import name '_merge_user_settings'`.

- [ ] **Step 3: Add the request/response schemas**

In `backend/api/schemas.py`, after `UserResponse` (ends ~line 36), add:

```python
class UserSettingsResponse(BaseModel):
    default_run_probe: bool


class UpdateUserSettingsRequest(BaseModel):
    default_run_probe: bool | None = None
```

`BaseModel` is already imported at the top of this file.

- [ ] **Step 4: Add the helper + endpoints**

In `backend/api/routers/orgs.py`, update the schema import block (lines 10-15) to include the two new names:

```python
from api.schemas import (
    InviteUserRequest,
    InviteUserResponse,
    OrganizationResponse,
    UpdateUserSettingsRequest,
    UserResponse,
    UserSettingsResponse,
)
```

Add `UserModel` is already imported (line 17). Then add, after the `get_organization` endpoint (ends line 45):

```python
def _merge_user_settings(
    existing: dict | None, *, default_run_probe: bool | None
) -> dict:
    """Return a NEW settings dict with the supplied fields applied.

    A fresh dict (not in-place mutation) is required so SQLAlchemy flags the
    JSONB column as dirty on assignment.
    """
    merged = dict(existing or {})
    if default_run_probe is not None:
        merged["default_run_probe"] = default_run_probe
    return merged


@router.get("/me/settings", response_model=UserSettingsResponse)
async def get_my_settings(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> UserSettingsResponse:
    """Get the current user's preferences."""
    if not auth.user_id:
        raise HTTPException(status_code=404, detail="No user in this context")
    async with get_session() as session:
        user = await session.get(UserModel, auth.user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        settings = user.settings or {}
        return UserSettingsResponse(
            default_run_probe=bool(settings.get("default_run_probe", False))
        )


@router.patch("/me/settings", response_model=UserSettingsResponse)
async def update_my_settings(
    request: UpdateUserSettingsRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> UserSettingsResponse:
    """Update the current user's preferences (self-serve)."""
    if not auth.user_id:
        raise HTTPException(status_code=404, detail="No user in this context")
    async with get_session() as session:
        user = await session.get(UserModel, auth.user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        user.settings = _merge_user_settings(
            user.settings, default_run_probe=request.default_run_probe
        )
        await session.commit()
        settings = user.settings or {}
        return UserSettingsResponse(
            default_run_probe=bool(settings.get("default_run_probe", False))
        )
```

`require_auth`, `AuthContext`, `Depends`, `HTTPException`, `Annotated`, and `get_session` are all already imported in `orgs.py`.

- [ ] **Step 5: Run the merge tests**

Run: `cd backend && set -a && source .env && set +a && uv run pytest tests/test_user_default_run_probe.py -v`
Expected: PASS (8 passed).

- [ ] **Step 6: Smoke-test the endpoints against a local server**

Run the backend (`cd backend && uvicorn api.app:create_app --factory --reload`), then with a valid token:

```bash
curl -s -X PATCH localhost:8000/me/settings -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"default_run_probe": true}'
```

Expected: `{"default_run_probe":true}`. A follow-up `GET /me/settings` returns the same.

- [ ] **Step 7: Commit**

```bash
git add backend/api/schemas.py backend/api/routers/orgs.py backend/tests/test_user_default_run_probe.py
git commit -m "feat(backend): add GET/PATCH /me/settings for per-user preferences"
```

---

### Task 4: Dashboard toggle

**Files:**
- Create: `frontend/src/app/api/settings/probe/route.ts`
- Modify: `frontend/src/app/(app)/settings/page.tsx`

- [ ] **Step 1: Create the BFF proxy route**

Create `frontend/src/app/api/settings/probe/route.ts` (mirrors `frontend/src/app/api/settings/api-keys/route.ts`):

```ts
import { NextRequest, NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

export async function GET() {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);
    if (!token) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }
    const res = await fetch(getBackendUrl("me/settings"), {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}

export async function PATCH(request: NextRequest) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);
    if (!token) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }
    const body = await request.json();
    const res = await fetch(getBackendUrl("me/settings"), {
      method: "PATCH",
      cache: "no-store",
      headers: { ...getAuthHeaders(token), "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
```

- [ ] **Step 2: Add the toggle to the settings page**

In `frontend/src/app/(app)/settings/page.tsx`, add the import near the other `@/components/ui` imports:

```tsx
import { Checkbox } from "@/components/ui/checkbox";
```

Add this state + handler inside the page component (near the other `useState`/`useSWR` hooks). Uses the existing `fetcher` already imported from `@/lib/api`:

```tsx
  const { data: probeSettings } = useSWR<{ default_run_probe: boolean }>(
    "/api/settings/probe",
    fetcher,
  );
  const [savingProbe, setSavingProbe] = useState(false);

  const toggleDefaultProbe = async (checked: boolean) => {
    setSavingProbe(true);
    try {
      await fetch("/api/settings/probe", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ default_run_probe: checked }),
      });
      await mutate("/api/settings/probe");
    } finally {
      setSavingProbe(false);
    }
  };
```

`mutate` is already imported from `swr` at the top of the file.

Then render a card in the settings layout (place it alongside the existing setting cards):

```tsx
        <Card>
          <CardContent className="flex items-center justify-between gap-4 pt-6">
            <div>
              <Label htmlFor="default-probe">Run probes by default</Label>
              <p className="text-sm text-muted-foreground">
                Automatically enqueue a probe trial for every task sweep you submit.
              </p>
            </div>
            <Checkbox
              id="default-probe"
              checked={probeSettings?.default_run_probe ?? false}
              disabled={savingProbe}
              onCheckedChange={(v) => toggleDefaultProbe(v === true)}
            />
          </CardContent>
        </Card>
```

`Card`, `CardContent`, and `Label` are already imported in this file.

- [ ] **Step 3: Verify the frontend builds and lints**

Run: `cd frontend && pnpm lint && pnpm build`
Expected: no type or lint errors in the changed files.

- [ ] **Step 4: Manual end-to-end check**

With backend + frontend running and the `general-probe` preset seeded (Task 5): toggle the checkbox on, submit a task sweep as that user, and confirm a probe trial is enqueued (check the experiment's trials / probe view). Toggle off and confirm no probe is enqueued on a fresh version.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/app/api/settings/probe/route.ts "frontend/src/app/(app)/settings/page.tsx"
git commit -m "feat(frontend): per-user default-probe toggle in settings"
```

---

### Task 5: Ensure the `general-probe` preset exists (prerequisite for end-to-end)

Without a `probe_presets` row whose `id == "general-probe"`, every defaulted submission hits the "preset not found, skipping" path (`oddish/src/oddish/core/auto_probe.py:76`). The existing seed script (`oddish/src/oddish/scripts/seed_probe_presets.py`) upserts on `(org_id, name)` and lets `id` auto-generate, so it does **not** by itself create the fixed-id row.

- [ ] **Step 1: Check whether the row already exists in the target DB**

Run against the target database:

```bash
psql "$DATABASE_URL" -c "select id, name, agent, model from probe_presets where id = 'general-probe';"
```

Expected: if one row returns, this task is already satisfied — skip to Step 3. If zero rows, continue.

- [ ] **Step 2: Insert the fixed-id preset row**

Decide the content values with the team (these are product decisions): `agent`, `model`, and `operator_prompt` are `NOT NULL`; `name` is `NOT NULL`; the rest are nullable (`oddish/src/oddish/db/models.py:1114-1124`). Insert idempotently:

```bash
psql "$DATABASE_URL" <<'SQL'
INSERT INTO probe_presets (id, org_id, name, agent, model, operator_prompt, is_seed, created_at, updated_at)
VALUES (
  'general-probe',
  NULL,
  'General Probe',
  '<agent>',            -- e.g. the standard probe agent slug used in prod
  '<model>',            -- a sane default; auto-probe rotates the model per version regardless
  '<operator_prompt>',  -- the directive prepended to instruction.md for probes
  true,
  now(),
  now()
)
ON CONFLICT (id) DO NOTHING;
SQL
```

`org_id = NULL` + `is_seed = true` makes it a global seed preset, matching the model's documented convention.

- [ ] **Step 3: Verify auto-probe resolves it**

With Task 1–2 deployed and a user's `default_run_probe` on, submit a sweep and confirm the server logs do **not** contain `Default probe preset 'general-probe' not found` and that a probe trial appears for the new version.

- [ ] **Step 4 (optional hardening): commit the seed in code**

To stop this from being a manual prod step, either (a) add `general-probe` to `SEED_PRESETS` in `seed_probe_presets.py` and extend `upsert_preset` to set the fixed `id` for seed rows, or (b) add a data migration in the **oddish** alembic tree (head discovered via `cd oddish && uv run alembic heads`) that runs the `INSERT ... ON CONFLICT DO NOTHING` above. Commit whichever you choose.

---

## Self-review notes

- **Spec coverage:** column (Task 1) → read/apply at submit (Task 2) → write API (Task 3) → frontend toggle (Task 4) → seeding dependency (Task 5). All four chosen requirements (per-user scope, JSONB storage, admin/self-serve API, frontend toggle) are covered.
- **Type consistency:** `_apply_user_default_run_probe(submission, user)`, `_merge_user_settings(existing, *, default_run_probe)`, `UserSettingsResponse.default_run_probe`, and `UpdateUserSettingsRequest.default_run_probe` are referenced identically across tasks and tests.
- **Known limitation (documented):** because `run_probe` is a plain `bool`, the per-user default cannot force-disable a per-submission `True`; it only OR-ins. If product later needs explicit per-run opt-out, `run_probe` must become tri-state (`bool | None`) — out of scope here.
