# Unify Skills & Probe Presets into one "Skills" feature

**Date:** 2026-06-25
**Status:** Approved (design), pending implementation plan
**Branch:** `feat/unify-skills-presets`

## Summary

Today the QA dashboard has two parallel, overlapping features:

- **Skills** (`/qa/skills`) — named, multi-file bundles (`SKILL.md` + `references/`,
  `scripts/`, …) with a folder-upload "Description UI". These are **already mounted**
  into probe runs via `stage_org_skills()` → `materialize_skills()` → Harbor's native
  `AgentConfig(skills=…)`, landing as `.claude/skills/<name>/` in the sandbox.
- **Probe presets** (`/qa/presets`) — operator directives that drive a probe launch:
  `operator_prompt`, `result_focus`, `evaluation_metric`, plus `agent`/`model`.

We are merging these into a **single "Skills" feature** built on the existing
`SkillModel` (which already owns the working multi-file bundle + mount pipeline).
The probe-preset model is retired and its rows migrated into skills.

### End state (user-visible)

- One page, **Skills**, at `/qa/skills`, using the existing folder-upload Description UI.
- `/qa/presets` is removed (redirects to `/qa/skills`); the nav entry is renamed/removed.
- A **Skill** is a mountable bundle **plus** optional probe-directive fields.
- `agent` and `model` are no longer stored on the directive — they are chosen at
  **probe run-time** (the launch form already supports this).
- Seeded with the 8 skills from `abundant-ai/skillz` + the
  `abundant-ai/harbor-lh` `resources/task-review-agent-guide.md` guide.
- A skill's bundle mounts into a probe **only when that skill is selected at launch**
  (replaces today's "mount every org skill into every probe").

## Conceptual model

A **Skill** = a mountable bundle, optionally also a probe directive:

| Field | Origin | Role |
|---|---|---|
| `name`, `description` | existing `SkillModel` | identity; description shown in Description UI |
| `files[]` (`SKILL.md` + supporting) | existing `SkillFileModel` | the mountable bundle |
| `operator_prompt` *(new, nullable)* | migrated from preset | directive injected when this skill drives a probe |
| `result_focus` *(new, nullable)* | migrated from preset | analyzer focus question / JSON schema |
| `evaluation_metric` *(new, nullable)* | migrated from preset | how the probe result is rendered |
| ~~`agent`, `model`~~ | **dropped** | chosen at probe run-time |

Two natural kinds (no explicit type column needed — derived from `operator_prompt`):

- **Bundle-only** (`operator_prompt IS NULL`): the skillz guides, harbor-lh guide.
  Selectable at launch to mount into the agent workspace; contributes no directive.
- **Directive** (`operator_prompt` set): the migrated presets (Cheat detector, Verifier
  critic, Ambiguity finder, Rust C compiler probe). Selecting one drives the probe and
  mounts its bundle.

## Data model & migration

### `SkillModel` (oddish/src/oddish/db/models.py)

Add three nullable columns:

```python
operator_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
result_focus: Mapped[str | None] = mapped_column(Text, nullable=True)
evaluation_metric: Mapped[str | None] = mapped_column(String, nullable=True)
```

`SkillFileModel` is unchanged.

### Alembic migration(s)

1. **Add columns** to `skills`.
2. **Migrate `probe_presets` → `skills`** (data migration). For each non-deleted
   `ProbePresetModel` row:
   - `name`, `org_id`, `is_seed`, `created_at` carried over.
   - `operator_prompt`, `result_focus`, `evaluation_metric` carried over.
   - `agent`/`model` dropped.
   - Synthesize a one-file bundle so it is a valid skill: a generated `SKILL.md`
     with YAML frontmatter (`name`, `description` derived from the preset name) and the
     `operator_prompt` as the body. `description` = first sentence/line of the prompt,
     truncated.
   - Handle the unique `(org_id, name)` partial index: if a skill with the same
     `(org_id, name)` already exists, suffix the migrated name (e.g. `"<name> (preset)"`).
3. **Drop `probe_presets`** table (after the data migration) — or keep it inert for one
   release if we want a rollback cushion. **Decision: drop it** in the same migration
   chain; the data has moved and the router is removed.
4. **Seed new global skills** (see Seeding) — can be a separate migration or a seed
   routine invoked from the existing skills seed path.

> Per repo gotcha (`CLAUDE.md`): the new columns are read by response builders, so if any
> compact/`load_only` query path enumerates skill columns it must include the new ones.
> Skills currently lazy-load `files`; confirm the new scalar columns are not excluded by a
> `load_only` on the skills list path.

## Backend changes

### Schemas (oddish/src/oddish/schemas.py)

- `SkillCreate` / `SkillUpdate` / `SkillResponse`: add optional `operator_prompt`,
  `result_focus`, `evaluation_metric`.
- Validate `result_focus` with the existing `parse_result_focus()` /
  `normalize_findings_schema()` helpers (reused from the preset path).
- Validate `evaluation_metric` against the allowed set
  (`"result_focus" | "none" | "cheat_ratio" | "ratio"`).
- Remove `ProbePresetCreate/Update/Response` once references are gone.

### Core (oddish/src/oddish/core/skills.py)

- `parse_skill()` unchanged (still parses `SKILL.md` frontmatter for name/description).
- `create_skill_core` / `update_skill_core`: accept and persist the three new fields;
  seeds remain 403-protected on mutation.

### Routers

- `backend/api/routers/skills.py`: unchanged surface (`GET/POST/PUT/DELETE /skills`),
  now carrying the new fields.
- **Remove** `backend/api/routers/probe_presets.py` and its registration.
- `oddish/src/oddish/core/probe/presets.py`: retire. Any logic still needed (e.g.
  `next_probe_model`, result_focus parsing) is moved/kept where used.

### Probe launch path

- **Selection moves from preset → skill.** The sweep/probe submission carries selected
  **skill id(s)** instead of a preset id. For v1, a probe launch selects **one** skill:
  - its `operator_prompt` → `extra_instructions` (existing `apply_probe_overlay` flow,
    `worker/probe_overlay.py`);
  - its `result_focus` / `evaluation_metric` → the analyzer (existing
    `worker/probe_analysis.py` flow);
  - its bundle is the one mounted (see next section);
  - `probe_name` = skill name.
- Selecting a **bundle-only** skill (no `operator_prompt`) mounts the bundle and runs the
  probe with no operator directive.
- `agent` and `model` come from the launch form (already supported,
  `probe-submit-form.tsx` lines ~520–554) — no longer read from the directive.

### Probe execution — mount only the selected skill

Today `stage_org_skills(skills_root, org_id)` loads **all** org skills and mounts them.
Change to **mount only the skill(s) selected for this probe**:

- Thread the selected skill id(s) through the submission → trial spec → worker.
- `stage_org_skills()` (oddish/src/oddish/worker/probe_staging.py) gains a
  `skill_ids: list[str] | None` filter; when provided, only those skills are
  materialized. The materialize/Harbor handoff (`worker/skills_overlay.py`,
  `worker/local_runner.py`) is unchanged.
- **Behavior change to call out in the PR:** existing custom org skills that were
  implicitly mounted into every probe will now mount only when selected. This is the
  approved trade-off (avoids 8 global seeds bloating every run).

### Auto-probe (oddish/src/oddish/core/probe/auto_probe.py)

- `maybe_enqueue_auto_probe()` currently reads `preset.agent` and `next_probe_model()`.
  Replace `preset` with the selected **skill** (directive source); take `agent` from a
  default (`"claude-code"`) or the originating sweep's agent, and `model` from
  `next_probe_model()` as today.

## Frontend changes

### Skills page (`frontend/src/app/(app)/qa/skills/skills-client.tsx`)

- Keep the list + folder-upload Description UI.
- Add optional fields to the create/edit form, in an "Probe directive (optional)"
  section: **Operator prompt** (textarea), **Result focus** (textarea), **Evaluation
  metric** (select). Auto-fill `operator_prompt` from the `SKILL.md` body on folder
  upload is **not** required (the body is already stored as a file); leave the directive
  fields blank by default so uploaded reference skills stay bundle-only.
- Optionally surface description as markdown using the existing `MarkdownRenderer`
  (already in the codebase) — nice-to-have, not required for v1.

### Presets page

- Delete `frontend/src/app/(app)/qa/presets/` (page + client).
- Add a redirect from `/qa/presets` → `/qa/skills`.
- Update navigation: rename/remove the "Presets" entry so only **Skills** remains.
- Remove the Next.js API proxy routes for presets (`/api/probe-presets/...`).

### Probe submit form (`frontend/src/components/probe-submit-form.tsx`)

- Replace the **preset** picker with a **skill** picker (lists skills; can show only
  directive skills, or all skills with directive ones highlighted).
- On select, populate `operator_prompt`/`result_focus`/`evaluation_metric` from the skill
  and send the selected **skill id** in the sweep payload (so the worker mounts that
  bundle).
- Keep the agent/model selectors (run-time choice).

## Seeding the skillz + harbor-lh content

Vendor the source content into the repo for deterministic, network-free seeding:

- Add `backend/seeds/skills/` (or reuse the existing skills seed location) containing the
  8 `abundant-ai/skillz` skill directories
  (`harbor-task-audit`, `harbor-task-harness-refactor`,
  `harbor-task-llmj-agentic-refactor`, `harbor-task-taiga-validate`, `oddish`,
  `sauron-cli`, `taiga-pull-problem-artifacts`, `take-home-pregrader`) — each with its
  `SKILL.md`, `references/`, `scripts/`, `agents/`.
- Add the harbor-lh `resources/task-review-agent-guide.md` as a single-file skill: wrap
  it as `task-review-agent-guide/SKILL.md` with synthesized frontmatter
  (`name: task-review-agent-guide`, `description:` a one-line summary), keeping the
  original markdown as the body (or as a `references/` file under a short SKILL.md).
- A seed routine/migration loads each directory into `SkillModel` (`is_seed=True`,
  `org_id=NULL`) + `SkillFileModel` rows, idempotently (skip if a global seed with that
  name exists). All seeds are **bundle-only** (no `operator_prompt`) unless a SKILL.md
  clearly encodes a directive — default bundle-only.
- License/attribution: these are first-party (`abundant-ai`) repos; copy as-is.

## Testing

- **Migration:** unit-test the preset→skill data migration (rows mapped, names
  de-duplicated, generated `SKILL.md` valid, table dropped). Run against a scratch DB.
- **Core:** `create/update_skill_core` persist + validate the new fields; seed mutation
  still 403s; `result_focus`/`evaluation_metric` validation matches old preset behavior.
- **Probe staging:** `stage_org_skills(skill_ids=[…])` materializes only the selected
  bundle(s); empty/None selection mounts nothing.
- **Launch path:** selecting a directive skill produces the same
  `extra_instructions`/`result_focus`/`evaluation_metric` wiring the preset produced
  (regression parity).
- **Builders:** confirm no `load_only` path defers the new skill columns (per CLAUDE.md
  gotcha) — exercise the skills list endpoint reading the new fields.
- **Frontend:** manual smoke — skills list, create with directive fields, upload folder,
  launch a probe from a directive skill, `/qa/presets` redirects.

## Out of scope (YAGNI)

- Multi-skill selection per probe (mount several bundles + pick a directive). v1 = one
  skill per launch.
- Markdown editing of `SKILL.md` beyond the existing file editor.
- Keeping `probe_presets` as a live table after migration.
- Re-introducing per-skill `agent`/`model` defaults.

## Open considerations (resolved defaults)

- **Auto-probe agent default:** use `"claude-code"` unless the originating sweep agent is
  available to carry forward. (Confirm during implementation.)
- **Skill picker filter:** show all skills but visually mark directive ones; allow
  selecting a bundle-only skill to mount without a directive.
- **Drop vs. retain `probe_presets`:** drop in the migration chain (approved).
