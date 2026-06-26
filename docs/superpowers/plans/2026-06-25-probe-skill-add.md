# `oddish probe skill add` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a CLI command `oddish probe skill add <dir>` that uploads a local skill folder to the org's skills DB, with the server auto-versioning the name on collision.

**Architecture:** Server change is confined to `create_skill_core` (`oddish/src/oddish/core/skills.py`): resolve a collision-free name within the org's uniqueness bucket and rewrite the `SKILL.md` frontmatter `name:` to match. CLI change restructures the flat `probe` command into a Typer group whose `invoke_without_command=True` callback preserves today's `oddish probe --task …` behavior, plus a nested `skill` group with one `add` subcommand that packages a folder and POSTs to the existing `/skills` endpoint.

**Tech Stack:** Python 3.11, Typer (CLI), httpx (HTTP), SQLAlchemy async (DB), PyYAML, pytest + pytest-asyncio.

## Global Constraints

- **Layering:** core functions receive an `AsyncSession` and **never commit** — the calling router owns the transaction. (`oddish/src/oddish/core/skills.py` docstring.)
- **Soft-delete:** `SkillModel` is registered in `register_soft_delete_models`, so every ORM SELECT auto-applies `WHERE deleted_at IS NULL`. Do **not** add a manual `deleted_at` filter.
- **Uniqueness bucket:** the unique index is `(COALESCE(org_id, ''), name) WHERE deleted_at IS NULL`. Name resolution must scope to `COALESCE(org_id, '') == COALESCE(creating_org_id, '')` so a seed (`org_id` NULL) never collides with a hosted org's skill.
- **No new schema/model/frontend changes.** `SkillCreate`/`SkillResponse`/`SkillModel` are unchanged.
- **Backward compatibility:** `oddish probe --task t --instructions "…"` must behave exactly as before.
- **Comments:** match the existing sparse comment density; explain only non-obvious *why*.
- **Core skills tests** run against a real Postgres: from `backend/`, `set -a && source .env && set +a && uv run pytest tests/test_skills.py`. **CLI tests** are pure (mock httpx) and run from `oddish/`: `uv run pytest tests/<file>`.

---

### Task 1: Pure helper — rewrite `SKILL.md` frontmatter name

**Files:**
- Modify: `oddish/src/oddish/core/skills.py` (add `import re`; add `_rewrite_skill_name`)
- Test: `backend/tests/test_skills.py` (add a DB-free unit test)

**Interfaces:**
- Produces: `_rewrite_skill_name(files: list[SkillFile], new_name: str) -> list[SkillFile]` — returns a new list where the root `SKILL.md`'s frontmatter `name:` line is set to `new_name`; all other files and the file body are unchanged. Assumes `files` already passed `parse_skill` (valid, closed frontmatter).

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_skills.py`:

```python
from oddish.core.skills import _rewrite_skill_name


def test_rewrite_skill_name_updates_frontmatter_only():
    files = [
        SkillFile(
            relative_path="SKILL.md",
            content="---\nname: my-skill\ndescription: does a thing\n---\n\n# Body\nkeep me\n",
        ),
        SkillFile(relative_path="scripts/run.sh", content="echo hi"),
    ]
    out = _rewrite_skill_name(files, "my-skill-2")
    skill_md = next(f for f in out if f.relative_path == "SKILL.md")
    assert "name: my-skill-2" in skill_md.content
    assert "description: does a thing" in skill_md.content  # untouched
    assert "# Body\nkeep me" in skill_md.content  # body untouched
    # other files pass through unchanged
    assert next(f for f in out if f.relative_path == "scripts/run.sh").content == "echo hi"
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `backend/`): `set -a && source .env && set +a && uv run pytest tests/test_skills.py::test_rewrite_skill_name_updates_frontmatter_only -v`
Expected: FAIL with `ImportError`/`AttributeError: cannot import name '_rewrite_skill_name'`.

- [ ] **Step 3: Write minimal implementation**

In `oddish/src/oddish/core/skills.py`, add `import re` to the imports block (after `import yaml`), and add this function below `parse_skill`:

```python
def _rewrite_skill_name(files: list[SkillFile], new_name: str) -> list[SkillFile]:
    """Return ``files`` with the root SKILL.md frontmatter ``name:`` set to
    ``new_name`` (used when a collision forces a version-suffixed name).

    Targeted line edit, not a YAML re-dump, so the rest of the file's
    formatting is preserved. Assumes ``files`` already passed ``parse_skill``.
    """
    out: list[SkillFile] = []
    for f in files:
        if f.relative_path != "SKILL.md":
            out.append(f)
            continue
        stripped = f.content.lstrip()
        prefix = f.content[: len(f.content) - len(stripped)]
        _, frontmatter, body = stripped.split(_FRONTMATTER_DELIM, 2)
        frontmatter = re.sub(
            r"(?m)^(\s*name:).*$",
            lambda m: f"{m.group(1)} {new_name}",
            frontmatter,
            count=1,
        )
        rebuilt = (
            prefix
            + _FRONTMATTER_DELIM
            + frontmatter
            + _FRONTMATTER_DELIM
            + body
        )
        out.append(SkillFile(relative_path=f.relative_path, content=rebuilt))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `set -a && source .env && set +a && uv run pytest tests/test_skills.py::test_rewrite_skill_name_updates_frontmatter_only -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/core/skills.py backend/tests/test_skills.py
git commit -m "feat(skills): add _rewrite_skill_name frontmatter helper"
```

---

### Task 2: Server — auto-version on name collision in `create_skill_core`

**Files:**
- Modify: `oddish/src/oddish/core/skills.py` (change `from sqlalchemy import or_, select` → `from sqlalchemy import func, or_, select`; add `_resolve_skill_name`; rewire `create_skill_core`)
- Test: `backend/tests/test_skills.py`

**Interfaces:**
- Consumes: `_rewrite_skill_name` (Task 1), `parse_skill` (existing).
- Produces: `_resolve_skill_name(session: AsyncSession, base_name: str, *, org_id: str | None) -> str` — smallest free name in the sequence `base`, `base-2`, `base-3`, … within the org's uniqueness bucket. `create_skill_core` now stores that resolved name and rewrites frontmatter when it differs from `base_name`.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_skills.py` (the existing `_payload()` helper builds a skill named `my-skill`; the existing `org_id` fixture cleans up rows after each test):

```python
@pytest.mark.asyncio
async def test_create_versions_on_collision(org_id):
    async with get_session() as session:
        first = await create_skill_core(session, data=_payload(), org_id=org_id)
        await session.commit()
        assert first.name == "my-skill"

    async with get_session() as session:
        second = await create_skill_core(session, data=_payload(), org_id=org_id)
        await session.commit()
        assert second.name == "my-skill-2"
        skill_md = next(f for f in second.files if f.relative_path == "SKILL.md")
        assert "name: my-skill-2" in skill_md.content  # frontmatter rewritten

    async with get_session() as session:
        third = await create_skill_core(session, data=_payload(), org_id=org_id)
        await session.commit()
        assert third.name == "my-skill-3"


@pytest.mark.asyncio
async def test_create_fills_version_gap(org_id):
    # Seed names "my-skill" and "my-skill-3"; the next create should fill "-2".
    async with get_session() as session:
        await create_skill_core(session, data=_payload(), org_id=org_id)  # my-skill
        await create_skill_core(session, data=_payload(), org_id=org_id)  # my-skill-2
        await create_skill_core(session, data=_payload(), org_id=org_id)  # my-skill-3
        await session.commit()
    async with get_session() as session:
        await session.execute(
            SkillModel.__table__.delete().where(
                SkillModel.org_id == org_id, SkillModel.name == "my-skill-2"
            )
        )
        await session.commit()
    async with get_session() as session:
        filler = await create_skill_core(session, data=_payload(), org_id=org_id)
        await session.commit()
        assert filler.name == "my-skill-2"


@pytest.mark.asyncio
async def test_seed_name_does_not_block_org(org_id):
    other_org = f"org_sk_{uuid.uuid4().hex[:8]}"
    try:
        async with get_session() as session:
            await create_skill_core(session, data=_payload(), org_id=other_org)
            await session.commit()
        async with get_session() as session:
            mine = await create_skill_core(session, data=_payload(), org_id=org_id)
            await session.commit()
            assert mine.name == "my-skill"  # different bucket, no bump
    finally:
        async with get_session() as session:
            await session.execute(
                SkillModel.__table__.delete().where(SkillModel.org_id == other_org)
            )
            await session.commit()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `set -a && source .env && set +a && uv run pytest tests/test_skills.py -k "collision or gap or seed_name" -v`
Expected: FAIL — `second.name` is currently `my-skill` (no versioning) or an `IntegrityError` on the duplicate insert.

- [ ] **Step 3: Write minimal implementation**

In `oddish/src/oddish/core/skills.py`, update the SQLAlchemy import line:

```python
from sqlalchemy import func, or_, select
```

Add `_resolve_skill_name` above `create_skill_core`:

```python
async def _resolve_skill_name(
    session: AsyncSession, base_name: str, *, org_id: str | None
) -> str:
    """Smallest free name in ``base``, ``base-2``, ``base-3``, … within the
    org's uniqueness bucket (matching ``idx_skills_unique_org_name``). The
    soft-delete auto-filter excludes tombstoned rows, so reused names are free.
    """
    result = await session.execute(
        select(SkillModel.name).where(
            func.coalesce(SkillModel.org_id, "") == (org_id or "")
        )
    )
    existing = {row[0] for row in result.all()}
    if base_name not in existing:
        return base_name
    n = 2
    while f"{base_name}-{n}" in existing:
        n += 1
    return f"{base_name}-{n}"
```

Rewrite the body of `create_skill_core` (keep its signature and docstring) so the name is resolved and frontmatter rewritten when bumped:

```python
async def create_skill_core(
    session: AsyncSession,
    *,
    data: SkillCreate,
    org_id: str | None = None,
    user_id: str | None = None,
) -> SkillModel:
    """Create a custom skill owned by ``org_id``, validating its SKILL.md.

    On a name collision within the org, the skill is stored under a
    version-suffixed name (``my-skill`` → ``my-skill-2`` → …) and its SKILL.md
    frontmatter ``name:`` is rewritten to match.
    """
    base_name, description = parse_skill(data.files)
    _validate_result_focus(data.result_focus)
    name = await _resolve_skill_name(session, base_name, org_id=org_id)
    files = data.files if name == base_name else _rewrite_skill_name(data.files, name)
    skill = SkillModel(
        org_id=org_id,
        created_by_user_id=user_id,
        name=name,
        description=description,
        is_seed=False,
        operator_prompt=data.operator_prompt,
        result_focus=data.result_focus,
        evaluation_metric=data.evaluation_metric,
        files=[
            SkillFileModel(relative_path=f.relative_path, content=f.content)
            for f in files
        ],
    )
    session.add(skill)
    await session.flush()
    return skill
```

> **Note (base updated):** `main` already includes the unify-skills work, so
> `create_skill_core` sets `operator_prompt`/`result_focus`/`evaluation_metric`
> and calls `_validate_result_focus`. This task **adds** name resolution +
> frontmatter rewrite *around* that existing logic — preserve those lines.

- [ ] **Step 4: Run tests to verify they pass**

Run: `set -a && source .env && set +a && uv run pytest tests/test_skills.py -v`
Expected: PASS (new tests + existing skills tests).

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/core/skills.py backend/tests/test_skills.py
git commit -m "feat(skills): auto-version skill name on collision"
```

---

### Task 3: CLI — restructure `probe` into a Typer group (behavior preserved)

**Files:**
- Modify: `oddish/src/oddish/cli/probe.py` (wrap `probe` as a group callback)
- Modify: `oddish/src/oddish/cli/__init__.py` (mount the probe sub-app)
- Test: `oddish/tests/test_cli_probe.py` (create)

**Interfaces:**
- Produces: `probe_app: typer.Typer` exported from `oddish/src/oddish/cli/probe.py`. Its `invoke_without_command=True` callback runs a probe when no subcommand is given; `--task` is validated in the callback body (not required at the Typer layer).
- Consumes (unchanged): `submit_sweep`, `watch_task` from `oddish.cli.api`.

- [ ] **Step 1: Write the failing test**

Create `oddish/tests/test_cli_probe.py`:

```python
from unittest.mock import patch

from typer.testing import CliRunner

from oddish.cli import app


def _set_env(monkeypatch):
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    monkeypatch.setenv("ODDISH_API_URL", "https://api.example.test")


def test_probe_without_subcommand_still_runs_probe(monkeypatch):
    _set_env(monkeypatch)
    captured = {}

    def _fake_submit_sweep(*, api_url, task_id, extra_instructions, **kwargs):
        captured["task_id"] = task_id
        captured["extra_instructions"] = extra_instructions
        return {"id": task_id, "new_trial_ids": ["tr1"], "trials_count": 1}

    with patch("oddish.cli.probe.submit_sweep", _fake_submit_sweep):
        result = CliRunner().invoke(
            app,
            ["probe", "--task", "task_123", "--instructions", "look at flakiness", "--background"],
        )

    assert result.exit_code == 0, result.output
    assert captured["task_id"] == "task_123"
    assert captured["extra_instructions"] == "look at flakiness"
    assert "Probe queued" in result.output


def test_probe_requires_task(monkeypatch):
    _set_env(monkeypatch)
    result = CliRunner().invoke(
        app, ["probe", "--instructions", "x", "--background"]
    )
    assert result.exit_code == 1
    assert "task" in result.output.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `oddish/`): `uv run pytest tests/test_cli_probe.py -v`
Expected: FAIL — currently `probe` is a flat command requiring `--task` at the parse layer, so `test_probe_requires_task` exits with Typer's usage error (exit code 2, not 1), and the import/structure for a group callback does not yet exist.

- [ ] **Step 3: Write minimal implementation**

In `oddish/src/oddish/cli/probe.py`, after the imports, introduce the group and convert `probe` into its callback. Replace the `def probe(` signature line and add a `ctx` param + an early return for subcommands; make `task_id` optional and validate it in the body. Concretely:

Add near the top (after `console = Console()`):

```python
probe_app = typer.Typer(
    help="Queue probe trials against a task, and manage org skills.",
    no_args_is_help=False,
)
```

Change the decorator/signature. The function currently starts:

```python
def probe(
    task_id: Annotated[
        str,
        typer.Option(
            "--task",
            help="Existing task ID to queue the probe against.",
        ),
    ],
```

Replace with:

```python
@probe_app.callback(invoke_without_command=True)
def probe(
    ctx: typer.Context,
    task_id: Annotated[
        Optional[str],
        typer.Option(
            "--task",
            help="Existing task ID to queue the probe against.",
        ),
    ] = None,
```

Then immediately inside the function body (before `if not api_url:`), add the subcommand short-circuit and required-`--task` validation:

```python
    if ctx.invoked_subcommand is not None:
        return
    if not task_id:
        error_console.print(
            "[red]--task is required to queue a probe.[/red]"
        )
        raise typer.Exit(1)
```

Leave the rest of the function body unchanged.

In `oddish/src/oddish/cli/__init__.py`, change the import and registration:

```python
from oddish.cli.probe import probe_app
```

and replace `app.command()(probe)` with:

```python
app.add_typer(probe_app, name="probe")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli_probe.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/cli/probe.py oddish/src/oddish/cli/__init__.py oddish/tests/test_cli_probe.py
git commit -m "refactor(cli): make probe a Typer group, preserve probe behavior"
```

---

### Task 4: CLI — `oddish probe skill add <dir>`

**Files:**
- Modify: `oddish/src/oddish/cli/probe.py` (add `skill_app`, `skill_add`, and folder/frontmatter helpers)
- Test: `oddish/tests/test_cli_probe_skill.py` (create)

**Interfaces:**
- Consumes: `probe_app` (Task 3), `get_api_url`, `require_api_key`, `get_auth_headers`, `error_console` (config), `httpx`.
- Produces: `oddish probe skill add <dir>` — packages a folder into `{name, description, files}`, POSTs to `/skills`, prints `Added skill '<name>' (<id>)`. Internal helpers `_collect_skill_files(directory: Path) -> list[dict]` and `_parse_skill_meta(skill_md: str) -> tuple[str, str]`.

- [ ] **Step 1: Write the failing tests**

Create `oddish/tests/test_cli_probe_skill.py`:

```python
from pathlib import Path

import httpx
from typer.testing import CliRunner

from oddish.cli import app
from oddish.cli.probe import _collect_skill_files


def _set_env(monkeypatch):
    monkeypatch.setenv("ODDISH_API_KEY", "ok_test")
    monkeypatch.setenv("ODDISH_API_URL", "https://api.example.test")


def _write_skill(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: does a thing\n---\n\n# My Skill\n"
    )
    (root / "scripts").mkdir()
    (root / "scripts" / "run.sh").write_text("echo hi")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "junk.pyc").write_text("nope")
    (root / ".DS_Store").write_text("nope")


def test_collect_skill_files_filters_junk_and_uses_posix(tmp_path):
    _write_skill(tmp_path / "skill")
    files = _collect_skill_files(tmp_path / "skill")
    paths = sorted(f["relative_path"] for f in files)
    assert paths == ["SKILL.md", "scripts/run.sh"]


def test_skill_add_missing_skill_md_errors(tmp_path, monkeypatch):
    _set_env(monkeypatch)
    (tmp_path / "empty").mkdir()
    result = CliRunner().invoke(app, ["probe", "skill", "add", str(tmp_path / "empty")])
    assert result.exit_code == 1
    assert "SKILL.md" in result.output


class _FakeClient:
    last_request: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, json=None):
        _FakeClient.last_request = {"url": url, "json": json}
        return httpx.Response(
            200, json={"id": "skill_abc", "name": "my-skill-2"}
        )


def test_skill_add_posts_and_reports_stored_name(tmp_path, monkeypatch):
    _set_env(monkeypatch)
    _write_skill(tmp_path / "skill")
    _FakeClient.last_request = {}
    monkeypatch.setattr(httpx, "Client", _FakeClient)

    result = CliRunner().invoke(app, ["probe", "skill", "add", str(tmp_path / "skill")])

    assert result.exit_code == 0, result.output
    req = _FakeClient.last_request
    assert req["url"] == "https://api.example.test/skills"
    assert req["json"]["name"] == "my-skill"
    assert req["json"]["description"] == "does a thing"
    assert sorted(f["relative_path"] for f in req["json"]["files"]) == [
        "SKILL.md",
        "scripts/run.sh",
    ]
    assert "my-skill-2" in result.output  # server's stored (versioned) name
    assert "skill_abc" in result.output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_probe_skill.py -v`
Expected: FAIL — `cannot import name '_collect_skill_files'` and the `skill add` subcommand does not exist (exit code 2 "No such command").

- [ ] **Step 3: Write minimal implementation**

In `oddish/src/oddish/cli/probe.py`, add `import yaml` and `from pathlib import Path` to the imports, and `httpx`. Add the helpers and subcommand (after the `probe` callback):

```python
_SKIP_DIRS = {".git", "__pycache__"}
_SKIP_FILES = {".DS_Store"}


def _collect_skill_files(directory: Path) -> list[dict]:
    """Walk ``directory`` into a list of ``{relative_path, content}`` entries,
    skipping VCS/cache junk. Paths are POSIX-style relative to the root."""
    files: list[dict] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(directory)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        if path.name in _SKIP_FILES or path.suffix == ".pyc":
            continue
        files.append({"relative_path": rel.as_posix(), "content": path.read_text()})
    return files


def _parse_skill_meta(skill_md: str) -> tuple[str, str]:
    """Extract (name, description) from SKILL.md frontmatter for the request
    body. The server re-validates and is authoritative; this fails fast for a
    nicer local error."""
    text = skill_md.lstrip()
    parts = text.split("---", 2)
    if not text.startswith("---") or len(parts) < 3:
        error_console.print("[red]SKILL.md must start with closed YAML frontmatter (---).[/red]")
        raise typer.Exit(1)
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        error_console.print("[red]SKILL.md frontmatter is not valid YAML.[/red]")
        raise typer.Exit(1)
    name = meta.get("name") if isinstance(meta, dict) else None
    description = meta.get("description") if isinstance(meta, dict) else None
    if not isinstance(name, str) or not name:
        error_console.print("[red]SKILL.md frontmatter is missing 'name'.[/red]")
        raise typer.Exit(1)
    if not isinstance(description, str) or not description:
        error_console.print("[red]SKILL.md frontmatter is missing 'description'.[/red]")
        raise typer.Exit(1)
    return name, description


skill_app = typer.Typer(
    help="Manage org skills (auto-staged into every trial).",
    no_args_is_help=True,
)
probe_app.add_typer(skill_app, name="skill")


@skill_app.command("add")
def skill_add(
    directory: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            help="Path to the skill folder (must contain a root SKILL.md).",
        ),
    ],
    api_url: Annotated[
        str,
        typer.Option("--api", help="API URL (defaults to ODDISH_API_URL)."),
    ] = "",
):
    """Upload a local skill folder to your org's skills DB.

    EXAMPLES:

        oddish probe skill add ./my-skill
    """
    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    files = _collect_skill_files(directory)
    skill_md = next((f for f in files if f["relative_path"] == "SKILL.md"), None)
    if skill_md is None:
        error_console.print(
            "[red]No SKILL.md found in the skill directory root.[/red]"
        )
        raise typer.Exit(1)
    name, description = _parse_skill_meta(skill_md["content"])

    payload = {"name": name, "description": description, "files": files}
    with httpx.Client(timeout=60.0, headers=get_auth_headers(api_url)) as client:
        response = client.post(f"{api_url}/skills", json=payload)
    if response.status_code != 200:
        error_console.print(f"[red]Failed to add skill:[/red] {response.text}")
        raise typer.Exit(1)

    result = response.json()
    console.print(
        f"[bold green]Added skill[/bold green] '{result['name']}' ({result['id']})"
    )
```

Also add the imports at the top of the file: `import httpx`, `import yaml`, and `from pathlib import Path` (alongside the existing `from typing import Annotated, Optional`). Add `get_api_url` is already imported; ensure `get_auth_headers` is imported from `oddish.cli.config` (add it to the existing import list).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_probe_skill.py -v`
Expected: PASS (all three tests).

- [ ] **Step 5: Run the full CLI test suite + commit**

Run: `uv run pytest tests/test_cli_probe.py tests/test_cli_probe_skill.py -v`
Expected: PASS

```bash
git add oddish/src/oddish/cli/probe.py oddish/tests/test_cli_probe_skill.py
git commit -m "feat(cli): add 'oddish probe skill add' to upload skills"
```

---

## Self-Review

**Spec coverage:**
- Server auto-version on collision → Task 2. ✓
- Frontmatter `name:` rewrite on bump → Task 1 (helper) + Task 2 (wired in). ✓
- Smallest-free-integer naming (`base`, `-2`, `-3`, gap fill) → Task 2 tests. ✓
- Same-bucket scoping (seed doesn't block org) → Task 2 `test_seed_name_does_not_block_org`. ✓
- No `IntegrityError` retry loop → not implemented (per spec non-goal). ✓
- `probe` → Typer group, `--task …` preserved → Task 3. ✓
- `oddish probe skill add <dir>` packaging, junk filter, POSIX paths, root-`SKILL.md` requirement, name/description from frontmatter, reports stored name → Task 4. ✓
- No schema/model/frontend changes → none in any task. ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code; every test step has assertions. ✓

**Type consistency:** `_rewrite_skill_name(files, new_name)` defined in Task 1, consumed in Task 2. `_resolve_skill_name(session, base_name, *, org_id)` defined and used in Task 2. `_collect_skill_files(directory) -> list[dict]` and `_parse_skill_meta(content) -> tuple[str,str]` defined and used in Task 4. `probe_app` defined in Task 3, consumed in Task 4. POST body keys (`name`/`description`/`files`) match `SkillCreate`. ✓
