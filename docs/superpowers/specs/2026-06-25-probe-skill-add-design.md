# Design: `oddish probe skill add` — upload a skill from the CLI

**Date:** 2026-06-25
**Status:** Approved (ready for implementation planning)

## Problem

Skills live in a Postgres-backed skills DB (`SkillModel` / `SkillFileModel`) and
are managed only via the `POST /skills` HTTP endpoint and the web dashboard
(`/qa/skills`). There is **no CLI path** to add a skill. At run time, every org
skill (plus global seeds) is auto-staged into *every* trial — including probes —
via `stage_org_skills` (`oddish/src/oddish/workers/harbor/runner.py`), so adding a
skill to the DB is all it takes to make it available to the agent. We want a
simple CLI command to create that DB entry.

## Goals

- `oddish probe skill add <dir>` packages a local skill folder and creates it in
  the org's skills DB via the existing `POST /skills` endpoint.
- On a name collision within the org, the server stores the skill under a
  version-suffixed name (`my-skill` → `my-skill-2` → `my-skill-3`) instead of
  failing. This logic lives **server-side**, where the uniqueness constraint is
  enforced, so the web UI inherits the same behavior.
- Preserve the existing `oddish probe --task … --instructions …` invocation
  unchanged.

## Non-goals

- No concurrency/race handling. Collisions are rare in practice; if two creates
  genuinely race for the same suffix, the second hits the unique constraint and
  errors. We accept that. No `IntegrityError` retry loop.
- No `skill ls` / `skill rm` CLI subcommands in v1 (the group leaves room to add
  them later).
- No per-probe skill *selection* — skills remain org-wide and auto-stage into all
  trials. This spec only covers *adding* a skill.
- No new schema fields on `SkillCreate` / `SkillResponse`.

## Background facts (verified)

- `parse_skill` (`oddish/src/oddish/core/skills.py:20`) derives `(name,
  description)` from the root `SKILL.md` YAML frontmatter and 422s if missing.
- The stored skill `name` is the source of truth for the run-time directory:
  skills materialize as `agent_skills/<name>/<relative_path>` and the agent
  registers them by that name. **For two versions to be distinct to the agent,
  the versioned name must be written into the `SKILL.md` frontmatter**, not just
  the DB column.
- Uniqueness is enforced by a partial unique index
  `idx_skills_unique_org_name` on `(COALESCE(org_id, ''), name) WHERE deleted_at
  IS NULL` (`oddish/src/oddish/db/models.py`). Seeds (`org_id` NULL) sit in the
  `''` bucket; a hosted org's skills sit in their own `org_id` bucket, so a
  custom skill never collides with a seed of the same name.
- `POST /skills` (`backend/api/routers/skills.py:48`) requires API key scope
  `TASKS` and returns a `SkillResponse` that already includes the stored `name`.
- All CLI commands are flat top-level Typer commands; `probe`
  (`oddish/src/oddish/cli/probe.py`) is currently a single command.

## Design

### 1. Server: auto-version on name conflict (`create_skill_core`)

In `create_skill_core` (`oddish/src/oddish/core/skills.py:95`), after
`parse_skill` derives the base name:

1. **Resolve a free name.** Query live skills in the *same uniqueness bucket* as
   the constraint — `COALESCE(org_id, '') == COALESCE(creating_org_id, '')` and
   `deleted_at IS NULL` — and collect existing names. Pick the smallest free name
   in the sequence `base`, `base-2`, `base-3`, … (`base` itself if free).
2. **Rewrite frontmatter if bumped.** If the resolved name differs from the
   parsed base name, rewrite the `name:` value inside the root `SKILL.md`
   frontmatter so the stored file content matches the stored DB name. Use a
   targeted replacement of the `name:` line within the frontmatter block (not a
   full YAML re-dump) to preserve the rest of the file's formatting. The
   `description` is untouched.
3. **Insert.** Set `SkillModel.name` to the resolved name and persist the
   (possibly rewritten) files. Plain insert, no retry loop.

This is the default behavior of create — no opt-in flag. The prior behavior on
collision was an unhandled DB error (effectively a 500), so silently storing a
versioned name and returning it is strictly better. The web UI, which also calls
`POST /skills`, displays whatever `name` comes back in the response.

### 2. CLI: restructure `probe` into a Typer group

Convert `probe` from a single command into a `typer.Typer` sub-app:

- A callback decorated `@probe_app.callback(invoke_without_command=True)` holds
  today's probe options. It runs the probe **only** when no subcommand was
  invoked (`ctx.invoked_subcommand is None`). The previously-required `--task`
  becomes optional at the Typer layer and is validated inside the callback body
  when running a probe, so `oddish probe skill add …` does not demand `--task`.
- A nested `skill` Typer group is mounted on the probe app, with a single `add`
  subcommand.

Registration in `oddish/src/oddish/cli/__init__.py` changes from
`app.command()(probe)` to mounting the probe sub-app
(`app.add_typer(probe_app, name="probe")`).

**Backward compatibility:** `oddish probe --task t --instructions "…"` resolves to
the callback with no subcommand and behaves exactly as before.

### 3. CLI: `oddish probe skill add <dir>`

- **Read the folder.** Walk `<dir>` recursively, building a list of
  `SkillFile(relative_path, content)` with POSIX-style relative paths. Skip junk
  entries: `.git/`, `__pycache__/`, `*.pyc`, `.DS_Store`.
- **Local validation.** Require a root `SKILL.md`; if absent, error before any
  network call (the server also enforces this with a 422).
- **Submit.** `POST {api_url}/skills` via `httpx` with `get_auth_headers()`.
  `SkillCreate` requires top-level `name`, `description`, and `files`, so the CLI
  parses the root `SKILL.md` frontmatter locally to fill `name`/`description`
  (the same fields the server re-derives via `parse_skill`; the server treats the
  frontmatter as authoritative and ignores any mismatch, so these are effectively
  just to satisfy schema validation). Reuse the API-URL/auth resolution
  (`get_api_url`, `require_api_key`) already used by `probe`.
- **Report.** On success print the stored name and id, e.g.
  `Added skill 'my-skill-2' (skill_abc123)`, making any version bump visible.

The command stays "dumb": it packages files and POSTs. All naming logic is
server-side.

## Data flow

```
oddish probe skill add ./my-skill
  └─ walk dir → [SkillFile(SKILL.md, …), SkillFile(scripts/run.sh, …)]
  └─ POST /skills  (SkillCreate{name, description, files})
       └─ create_skill_core
            ├─ parse_skill → ("my-skill", desc)
            ├─ resolve free name in org bucket → "my-skill-2"
            ├─ rewrite SKILL.md frontmatter name: my-skill-2
            └─ insert SkillModel(name="my-skill-2", files=…)
       └─ 200 SkillResponse{name:"my-skill-2", id:"skill_abc"}
  └─ print: Added skill 'my-skill-2' (skill_abc)

later, any trial/probe for this org:
  stage_org_skills → agent_skills/my-skill-2/SKILL.md (+ files) → agent registers it
```

## Error handling

- Missing root `SKILL.md`: CLI errors locally; server returns 422 as a backstop.
- Invalid frontmatter (no `name`/`description`, bad YAML): server 422 surfaced by
  the CLI as a readable message.
- Missing/invalid API key: handled by existing `require_api_key`.
- Name-resolution + frontmatter rewrite happen in one synchronous create path; a
  true concurrent collision is out of scope (see Non-goals) and would surface as
  the underlying DB error.

## Testing

**Core (`oddish` package):**
- Base name free → stored unchanged.
- Base name taken → stored as `base-2`; taken again → `base-3`.
- Gap filling: with `base` and `base-3` present, next is `base-2`.
- Seed with same name does not trigger a bump for an org (different bucket).
- Frontmatter `name:` is actually rewritten in the stored `SKILL.md` when bumped,
  and `description` is preserved.

**CLI:**
- Folder packaging produces the expected `SkillFile` list with POSIX relative
  paths and junk filtered out.
- Missing `SKILL.md` errors before any request.
- Output shows the stored (possibly versioned) name returned by the server.
- `oddish probe` with no subcommand still runs a probe (callback path), and
  `--task` is still required for that path.

## Files touched

- `oddish/src/oddish/core/skills.py` — name resolution + frontmatter rewrite in
  `create_skill_core` (plus a small helper for each).
- `oddish/src/oddish/cli/probe.py` — restructure into a group; add `skill add`.
- `oddish/src/oddish/cli/__init__.py` — mount the probe sub-app.
- Tests under `oddish/tests/` (core + CLI).

No backend router, schema, DB model, or frontend changes are required.
