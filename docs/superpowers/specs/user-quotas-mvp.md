# User Quotas MVP — Design

**Owners:** Charles Huang, pratty@abundant.ai
**Status:** Build-ready (simplified). Design + §14/§15/§17 + **§18 simplifications** (stamp-at-creation, default-at-read; 2 prod-breakers designed away) + **§19 scan corrections** (stamp-at-creation wiring in S2, default-at-read at all reads, quota_mode seam). §16 = reduced plan (~36 todos / ~27 behaviour tests). §18/§19 supersede affected parts of §4/§6/§7/§13/§17.
**Scope:** Per-user dollar budgets enforced at trial-admission time. Block new billable work when a user is over budget. No in-flight cancellation.

---

## 1. Goal

Make Oddish usage **attributable and capped** at the finest grain we can ship now: a per-user **daily** dollar budget that blocks new submissions when exceeded. MVP slice of the larger quota proposal — org/project caps, accurate per-call cost, and analytics are future work.

## 2. In scope

- A per-user dollar limit, settable by org admins.
- A hard block when a user over budget tries to create new billable trials (CLI `oddish run`, retries, dashboard).
- An org-admin page to view usage and set/change quotas for any member.
- Usage = `SUM(TrialModel.cost_usd)` over the user's trials **since the start of the current UTC day**.

## 3. Non-goals (deferred)

Org/project-level caps · auto-cancel of in-flight trials · soft caps · full cost reservation+reconcile · per-scope concurrency · BYOK · better cost accounting (per-call/per-kind/reasoning) · GitHub→Clerk identity **verification** (`GET /github/linkage`, OIDC — all-xfail in `backend/tests/test_github_linkage_gate.py`) · richer analytics.

## 4. Locked decisions (interview Batch 1, revised by Codex review)

| # | Decision | Choice |
|---|----------|--------|
| Q1 | Scope | Per-user only, keyed `(org_id, user_id)`. No org-aggregate cap in MVP. |
| Q2 | Attribution (billed user) | **`trials.billed_user_id` (NEW column)**, resolved with **owner-precedence** (`github_username`→active member first), so CI runs bill the **PR author** and interactive runs bill the submitter. Stamped per-trial (fixes append/retry — Codex P0). NOT `tasks.created_by_user_id` (api-key-owner-first → would bill the CI key owner for every run). |
| Q3 | Storage | New `quotas` table in the **backend** Alembic tree. *(Codex P2: confirmed FK-clean — that tree owns `users`/`organizations`.)* |
| Q4 | Period | **Daily, calendar UTC.** `start_of_today_utc = now(utc).replace(hour=0,min=0,sec=0,µs=0)` — a midnight boundary, **not** a rolling 24h `timedelta` (the `dashboard.py:1318` idiom is rolling — do not copy it). Add + unit-test the helper. Window keyed on **`finished_at` = settlement day** (§14-D1 resolved: "use end time"). |
| Q5 | Over-quota computation | Settled `SUM(cost_usd)` **+ count-based pending reservation** (adopted, §10). |
| Q6 | Granularity | All-or-nothing per admission: reject before trials are built. |
| Q7 | Rejection shape | HTTP **402** with `{message, used_usd, limit_usd, period}`. |
| Q8 | Default | **Default-at-read** (§18-B) — no quota row ⇒ enforced at `DEFAULT_DAILY_QUOTA_USD` (deploy config); rows exist only to OVERRIDE. Missing row = fail-closed-at-default. No seed job. `billed_user_id` stamped at trial **creation** (§18-A), not backfilled. |
| Q9 | Who sets quotas | Org `ADMIN`, **self-service for all orgs** (not `@abundant.ai`-gated), via a **dedicated** `can_manage_quotas(auth)` dependency (not bare `require_admin` — see §9, Codex P1). |
| Q10 | Self-edit | Admins may edit any member's quota incl. their own. Cooperative guardrail. |

## 5. Architecture blocks

1. **Quota schema + migration** — `quotas` table + `trials.billed_user_id` column + supporting index.
2. **Trial-admission helper** — `admit_trials(org_id, billed_user_id, count)`; the single quota chokepoint.
3. **Usage aggregation helper** — `SUM(cost_usd)` by `(org_id, billed_user_id, day)`.
4. **Wire admission into every billable-trial path** — create, append, retry, auto-probe.
5. **Quota admin API** — get/set/list-members + member usage read.
6. **Org-admin quota page** (FE) + **CLI rejection UX**.

## 6. Data model

New `quotas` table (`backend/models.py` + `backend/alembic`):

```
quotas
  id          uuid pk
  org_id      fk organizations  (indexed)
  user_id     fk users          (indexed)
  limit_usd   numeric(12,4)     not null
  period_kind enum {DAILY} default DAILY
  created_at / updated_at  timestamptz
  unique (org_id, user_id)
```

**New column** `trials.billed_user_id` (`oddish/src/oddish/db/models.py`, the `TrialModel` block ~L663–807, which today has **no** per-user field) — a nullable **`String(64)` denormalized column, NOT a real FK** to backend `users` (trials live in the oddish pkg, users in backend; mirrors the unconstrained `org_id`/`created_by_user_id`, models.py:496-500) — stamped at trial **creation** (§18-A / §19-P0-1, in all modes) with the **owner-precedence** resolution (reuse `_resolve_experiment_owner_user_id`, [tasks.py:304-332](backend/api/routers/tasks.py#L304-L332): `github_username`→exactly-one active org member first, else `auth.user_id`, else `api_key.created_by_user_id`). This is why CI runs bill the PR author. **No historical backfill** (§18-A): pre-rollout rows stay NULL (harmless — outside today's window).

**`billed_user_id` doubles as the billable marker (§11.5).** The four creation paths (sweep-create/append/retry/auto-probe) stamp it (§18-A/§19); import and combine don't, so they stay `NULL`. Because usage is `SUM(cost_usd) WHERE billed_user_id = :user_id`, `NULL` rows count against nobody's quota. Semantics: `billed_user_id` = *"the user whose daily budget this trial's cost draws down; NULL = draws down no one."* No origin column or `worker_jobs` join needed. **One requirement:** combine must pass `billed_user_id=None` explicitly and never add it to `_COMBINE_TRIAL_RESULT_FIELDS` (`deletion.py:488/699`); import leaves it unset (NULL). Combine copies `cost_usd`/`origin`/`started_at`/`finished_at` but the NULL marker — not `origin` — is what excludes it, so **`TrialOrigin.COMBINED` is unnecessary and dropped** (§18-A; superseded req to add it + its CHECK migration). Bonus: combine copies the historical `finished_at`, so combined rows also sit outside today's window by date (defense in depth).

**New index** on trials: `(org_id, billed_user_id, finished_at) WHERE deleted_at IS NULL`. Existing trial indexes (`task_id` only; `(org_id, created_at, model, provider)`) don't support the per-user daily SUM (Codex P1).

**Type note (REQUIRED):** `TrialModel.cost_usd` is `Float`; `limit_usd` is `numeric(12,4)`. The `used + reserved >= limit_usd` check MUST be computed in Decimal or integer cents so a value exactly at the cap is deterministic. Add a boundary test at exactly `limit_usd`.

## 7. Enforcement — admission helper, not an endpoint gate

The original design gated only `POST /tasks/sweep`. Codex found that leaks: **retries, appends, and auto-probe all create billable trials on other paths.** So quota admission is a **shared helper** called wherever Oddish-origin queued trials are inserted+enqueued.

```
# NB (§18/§19): billed_user_id is stamped at CREATION (S2), upstream of and in
# ALL modes. This helper is the check only; mode-gating wraps the RAISE, not the stamp.
admit_trials(session, org_id, billed_user_id, count, mode):
    if org_id is None: return            # OSS single-tenant → no-op every mode (§19 P1-2)
    if mode == off:    return            # skip the check (stamping already happened at creation)
    if billed_user_id is None:           # unlinked/ambiguous handle, ownerless API key
        block(Unattributed, mode)        # enforce→403 "link GitHub"; shadow→log, no raise (§11.3)
    limit    = quota.limit_usd if (quota := get_quota(org_id, billed_user_id)) \
               else DEFAULT_DAILY_QUOTA_USD   # §18-B default-at-read: no row ⇒ enforce at default
    used     = sum_cost_usd(org_id, billed_user_id, start_of_today_utc)   # settled today, by finished_at (Decimal)
    reserved = (inflight_count(org_id, billed_user_id) + count)           # in-flight = finished_at IS NULL
               * PENDING_TRIAL_RESERVATION_USD          # §10 (kept)
    if count == 0: return                # fully-reconciled append: nothing new (§19 P2-2)
    if used + reserved >= limit:         # Decimal compare
        block(QuotaExceeded(used, limit, period), mode)  # enforce→402; shadow→log, no raise
```

Call sites (every billable insertion point — Codex P0 chokepoint list):

| Path | Entry | Insert/enqueue |
|---|---|---|
| Sweep create | `tasks.py:451` → `sweep.py:70` | `queue.py:~831` |
| Sweep append | same | `queue.py:~1068` (today inserts trials with **no** submitter field) |
| Auto-probe | internal to sweep `sweep.py:~304` → `auto_probe.py:~105` | (inside sweep flow) |
| **Trial retry** | **`trials.py:143` (`POST /trials/{id}/retry`, TASKS scope)** | `core/endpoints/trials.py:~207` — **bypasses sweep entirely** |

**Idempotency ordering (Codex P1):** resolve the idempotency replay **before** calling `admit_trials`, so a faithful replay returns the original response without re-checking quota or creating work. (Today the replay short-circuit lives inside `create_task_sweep_core`, caught at `tasks.py:478`.)

**Import/combine (Codex P1):** `POST /trials` import (`trials.py:114`) and combine (`deletion.py:488/699`) create terminal `TrialModel` rows carrying caller/copied `cost_usd` but enqueue **no** worker job — not Oddish-billed compute. Decision in §11.

## 8. Usage aggregation

With `trials.billed_user_id` added, the period SUM is a single-table scan (no JOIN), mirroring `get_model_usage_core` (`dashboard.py:1307`):

```
sum_cost_usd(org_id, user_id, period_start) =
  SELECT COALESCE(SUM(cost_usd), 0) FROM trials
  WHERE org_id = :org_id AND billed_user_id = :user_id
    AND finished_at >= :period_start AND deleted_at IS NULL
```

Keyed on **`finished_at`** (settlement day, §14-D1): a trial's cost counts on the day it *lands*, so there's no midnight-leakage window and no naive "cost is still NULL" gap — an in-flight trial simply has `finished_at IS NULL` and isn't in the SUM (it's covered by the reservation instead, §10). Bonus: combine *copies* the source's real `finished_at` (`deletion.py:519`), so combined rows carry a **historical** `finished_at`, keeping them out of today's window by date **as well as** by the `billed_user_id NULL` marker — defense in depth. Backed by the new `(org_id, billed_user_id, finished_at)` index. Reused by the admission helper and the admin/member usage endpoints.

## 9. Admin API + FE + auth

- **`can_manage_quotas(auth)`** — a dedicated dependency, **not** bare `require_admin`. `require_admin` treats any `FULL`-scope API key as admin (`backend/auth/__init__.py:303`); quota management should be **user-auth-only** (role==ADMIN), like `can_create_api_keys` is user-only (`permissions.py:28`). Encode the API-key policy explicitly rather than inheriting it (Codex P1).
- Endpoints in `backend/api/routers/orgs.py`:
  - `GET /quotas` — members with `{user_id, limit_usd, used_usd, period}` (admin).
  - `PUT /quotas/{user_id}` — set/clear a member's limit (admin).
  - `GET /quotas/me` — caller's own usage-vs-limit (any member).
- FE: page/tab reusing `TagAdminPolicyForm` (useSWR+draft+save) + the `/api/*` proxy. Placement TBD (§11).

## 10. Over-quota computation + the overshoot (Q5 — ADOPTED)

`cost_usd` settles only at trial completion, so a settled-only gate lets a user at \$0 launch a huge sweep that runs far past the (now **daily**) cap before anything settles. The trial **count is deterministic at submission** (`schemas.py:164` `n_trials`; expanded `sweeps.py:61`; append reconciles count `sweep.py:246`), so a **count-based pending reservation** is nearly free — no token estimation:

```
reserved = (live in-flight trial count for the user today + this submission's count)
           * PENDING_TRIAL_RESERVATION_USD
```

**DECISION: keep (confirmed).** Without it, end-time billing lets one big sweep sail through at ~$0 `used` and overshoot before anything finishes — the pre-charge is what actually stops a runaway job. `inflight_count` = the user's currently in-flight trials (`finished_at IS NULL`, status `PENDING/QUEUED/RUNNING/RETRYING`, not day-bound), and the reservation covers retry + auto-probe trials. `PENDING_TRIAL_RESERVATION_USD` and `DEFAULT_DAILY_QUOTA_USD` are **deploy-time config** (env/settings, tunable without a code change), not hardcoded — so exact values aren't architecturally blocking.

## 11. Open decisions (Batch 2)

1. **Q5 count reservation — RESOLVED: adopt** the count-based pending guard (§10).
2. **CI / GitHub attribution — RESOLVED: bill the PR author.** The live experiments-repo workflow ([`oddish-experiment.yml`](https://github.com/abundant-ai/experiments/blob/main/.github/workflows/oddish-experiment.yml) L120-126, L269) submits `--github-user "$PR_AUTHOR"` where `PR_AUTHOR = gh api .../pulls/N → .user.login` — the PR author's handle, read server-side on a default-branch workflow (so not PR-spoofable). Decision: the admit helper resolves `billed_user_id` with **owner-precedence** (`github_username` → exactly-one active member → PR author), so CI runs bill the author and interactive runs bill the submitter. **Accepted risks (MVP, consistent with the account-merge-plan's signed-off API-key bypass):** (i) Oddish does not verify the handle — a direct `ODDISH_API_KEY` call to `/tasks/sweep` can forge `--github-user`; secure quota would need server-side identity binding (deferred). (ii) `/oddish` is triggered by a **commenter who may not be the PR author**, so a collaborator can spend against the author's budget — accepted. (iii) An unlinked/ambiguous author handle → `billed_user_id = None` → org/system bucket (uncapped), see #3.
3. **Null-owner submitter — RESOLVED: reject unconditionally.** When `billed_user_id` resolves to `None` (unlinked/ambiguous `--github-user`, or ownerless API key), **block** with a "link your GitHub at oddish.app" error (403). Aligns with account-merge-plan Q12 (unlinked → push fails). The grandfathering conflict is gone: since everyone is seeded a quota (§8), enforcement is universal — no per-org enforcement flag. Consequence: an experiments-repo PR by an author who hasn't linked GitHub at oddish.app is blocked until they link.
4. **Concurrency race — RESOLVED: accept for MVP.** Two near-simultaneous submissions just under the cap can both pass (no row lock). Bounded by the daily cap + reservation; not worth an advisory lock on the hot path for MVP.
5. **Imported / combined trials — RESOLVED: exclude** via the `billed_user_id IS NULL` marker (§6). Import/combine bypass `admit_trials`, so their rows never join a user's SUM; combine must null the copied `billed_user_id`.
6. **Probe / QA inclusion — RESOLVED: count probes.** `is_probe=True` TRIAL rows carry cost and route through auto-probe admission, so they get a `billed_user_id` and draw down the author's daily budget. (QA jobs still have no `cost_usd` — invisible, deferred.)
7. **FE placement — RESOLVED: `/admin` tab** (admin-only management surface, beside the existing org/queue/tags admin tabs).
8. **Q9 rollout — RESOLVED: (a) self-service for all org admins** (not `@abundant.ai`-gated).

## 12. Multi-org (corrected)

`clerk_user_id` is **NOT** globally unique — one human is one `users` row **per org** (upsert on `(clerk_user_id, org_id)`; `backend/models.py:99`), and personal-org fallback (`provisioning.py:350`) can mint another. Quotas are therefore per **`(org_id, user_id)` membership row**, and the same person carries independent budgets in different orgs. (Earlier "single-org enforced by UNIQUE" claim was wrong.)

## 13. Rollout — SUPERSEDED by §18/§19 (see §16 slice order + its rollout line)

> ⚠️ This section's original steps (historical backfill + physical seeding) are **removed** by §18: `billed_user_id` is stamped at trial **creation** (no backfill), and missing quota rows enforce at `DEFAULT_DAILY_QUOTA_USD` (no seed job). The authoritative rollout is the S1→S5 slice order in §16, ending `off → shadow → enforce`. Kept here only as a change-log anchor.

1. ~~Backfill `billed_user_id` on historical rows~~ → **stamp at creation** (§18-A); old rows stay NULL (harmless).
2. ~~Seed a quota row per user~~ → **default-at-read** (§18-B); rows are override-only.
3. Ship the schema + creation-stamp (S1–S2), usage read (S3), overrides + admin (S4), then admission `off→shadow→enforce` (S5).

## 14. Round-2 review resolutions (Codex + date-recon, convergent)

**P0 — must resolve before build:**
- **Schema-first, or the SUM silently fail-opens.** `billed_user_id` doesn't exist yet; the usage SUM keys on it, so if admission ships before the column+index+backfill, every row is implicit-NULL → SUM = 0 for everyone → no enforcement. Ship schema as strict rollout step 1; add a **startup/CI assertion** the column + `(org_id, billed_user_id, finished_at) WHERE deleted_at IS NULL` index exist before enforcement is enabled.
- **Add `TrialOrigin.COMBINED` + null `billed_user_id` on combine.** Both reviews: combine copies `origin`+`cost_usd` and there's no COMBINED value, so backfill can't tell combined rows from real runs → double-attribution. (See §6.)
- **Admission lives in the shared core/queue layer, NOT the router.** Batch (`sweep.py:453` per-item), the OSS server (`server/__init__.py:364`), and retry (`trials.py:143`, no gate today) all reach trial creation without passing a router-level gate. Wire `admit_trials` + `billed_user_id` stamping at the insert/enqueue seam covering all of: sweep-create (`_bulk_insert_trials` `queue.py:794`), append (`queue.py:1033`), retry (`core/endpoints/trials.py:120`), auto-probe (`auto_probe.py:105`).

**P1 — resolve during build:**
- **Reservation status set:** inflight = non-superseded `PENDING/QUEUED/RUNNING/RETRYING` (include worker auto-retries, `trial_handler.py:636`). Matches existing active-trial logic (`queue.py:1165`).
- **Provisioning seeding:** add idempotent `ensure_quota(org_id, user_id)` at **both** provisioning return points — new user (`provisioning.py:335`) and personal-org (`provisioning.py:405`). Missing quota row = **fail-open** (defensive), since everyone is seeded.
- **Invariant + tests:** `billed_user_id IS NULL` on every non-Oddish-compute trial. Required tests: (a) combined copy NULL; (b) imported trial NULL; (c) daily boundary bucketing at 23:59:59Z vs 00:00:00Z; (d) exact-`limit_usd` comparison determinism.

**Open decisions (need your call):**
- **D1 — daily semantics — RESOLVED: settlement day (`finished_at`).** "Use end time." `used` = cost of trials that *finished* today. Cleaner than `created_at`: no midnight-leakage, no "cost still NULL" gap (in-flight trials have `finished_at IS NULL` → not summed → covered by the reservation), and combine's copied historical `finished_at` keeps combined rows out of today's window by date too. Tradeoff accepted: a multi-day trial counts against the budget of the day it finishes, not the day it launched.
- **D2 — reservation coarseness — RESOLVED: flat constant for MVP.** A flat `PENDING_TRIAL_RESERVATION_USD` is coarse against 100× model cost variance (`model_pricing.py:99`) but is the minimal option and the whole reservation is just a guardrail. First tightening later = per-provider/model tier.

## 15. Prod-breaking review — Codex bake-off (both findings verified against code)

Two independent Codex agents hunted prod-breakers; every kept finding is code-grounded and cross-checked.

**P0 — breaks prod on ship (or ships a silent exploit):**
- **[A] Cross-package FK is illegal.** `trials.billed_user_id` = nullable `String(64)`, NOT an FK to backend `users` (models.py:496). A real FK breaks OSS-first installs/migrations. (Fixed in §6.)
- **[A/B] `origin` is a CHECK constraint, not a native enum.** Add `'combined'` by replacing `CHECK(origin IN …)`, never `ALTER TYPE ADD VALUE`; override `origin=COMBINED` on combine inserts; leave historical maybe-combined rows `billed_user_id NULL`. (Fixed in §6.)
- **[A] Day-1 CI lockout.** Universal enforce + reject-null-owner 403s every experiments-repo PR by an unlinked author → CI down org-wide. REQUIRED before ship: an org/env **kill switch** + a **dry-run / alert-only mode**; a pre-ship audit of recent PR authors through the resolver; notify unlinked users; flip to hard 403 only once the unlinked count is low.
- **[B — 🍪] Cancel-to-zero quota bypass.** Cancelled trials get `finished_at` set (`queue.py:222`) but the result writer early-returns (`trial_handler.py:562`) BEFORE writing `cost_usd` (`:594`) — so they're in neither the settled SUM (`cost_usd` NULL) nor the in-flight reservation (`finished_at` set): the reservation is released and real partial cost is invisible. A start-then-cancel loop evades the cap. FIX: on cancel, persist available cost/token fields from `outcome`, or hold a settled-on-cancel debit; never silently release the reservation to $0.

**P1 — breaks under load/edge:**
- **[A] Migration locks.** oddish migrations run in a txn (`env.py:79`); build the index with `CREATE INDEX CONCURRENTLY` in an `autocommit_block()` (pattern: `mine_filter_idx_001`); run the backfill as a bounded, batched, idempotent callable job — never inside the migration.
- **[A] Batch = N quota checks.** `create_task_sweep_core` runs per-item (`sweep.py:446`). In batch mode group by billed user, compute usage + in-flight once per user, apply cumulative amounts in the savepoint loop; cap batch size.
- **[A+B] Seeding misses existing users → silent no-enforcement.** `ensure_quota` only at the new-user path misses existing-user early returns (`provisioning.py:311/326/380`); with fail-open those users are silently unenforced. FIX: `INSERT … ON CONFLICT (org_id,user_id) DO NOTHING` on EVERY (user,org) return path, non-fatal (never fail auth); + offline seed job; + pre-enablement check that all active users have a row.
- **[B] Non-atomic admission race** (already §11.4-accepted for MVP). Tightening when needed: `SELECT … FOR UPDATE` on the quota row, held through insert.
- **[B] MissingGreenlet landmine (forward).** When `billed_user_id`/quota fields are surfaced in `build_trial_response` / `build_compact_trial_response` / `_build_task_status_response`, they MUST be added to the compact `load_only` set (`tasks_query.py:151`) or the experiment page 500s (per CLAUDE.md).

**Cleared (false alarm):** duplicate GitHub handles do NOT throw `MultipleResultsFound` — the resolver uses `.all()` exact-one semantics (`tasks.py:248`).

**🍪 Winner: Agent B** — the cancel-to-zero bypass is a novel, verified, silently-shippable exploit found by tracing execution (not reading the spec), plus B cleared a false alarm and flagged the load_only landmine. Honorable mention: Agent A landed more total confirmed bugs and the two most deploy-blocking migration catches (cross-package FK, CHECK-not-enum).

## 16. Implementation plan — vertical slices (issue tracker)

This is the **reduced** plan: the §17 corrections are folded in inline (no separate correction layer), and the adopted simplifications are applied throughout — (A) `billed_user_id` is stamped at trial **creation** in the four billable paths (sweep bulk-insert, append, retry, auto-probe); combine/import leave it NULL, and there is **no historical backfill**, no `TrialOrigin.COMBINED`, no combined-row parking, no batched migration UPDATE; and (B) **default-at-read** — a missing quota row means enforce at `DEFAULT_DAILY_QUOTA_USD` (fail-closed-at-default), so there is **no `seed_quotas`, no `ensure_quota`/provisioning write-sites, and no coverage gate**; rows exist only to *override* the default, and the admin list uses `COALESCE(row.limit, default)`. Cancel-floor is folded into cost-completeness (one native→estimate→constant chain via one `_apply_cost_fields` helper). One `quota_mode` enum `{off|shadow|enforce}` replaces the kill-switch+dry-run bools; one Decimal `sum_cost_usd`; one `start_of_today_utc` helper; single config home at `oddish/src/oddish/config.py:838`; `resolve_billed_user_id` delegates to `_resolve_experiment_owner_user_id`. Ship slices in order; each is independently deployable. `[must]` = required regression for a §15 prod-breaker.

The two prod-breakers from §17 are **designed away, not patched**: "backfill double-attributes combined rows" is gone because there is no backfill and combine writes NULL at creation; "seeding misses users → silent no-enforcement" is gone because missing rows enforce at the default. ~27 behaviour tests total (S1≈4, S2≈5, S3≈6, S4≈4, S5≈9).

### S1 — Cost completeness (foundation)

> ⚠️ **Reverted — see `docs/flow/S1-flow.md`.** The cost-synthesis machinery
> below (native→estimate→floor settlement, accumulation, `cost_settled_attempt`)
> was removed: `cost_usd` now stays the raw provider value (NULL when unreported).
> The `pending_trial_reservation_usd` floor survives only as the S5 *in-flight*
> reservation, and `estimate_cost_usd` survives only as a read-side display
> estimate. The rest of this section is a historical planning record.

**Goal:** Every settled billable trial persists a non-NULL `cost_usd` (native → estimate → constant floor) so quota accounting can't be bypassed by start-then-cancel or by tokens-but-no-native-cost.
**Depends on:** none

**Todo (6):**

- [ ] **S1.1 Add `pending_trial_reservation_usd` to the single config home** — Add `pending_trial_reservation_usd: Decimal` (env `ODDISH_PENDING_TRIAL_RESERVATION_USD`) to the `Settings` class at `oddish/src/oddish/config.py:838` (NOT `settings.py`, which does not exist — §17-P1.5). Parsed as `Decimal`, never float (§6 type note). This is the constant floor used by S1's cost-completeness fallback, S5's reservation, and the default-at-read seed value origin. · files: `oddish/src/oddish/config.py` · _done when:_ `Settings().pending_trial_reservation_usd` is a `Decimal`; an `ODDISH_`-prefixed env var round-trips into it; no dollar constant is hardcoded in trial_handler/queue.
- [ ] **S1.2 Extract `_apply_cost_fields(trial, outcome)` + native→estimate→floor chain** — Refactor the field-copy block at `trial_handler.py:588-594` (input_tokens, cache_tokens, cache_write_tokens, output_tokens, total_steps, cost_usd) into a terse comment-free helper so success and cancel paths write the identical field set (no drift; cache_write_tokens landed recently in 3a2c9184). Inside it, resolve cost as: native `outcome.cost_usd` if not None; else `estimate_cost_usd(...)` from the token counts (the estimate today runs only on READ in `core/helpers.py`/`tasks_query.py` and is never persisted — §17-P0.1); else the `pending_trial_reservation_usd` constant floor. Never overwrite an already-set non-NULL `cost_usd` with NULL. · files: `oddish/src/oddish/workers/queue/trial_handler.py` · _done when:_ Normal `if outcome:` path routes through the helper unchanged (existing tests green); a tokens-only outcome persists an estimate; a no-token/no-cost outcome persists the floor; the helper is reusable by the cancel branch.
- [ ] **S1.3 Persist partial cost on the cancel early-return path** — In `_store_trial_results` at the `if user_cancelled or (runtime_cancelled and not is_modal_image_build_error):` block (`trial_handler.py:558-562`), before `return`, call `_apply_cost_fields(trial, outcome)` (S1.2) so a late worker outcome's partial cost/tokens land. Do NOT touch status/error_message/harbor_stage/finished_at here (owned by the cancel writer, `queue.py:222-229`); only add the missing cost columns; keep the early return. · files: `oddish/src/oddish/workers/queue/trial_handler.py` · _done when:_ A user-cancel or runtime-cancel with a late outcome persists cost/tokens; status/error/finished_at untouched; the §15[B] discard-at-`:562` hole is closed.
- [ ] **S1.4 Write the cancel-time floor synchronously in the `queue.py` cancel writer** — Per §17-P0.2, a killed worker's `_store_trial_results` early-return never fires, so the cancel writer at `queue.py:216-233` must itself guarantee non-NULL cost. Before it flips active trials to FAILED/`finished_at`/`harbor_stage`/`max_attempts`, when the row consumed a billable slot (was RUNNING/QUEUED/RETRYING) and `cost_usd` is still NULL, set `cost_usd = pending_trial_reservation_usd`. S1.3 still overwrites this with a real late outcome (overwrite, not add → no double-charge). Skip the floor for never-started PENDING rows (don't charge pure queue churn). · files: `oddish/src/oddish/queue.py`, `oddish/src/oddish/config.py` · _done when:_ A killed-worker cancel leaves `cost_usd` at a positive floor, never NULL; a never-started PENDING cancel is not charged; a late real outcome replaces the floor (single value, not summed).
- [ ] **S1.5 Make `finished_at ⇒ cost_usd` coherent for ALL terminal states** — Audit every terminal branch in `_store_trial_results` (SUCCESS, no-reward SUCCESS, modal-image-build FAILED, non-retryable FAILED, max-attempts FAILED, exception FAILED) plus both cancel writers (`queue.py:222-229`; the timeout/reward-derivation path). Each state that sets `finished_at` on a billable trial must leave `cost_usd` non-NULL (native, estimate, floor, or explicit `0.0` where provably no billable work). Confirm the modal-image-build carve-out (`runtime_cancelled and not is_modal_image_build_error`) still flows through the normal cost block, not the floor. One-line invariant comment: `finished_at set ⇒ cost_usd not NULL for billable trials`. · files: `oddish/src/oddish/workers/queue/trial_handler.py`, `oddish/src/oddish/queue.py` · _done when:_ Every `finished_at`-setting billable branch guarantees non-NULL `cost_usd`; image-build-with-cancelled-stage still lands `image_build_failed` via the normal path; a parametrized test enumerates the branches.
- [ ] **S1.6 Run the oddish suite; update fixtures; confirm no MissingGreenlet** — Run `pytest` from `oddish/` (targeting `test_harbor_runner.py` + handler/queue tests) and linters. SimpleNamespace fixtures must carry every field the helper writes (cache_write_tokens, total_steps) or attribute errors surface. No response-builder column is added this slice, so the compact `load_only` set is untouched — confirm no builder reads a newly-deferred column. · files: `oddish/tests/test_harbor_runner.py` · _done when:_ `pytest` green for touched modules; linters clean; fixtures complete.

**Acceptance criteria:**

- For every settled billable trial, `finished_at IS NOT NULL ⇒ cost_usd IS NOT NULL`, across all terminals (SUCCESS, FAILED image-build/non-retryable/max-attempts/exception, user-cancel, runtime-cancel, agent-timeout).
- Cost resolves native → `estimate_cost_usd` → constant floor; a tokens-but-no-native-cost trial is no longer persisted NULL (§17-P0.1); a no-signal cancel gets the floor.
- A killed-worker cancel is floored synchronously in the `queue.py` writer (§17-P0.2); a real late outcome overwrites the floor with exactly one value (no double-charge); a never-started PENDING cancel is not charged.
- Cancel and success write the identical field set (shared `_apply_cost_fields`), so token accounting can't drift.
- The floor/reservation dollar amount is deploy-time config in `config.py:838` (Decimal), not hardcoded; existing non-cancel terminals and the modal-image-build permanence test stay green; no new MissingGreenlet risk.

**Tests — 4 (behaviour):**

- `S1-T1` (integration) A user-cancelled trial with a late outcome persists the real partial `cost_usd`+tokens instead of discarding them. Guards §15[B] start-then-cancel quota bypass. `[must]`
- `S1-T2` (integration) A tokens-only outcome (no native cost) persists an `estimate_cost_usd` value, not NULL. Guards §17-P0.1 estimate-only undercount. `[must]`
- `S1-T3` (integration) A killed-worker cancel (no `_store_trial_results`, floor written in `queue.py`) leaves `cost_usd` at the positive floor, and a never-started PENDING cancel is not charged. Guards §17-P0.2 reservation-release-to-$0. `[must]`
- `S1-T4` (unit) For every terminal branch that sets `finished_at` on a billable trial, `cost_usd` is non-NULL. Guards the general "settled row invisible to the SUM" invariant. `[must]`

### S2 — Per-trial attribution (stamp at creation)

**Goal:** Every Oddish-billed trial carries `billed_user_id` (who pays), stamped at CREATION in the four billable paths; imported & combined trials leave it NULL.
**Depends on:** S1

**Todo (7):**

- [ ] **S2.1 Add `trials.billed_user_id` column + supporting index to `TrialModel`** — Add `billed_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)` near `org_id` (~L688) — denormalized, unconstrained, **NOT a ForeignKey** (trials are oddish pkg, users live in backend; a cross-package FK breaks OSS installs, §15[A]); mirror `org_id`/`created_by_user_id` (`models.py:496-501`). Comment: `NULL = draws down nobody's quota` (§6/§11.5). Add `Index('idx_trials_org_billed_user_finished','org_id','billed_user_id','finished_at', postgresql_where=text('deleted_at IS NULL'))` to `__table_args__` so `create_all`/preview builds it. No `TrialOrigin.COMBINED` (simplification A). · files: `oddish/src/oddish/db/models.py` · _done when:_ `Base.metadata.tables['trials'].columns['billed_user_id']` resolves, `.foreign_keys` is empty, and the partial index is present in `__table_args__`; models import cleanly.
- [ ] **S2.2 Migration: add column + create index CONCURRENTLY** — New revision under `oddish/alembic/versions/`. Resolve `down_revision` against the current single head (`alembic heads`; add a merge migration first if multiple). `upgrade()`: (1) `ALTER TABLE trials ADD COLUMN IF NOT EXISTS billed_user_id VARCHAR(64)` (nullable, no default, no FK); (2) `CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_trials_org_billed_user_finished ON trials (org_id, billed_user_id, finished_at) WHERE deleted_at IS NULL` inside `with op.get_context().autocommit_block():` (oddish migrations run in a txn, `env.py:66/79`; CONCURRENTLY can't — copy `mine_filter_idx_001`). **No** origin-CHECK change, **no** backfill, **no** batched UPDATE (simplification A). `downgrade()`: drop index (CONCURRENTLY in autocommit_block), drop column. · files: `oddish/alembic/versions/` · _done when:_ `upgrade head` then `downgrade -1` round-trips clean on Postgres; single head; `\d trials` shows the column + partial index; no `ALTER TYPE`, no data statements.
- [ ] **S2.3 Add `resolve_billed_user_id` delegating to `_resolve_experiment_owner_user_id`** — Add `resolve_billed_user_id(session, submission, auth) -> str | None` that **calls** `_resolve_experiment_owner_user_id` (`backend/api/routers/tasks.py:363-394`) directly — never a reimplemented chain (§17-P0.4). Owner-precedence: github handle → exactly-one active member (PR author for CI, §11.2) → `auth.user_id` → `api_key.created_by_user_id`; an **explicit but unresolved** github handle returns `None` immediately (does NOT fall through to `auth.user_id`, `tasks.py:318-320`) so unresolved-CI runs 403 rather than mis-bill the key owner; a duplicate handle returns `None` (exact-one `.all()`, not `MultipleResultsFound`). · files: `backend/api/routers/tasks.py` · _done when:_ Results are identical to `_resolve_experiment_owner_user_id` for github/user/api-key cases; explicit-unresolved-github and duplicate-handle both return `None`.
- [ ] **S2.4 Stamp `billed_user_id` at CREATION in the sweep bulk-insert** — In `_bulk_insert_trials`/`_TRIAL_BULK_INSERT_SQL` (`oddish/src/oddish/queue.py:595`, `:633-665`) add `billed_user_id` to the INSERT column list, the `unnest`/`CAST(:billed_user_id AS text[])` block, the params dict, and keep ordering aligned across INSERT cols / SELECT / unnest args / ORDINALITY alias. Thread a `billed_user_id` param through `create_task` (`:668`, stamped into each `trial_rows` dict) and `append_trials_to_task` (`:981`, into `new_trial_rows`). This is the schema+plumbing seam; the resolved value is passed by S5. · files: `oddish/src/oddish/queue.py` · _done when:_ Sweep-created and appended trials carry the passed `billed_user_id` (NULL by default); unnest arity matches (no "INSERT has more target columns"); existing sweep tests pass.
- [ ] **S2.5 Stamp `billed_user_id` at CREATION on retry** — In `retry_trial_core` (`oddish/src/oddish/core/endpoints/trials.py:120-137`) add `billed_user_id=old_trial.billed_user_id` to the replacement `TrialModel(...)` so a retry carries the same payer forward (a NULL-billed imported/combined retry stays NULL — the safe default). Attribution only here; the admit check is S5. · files: `oddish/src/oddish/core/endpoints/trials.py` · _done when:_ A retry of an oddish trial with `billed_user_id=U` yields `U`; a retry of a NULL-billed trial yields NULL.
- [ ] **S2.6 Force `billed_user_id=None` on combine; leave import NULL** — In `deletion.py` combine (`:699-709`) pass explicit `billed_user_id=None` on the `TrialModel(...)` and **never** add `billed_user_id` to `_COMBINE_TRIAL_RESULT_FIELDS` (§6.1) — combined copies draw down nobody's quota (no `TrialOrigin.COMBINED` needed since nothing keys off origin now; `cost_usd`/`started_at`/`finished_at` still copied, so combined rows carry a historical `finished_at` that keeps them out of today's window as defense in depth). In `trial_imports.py` (`:246-282`) leave `billed_user_id` unset (defaults NULL) with an inline comment locking the invariant. · files: `oddish/src/oddish/core/endpoints/deletion.py`, `oddish/src/oddish/core/ingest/trial_imports.py` · _done when:_ Every combined and imported trial has `billed_user_id IS NULL`; `billed_user_id` absent from `_COMBINE_TRIAL_RESULT_FIELDS`; existing combine/import tests pass.
- [ ] **S2.7 Schema-presence assertion + keep `billed_user_id` out of the compact `load_only`** — Add a CI/startup metadata assertion that `trials.billed_user_id` exists with empty `.foreign_keys` and the partial index (predicate `deleted_at IS NULL`) exists, so the SUM can't silently fail-open (§14-P0). Leave `billed_user_id` out of `build_trial_response`/`build_compact_trial_response` and therefore out of the compact `load_only` set (`tasks_query.py:151`) with a comment noting it must be added there if ever surfaced (CLAUDE.md landmine). Run `pytest` in `oddish/`+`backend/`; update fixtures enumerating trial columns. · files: `oddish/tests/`, `oddish/src/oddish/core/endpoints/tasks_query.py` · _done when:_ The assertion fails if the column, the no-FK invariant, or the index is removed; no MissingGreenlet on the compact/experiment page; suites green.

**Acceptance criteria:**

- `trials.billed_user_id` is a nullable `String(64)` denormalized column with **no** ForeignKey; the `(org_id, billed_user_id, finished_at) WHERE deleted_at IS NULL` index exists both in `__table_args__` and via `CREATE INDEX CONCURRENTLY` in the migration; migration round-trips to a single head with no data steps and no `ALTER TYPE`.
- `billed_user_id` is stamped at **creation** in all four billable paths (sweep bulk-insert, append, retry, auto-probe) and left NULL by import and combine; combine passes `billed_user_id=None` explicitly and never adds it to `_COMBINE_TRIAL_RESULT_FIELDS`.
- `resolve_billed_user_id` delegates to `_resolve_experiment_owner_user_id` (no drifted chain); an explicit-but-unresolved github handle and a duplicate handle both resolve to `None` (403, not mis-bill, not exception).
- No historical backfill, no `TrialOrigin.COMBINED`, no combined-row parking exist anywhere (old trials stay NULL — harmless; only pre-rollout analytics lost, out of scope).
- A schema-presence assertion gates the SUM; `billed_user_id` is deliberately not surfaced in any response builder, so the compact `load_only` set is unchanged and the experiment page does not 500.

**Tests — 5 (behaviour):**

- `S2-T1` (unit) `trials.billed_user_id` is `String(64)`, nullable, with an empty foreign-key set. Guards the §15[A] cross-package-FK OSS-install breaker. `[must]`
- `S2-T2` (migration) Migration adds the column and builds the partial index via `CREATE INDEX CONCURRENTLY` in an autocommit_block, with no data statements. Guards the §15-P1 migration-lock breaker. `[must]`
- `S2-T3` (integration) Combine and import produce trials with `billed_user_id IS NULL`, unconditionally. Guards the combined-row double-attribution breaker (designed away, still asserted). `[must]`
- `S2-T4` (integration) `resolve_billed_user_id` bills the PR author for a CI-style github submission and returns `None` for an explicit-but-unresolved handle. Guards §17-P0.4 mis-bill-to-key-owner. `[must]`
- `S2-T5` (integration) A retry of an oddish trial carries `billed_user_id` forward; a retry of a NULL-billed trial stays NULL. Guards the retry-loop free-compute bypass.

### S3 — Usage visibility (read-only, no enforcement)

**Goal:** Members see their own daily spend; admins see per-member spend vs the effective limit (`COALESCE(row.limit, default)`). Nothing is blocked yet.
**Depends on:** S2

**Todo (6):**

- [ ] **S3.1 Add `start_of_today_utc(now=None)` calendar-day helper** — In a new `oddish/src/oddish/core/quotas.py`, `start_of_today_utc(now: datetime | None = None) -> datetime` returns `now(utc).replace(hour=0, minute=0, second=0, microsecond=0)` — a tz-aware UTC **midnight** boundary. Do **not** copy the rolling `now - timedelta(...)` idiom at `dashboard.py:1318`. Injectable `now` so tests can freeze time. · files: `oddish/src/oddish/core/quotas.py` · _done when:_ Frozen at `2026-06-30T23:59:59.999Z` returns `2026-06-30T00:00:00Z`; at `00:00:00Z` returns the same instant; tz-aware.
- [ ] **S3.2 Add the single Decimal `sum_cost_usd(session, org_id, user_id, period_start)`** — One helper (§17-P2.9: not defined twice) returning **Decimal**: `SELECT COALESCE(SUM(cost_usd),0) FROM trials WHERE org_id=:org_id AND billed_user_id=:user_id AND finished_at >= :period_start AND deleted_at IS NULL`, keyed on `finished_at` (settlement day, §8) not `created_at`, mirroring `dashboard.py:1329`. `cost_usd` is Float → cast to Decimal (`Decimal(str(x))`) to avoid drift. Add the explicit `deleted_at.is_(None)` predicate for correctness under raw selects. Imported unchanged by S3/S4/S5. · files: `oddish/src/oddish/core/quotas.py` · _done when:_ Returns COALESCE-0 Decimal when nothing matches; sums only matching org+billed_user; excludes soft-deleted, in-flight (`finished_at IS NULL`), other-user/org, and before-window rows.
- [ ] **S3.3 Add `can_manage_quotas`/`require_can_manage_quotas` (user-auth-only ADMIN)** — In `backend/auth/permissions.py`, `can_manage_quotas(auth) -> bool` returns True only for `role == ADMIN` AND not an API key — mirror `can_create_api_keys` (`:28`) **without** the `@abundant.ai` gate (§9 Q9(a): self-service). In `backend/auth/__init__.py`, `require_can_manage_quotas` raises 403 when `auth.method == API_KEY` (a FULL-scope key must NOT pass, which bare `require_admin` would allow, `__init__.py:303`) or role != ADMIN. · files: `backend/auth/permissions.py`, `backend/auth/__init__.py` · _done when:_ True for ADMIN user-session, False for MEMBER and for FULL-scope API key; the dependency 403s API-key and non-admin auth.
- [ ] **S3.4 Add quota-usage response schema + `DEFAULT_DAILY_QUOTA_USD` config** — Add `DEFAULT_DAILY_QUOTA_USD: Decimal` (env `ODDISH_DEFAULT_DAILY_QUOTA_USD`) to `config.py:838` (§17-P1.5 single config home) — this is the value a missing quota row enforces at (default-at-read). Add `QuotaUsageResponse {user_id: str, limit_usd: float | None, used_usd: float, period: str}` and a list variant in `backend/api/schemas.py`. `limit_usd` here is the **effective** limit (`COALESCE(row.limit, DEFAULT_DAILY_QUOTA_USD)`); serialize Decimal→float for display. · files: `oddish/src/oddish/config.py`, `backend/api/schemas.py` · _done when:_ `DEFAULT_DAILY_QUOTA_USD` is a Decimal from env with a documented fallback; the schema serializes a member row with effective `limit_usd`, `used_usd`, `user_id`, `period`.
- [ ] **S3.5 Implement `GET /quotas/me` (any member) + admin `GET /quotas`** — In `backend/api/routers/orgs.py`: `GET /quotas/me` behind `require_auth` returns the caller's own `{user_id, effective limit_usd, used_usd = sum_cost_usd(org, auth.user_id, start_of_today_utc()), period}`, strictly scoped to `auth.user_id` (never leaks another member). `GET /quotas` behind `require_can_manage_quotas` lists every org member with per-member `used_usd` computed by **one grouped** query (`GROUP BY billed_user_id`, no N+1) and effective `limit_usd = COALESCE(quotas.limit_usd, DEFAULT_DAILY_QUOTA_USD)` via a LEFT JOIN — members with no quota row show the default, members with no trials show `used_usd=0`. · files: `backend/api/routers/orgs.py`, `oddish/src/oddish/core/quotas.py` · _done when:_ `/quotas/me` returns caller-only usage; admin `/quotas` returns one row per member with grouped (O(1)) usage and COALESCE'd effective limit; non-admin/API-key gets 403.
- [ ] **S3.6 FE: member self-usage widget + admin read-only Quotas tab + proxy routes** — Add `/api/quotas` and `/api/quotas/me` Next.js proxy routes (mirror `/api/tag-policy`) forwarding the Clerk token. Add `QuotaUsage` TS types to `frontend/src/lib/types.ts`. A read-only member widget (`useSWR('/api/quotas/me')`) shows "$X of $Y today". A read-only `Quotas` tab on `/admin` (`useSWR('/api/quotas')`) lists members with today's used $ and effective limit; degrade to "Admins only." on 403. No edit form yet (PUT is S4). · files: `frontend/src/app/api/quotas/route.ts`, `frontend/src/app/api/quotas/me/route.ts`, `frontend/src/lib/types.ts`, `frontend/src/app/(app)/admin/page.tsx`, `frontend/src/components/quota-usage-card.tsx`, `frontend/src/components/quota-admin-card.tsx` · _done when:_ A member sees their own spend vs limit; an admin sees a per-member table; both proxy through `/api/quotas*` and handle empty/error.

**Acceptance criteria:**

- `start_of_today_utc()` is a tz-aware UTC calendar-midnight (not the rolling `timedelta` idiom), unit-tested at both day edges.
- One Decimal `sum_cost_usd` keyed on `finished_at` excludes in-flight, soft-deleted, other-user/org, `billed_user_id IS NULL`, and before-window rows; the same helper feeds S3 UI and S5 enforcement (no drift).
- `GET /quotas/me` returns caller-only usage; admin `GET /quotas` returns per-member `used_usd` (one grouped query) and the **effective** `limit_usd = COALESCE(row, default)`, gated so a FULL-scope API key and non-admin both 403.
- No enforcement: no path returns 402, no trial creation is affected; the compact experiment page still loads (no MissingGreenlet).
- FE member widget + admin read-only Quotas tab render through the `/api/quotas*` proxy and degrade gracefully.

**Tests — 6 (behaviour):**

- `S3-T1` (unit) `start_of_today_utc` puts `23:59:59Z` and `00:00:00Z` in adjacent day buckets (calendar boundary, not rolling 24h). Guards the wrong-window off-by-one. `[must]`
- `S3-T2` (integration) `sum_cost_usd` excludes in-flight (`finished_at IS NULL`) and soft-deleted rows and keys on `finished_at` not `created_at`. Guards the settled-only SUM correctness. `[must]`
- `S3-T3` (integration) `sum_cost_usd` ignores `billed_user_id IS NULL` (imported/combined) rows so their cost counts against nobody. Guards the imported/combined-cost mis-attribution. `[must]`
- `S3-T4` (security) `GET /quotas/me` returns only the caller's usage and never another member's. Guards cross-member usage leak.
- `S3-T5` (security) Admin `GET /quotas` returns 403 for a FULL-scope API key and for a non-admin member. Guards the FULL-key-treated-as-admin bypass. `[must]`
- `S3-T6` (integration) Admin `GET /quotas` shows the default effective limit for a member with no quota row and `used_usd=0` for a member with no trials. Guards default-at-read visibility.

### S4 — Quota overrides + admin management

**Goal:** Admins set/clear per-user daily limits that **override** the read-time default; no seeding, no coverage gate.
**Depends on:** S3

**Todo (5):**

- [ ] **S4.1 Add `QuotaModel` + `quotas` migration (backend tree)** — In `backend/models.py` define `QuotaModel(TimestampedMixin, Base)` `__tablename__ = "quotas"`: `id` String(64) pk; `org_id` FK organizations CASCADE not-null indexed; `user_id` FK users CASCADE not-null indexed; `limit_usd` Numeric(12,4) not-null; `period_kind` varchar(16) default `'daily'` with a `CheckConstraint("period_kind IN ('daily')")` (not a native PG enum, mirror `origin`/§15); `UniqueConstraint('org_id','user_id')`. Import `Numeric`. Migration `backend/alembic/versions/<rev>_add_quotas_table.py` chained off the current backend head: raw `CREATE TABLE IF NOT EXISTS quotas (...)` + `CREATE UNIQUE INDEX ... (org_id,user_id)`, **no data-seeding** (§13/§15-P1: rows are override-only, preview/prod both rely on default-at-read). `downgrade`: `DROP TABLE IF EXISTS quotas`. · files: `backend/models.py`, `backend/alembic/versions/` · _done when:_ `from models import QuotaModel` imports; `upgrade`→`downgrade` round-trips on Postgres; `\d quotas` shows the unique index + CHECK; the migration file contains no INSERT/UPDATE.
- [ ] **S4.2 Add quota request/response schemas** — In `backend/api/schemas.py`: `QuotaMemberItem {user_id, email, name|None, github_username|None, role, limit_usd: Decimal|None, used_usd: Decimal, period}` (where `limit_usd` is the effective `COALESCE(row, default)`), `QuotaListResponse {members: [...]}`, and `QuotaUpdateRequest {limit_usd: Decimal|None}` where `null` **clears the override** (falls back to the read-time default — no separate reset row). Document Decimal serialization consistent with FE. · files: `backend/api/schemas.py` · _done when:_ Schemas import; `QuotaUpdateRequest` accepts `{"limit_usd":"5.00"}` and `{"limit_usd":null}`; `QuotaListResponse` round-trips through `response_model`.
- [ ] **S4.3 Implement `PUT /quotas/{user_id}` (admin, override upsert / clear)** — In `backend/api/routers/orgs.py` behind `require_can_manage_quotas`: validate `user_id` belongs to `auth.org_id` (404 otherwise, mirror `remove_user`); on non-null `limit_usd` upsert the `(org_id, user_id)` row (`INSERT ... ON CONFLICT (org_id,user_id) DO UPDATE`) as the override; on null `limit_usd` **DELETE** the override row so the member reverts to the read-time default; bump `updated_at`; return the updated effective member row. Admins may edit their own quota (§Q10). Never accept `org_id` from the body (tenant-scope). The `GET /quotas` list already exists (S3.5) — it reads `COALESCE(row, default)`. · files: `backend/api/routers/orgs.py` · _done when:_ PUT sets an override that reflects on the next GET; PUT with `null` clears the override and the member shows the default again; a cross-org `user_id` 404s; API-key and MEMBER auth 403.
- [ ] **S4.4 FE: admin Quotas edit form** — Upgrade the S3 read-only Quotas tab into an editable form (`frontend/src/components/quota-admin-form.tsx`, modeled on `tag-admin-policy-form.tsx`): `useSWR<QuotaListResponse>('/api/quotas')`, a per-user `draft` map, an editable `limit_usd` Input (empty = clear override → default), Save → `PUT /api/quotas/{user_id}` then `mutate()`. Add the `/api/quotas/[user_id]` PUT proxy route forwarding the Clerk token and passing through backend status codes. Show "Admins only." on 403. · files: `frontend/src/components/quota-admin-form.tsx`, `frontend/src/app/api/quotas/[user_id]/route.ts`, `frontend/src/app/(app)/admin/page.tsx`, `frontend/src/lib/types.ts` · _done when:_ Editing a limit and clicking Save persists and refetches; clearing the field removes the override (member reverts to default); non-admins see "Admins only."
- [ ] **S4.5 Run backend + FE checks** — `pytest` from `backend/` (DB tests run when `ODDISH_DATABASE_URL` set, skip cleanly otherwise); FE lint/typecheck. Confirm no `seed_quotas`, no `ensure_quota`, no provisioning write-site, and no coverage-gate assertion exist (simplification B). · files: `backend/tests/`, `frontend/` · _done when:_ Backend `pytest` and FE lint/typecheck pass; grep confirms the deleted artifacts are absent.

**Acceptance criteria:**

- `quotas` table exists in the **backend** alembic tree (FK org/user CASCADE, `limit_usd NUMERIC(12,4)`, `period_kind` varchar+CHECK not native enum, `UNIQUE(org_id,user_id)`), round-trips upgrade→downgrade, and contains **no** data-seeding.
- Rows are **override-only**: a present row overrides `DEFAULT_DAILY_QUOTA_USD`; `PUT` with a value upserts the override, `PUT` with `null` deletes it and the member reverts to the default (no seeding, no `ensure_quota`, no coverage gate — simplification B).
- `PUT /quotas/{user_id}` is admin-user-only (API-key/MEMBER 403), tenant-scoped to `auth.org_id` (cross-org 404), allows admin self-edit.
- The admin `GET /quotas` list (S3) shows the effective `COALESCE(row, default)` limit; FE Quotas tab edits persist via PUT and refetch; proxy forwards the Clerk token and passes through status codes.
- Backend `pytest` + FE lint/typecheck pass.

**Tests — 4 (behaviour):**

- `S4-T1` (migration) The `quotas` migration round-trips upgrade→downgrade on Postgres and contains no INSERT/UPDATE data statements. Guards migration reversibility + the "no seeding in migration" rule. `[must]`
- `S4-T2` (integration) `PUT /quotas/{user_id}` with a value overrides the default, and the member's effective limit on `GET /quotas` reflects it. Guards the override write path.
- `S4-T3` (integration) `PUT /quotas/{user_id}` with `null` deletes the override and the member reverts to `DEFAULT_DAILY_QUOTA_USD` on the next GET. Guards default-at-read clear semantics (replaces seeding).
- `S4-T4` (security) `PUT /quotas/{user_id}` for a `user_id` in another org returns 404, and API-key/MEMBER auth gets 403. Guards cross-tenant quota tampering + FULL-key-as-admin.

### S5 — Admission enforcement (off → shadow → enforce)

**Goal:** Block submissions over the daily budget, rolled out safely via one `quota_mode` enum, with a missing quota row enforced at the default.
**Depends on:** S1, S2, S3, S4

**Todo (9):**

- [ ] **S5.1 Add the `quota_mode` enum config** — In `config.py:838` add `quota_mode: QuotaMode` (env `ODDISH_QUOTA_MODE`, enum `{off|shadow|enforce}`, default `off` so the first deploy is a no-op) replacing the old kill-switch+dry-run bools. Semantics: `off` → `admit_trials` is a full no-op (not even a log); `shadow` → resolve+compute+emit a structured `quota.would_block` event but never raise; `enforce` → raise. `pending_trial_reservation_usd` and `default_daily_quota_usd` already live here from S1/S3 (single config home). · files: `oddish/src/oddish/config.py` · _done when:_ `quota_mode` round-trips from env into the enum, defaults `off`; the two dollar values are Decimal; no separate kill-switch/dry-run bool exists.
- [ ] **S5.2 Add `inflight_count` + `get_effective_limit`** — In `oddish/src/oddish/core/quotas.py`: `inflight_count(session, org_id, billed_user_id)` → `COUNT(*) WHERE org_id AND billed_user_id AND finished_at IS NULL AND deleted_at IS NULL AND superseded_by_trial_id IS NULL AND status IN (PENDING,QUEUED,RUNNING,RETRYING)` (not day-bound; matches `queue.py:1165`). `get_effective_limit(session, org_id, user_id) -> Decimal` returns the `quotas.limit_usd` row if present else `DEFAULT_DAILY_QUOTA_USD` (default-at-read — a missing row is fail-**closed** at the default, §11.8 as reinterpreted by simplification B). Reuse the single `sum_cost_usd` (S3.2). · files: `oddish/src/oddish/core/quotas.py` · _done when:_ `inflight_count` counts only non-superseded active `finished_at IS NULL` rows; `get_effective_limit` returns the row when present and the default when absent.
- [ ] **S5.3 Implement `admit_trials(session, org_id, billed_user_id, count)`** — New `oddish/src/oddish/core/quota_admission.py`. Algorithm (§7): (1) `quota_mode == off` → return no-op. (2) `org_id is None` (OSS single-tenant) → return (never touches quotas). (3) `billed_user_id is None` → raise `Unattributed` (→403) unless `shadow` (log `quota.would_block` and return). (4) `limit = get_effective_limit(...)` (never None — default-at-read). (5) `used = sum_cost_usd(..., start_of_today_utc())`; `reserved = (inflight_count(...) + count) * pending_trial_reservation_usd`. (6) compute `used + reserved >= limit` **entirely in Decimal** (§6 type note) → raise `QuotaExceeded(used_usd, limit_usd, period='daily')` (→402 `{message, used_usd, limit_usd, period}`). (7) `shadow` short-circuits the raise: emit the structured event and return. Define `QuotaExceeded`/`Unattributed` carrying the payload. · files: `oddish/src/oddish/core/quota_admission.py` · _done when:_ Raises `QuotaExceeded` at/over the cap, `Unattributed` on None-billed (enforce), no-ops on `off`/org-None; `shadow` computes+logs but never raises; the `>=` is Decimal; unit tests cover each branch.
- [ ] **S5.4 Wire admit into sweep create + append** — In `create_task_sweep_core` (`sweep.py:70`), **after** the idempotency reservation short-circuit (`:125-142`, so a faithful replay returns the stored response without re-checking quota or creating work, §7 P1) and after the create/append branch resolves, `await admit_trials(session, org_id, billed_user_id, count=len(expanded specs))` immediately before `create_task`/`append_trials_to_task`, then pass `billed_user_id` into them. `billed_user_id` arrives as a new core param (resolved in the router, S5.7). Admit runs inside the same txn/savepoint so a 402 rolls back cleanly (no half-created task/trials). · files: `oddish/src/oddish/core/endpoints/sweep.py` · _done when:_ Admit runs after replay and before insert; a replay skips the check; an over-budget create/append raises 402 with no rows committed.
- [ ] **S5.5 Wire admit into retry (legacy-NULL skips admission)** — In `retry_trial_core` (`trials.py:64`), before inserting the replacement trial, `await admit_trials(session, org_id, old_trial.billed_user_id, count=1)`. Per §17-P1.6, if `old_trial.billed_user_id is None` (historical/imported/combined) **skip admission entirely** — never enter the `Unattributed` 403 branch; create the replacement with NULL `billed_user_id` (S2.5 already copies it forward). Retry bypasses sweep, so this is a distinct chokepoint. · files: `oddish/src/oddish/core/endpoints/trials.py` · _done when:_ An over-budget retry of a billed trial raises 402 and neither creates a replacement nor supersedes the old row; a legacy NULL-billed retry still succeeds.
- [ ] **S5.6 Wire admit into auto-probe (over-budget probe skipped, not 402'd up)** — In `auto_probe.py:105` resolve the probe's `billed_user_id` from the task's existing trials / experiment owner, stamp it on the probe trials, and `await admit_trials(session, org_id, billed_user_id, count=probe_count)` before append (§11.6 probes are billable). Since `maybe_enqueue_auto_probe` swallows all exceptions (`:121-124`), put the probe admit in its own try and **downgrade `QuotaExceeded` to skip-with-log** ("probe skipped: over quota") so an over-budget probe never 402s the parent real sweep and the swallow-all doesn't hide a main-path enforcement bug. · files: `oddish/src/oddish/core/probe/auto_probe.py` · _done when:_ Probe trials carry `billed_user_id`; an over-budget probe is skipped+logged; the real sweep still succeeds.
- [ ] **S5.7 Resolve `billed_user_id` in the router; map exceptions; batch grouping** — In `backend/api/routers/tasks.py` single-sweep (`:512`) and batch `_prepare/_finalize` (`:602-628`), call `resolve_billed_user_id` (S2.3) **after** `_apply_github_attribution` (so `github_username` is populated) and pass it into `create_task_sweep_core`. Map `QuotaExceeded`→402 (exact `{message, used_usd, limit_usd, period}` body) and `Unattributed`→403 via a FastAPI handler/try-except. For **batch**, group submissions by resolved `billed_user_id`, compute `used`+`inflight_count` **once per user**, and apply the running cumulative reserved within each item's `begin_nested()` savepoint so two same-user items can't jointly overshoot while an unrelated item's 402 rolls back only itself (§15-P1). Retry route needs no resolution (inherits `old_trial.billed_user_id`). · files: `backend/api/routers/tasks.py`, `backend/api/app.py` · _done when:_ Single + batch resolve via owner-precedence; 402/403 carry the exact bodies; a CI submission bills the PR author; batch queries usage+inflight once per distinct user and blocks the joint-overshoot second item only.
- [ ] **S5.8 Startup/CI schema guard + OSS fail-open + CLI 402/403 rendering** — (a) Add a startup/CI guard (`backend/api/app.py`) that, when `quota_mode != off`, asserts `trials.billed_user_id` + the partial index exist (SQLAlchemy Inspector), failing loudly (or forcing `off`) rather than silently fail-opening the SUM (§14-P0). (b) Confirm `admit_trials` no-ops for the OSS server's `org_id=None` (`server/__init__.py:365/386`) across sweep/retry/batch — a guard, tested. (c) In `oddish/src/oddish/cli/api.py` render a top-level 402 as "Over your daily budget: used $X of $Y (daily). Ask an org admin to raise your quota." and a 403 as the "link your GitHub at oddish.app" guidance, plus per-item 402/403 in the batch renderer — never raw JSON. · files: `backend/api/app.py`, `oddish/src/oddish/core/quotas.py`, `oddish/src/oddish/server/__init__.py`, `oddish/src/oddish/cli/api.py` · _done when:_ Startup fails/forces-off when the column or index is missing with enforcement on; an OSS `org_id=None` sweep/retry/batch never raises and never touches quotas; the CLI renders human-readable 402/403 for single and per-item batch results.
- [ ] **S5.9 Rollout docs + full suites** — Document the `off → shadow → enforce` rollout in a docstring / `backend/README.md` (NOT a standalone report .md): deploy `off`; flip to `shadow` and scrape the structured `quota.would_block` events to enumerate over-budget submissions and unlinked authors (`billed_user_id` None); notify unlinked users; flip to `enforce`. **No seed/coverage pre-step** — stamping is already live from S2 and missing rows enforce at the default. Run full `oddish/` + `backend/` `pytest`; confirm no MissingGreenlet (no quota field surfaced in compact builders). · files: `oddish/src/oddish/core/quota_admission.py`, `backend/README.md` · _done when:_ `shadow` emits a structured would-block event carrying org_id/billed_user_id/used/limit; flipping to `enforce` blocks; suites green; the runbook has no seed/coverage step.

**Acceptance criteria:**

- `admit_trials` lives in the shared core layer (not the router) and is invoked at all four billable seams (sweep-create, append, retry, auto-probe), so batch, OSS server, and retry are all covered; import/combine bypass it and leave `billed_user_id` NULL.
- `billed_user_id` resolved via owner-precedence in the router; `None` → 403 Unattributed; over-budget → 402 `{message, used_usd, limit_usd, period}`.
- `used` keys on `finished_at >= start_of_today_utc()` (calendar UTC midnight); `reserved = (inflight_count + count) * pending_trial_reservation_usd` (inflight = `finished_at IS NULL`, non-superseded, active status set); the `used + reserved >= limit` compare is Decimal and deterministic at exactly the cap; the effective limit is `COALESCE(row, default)` — a missing row enforces at the default (fail-closed).
- Idempotency replay is resolved **before** admit (faithful replay returns the stored response without re-checking quota or creating work).
- `quota_mode` `{off|shadow|enforce}` (single enum): `off` fully disables via env flip, `shadow` logs-but-allows, `enforce` blocks; the slice ships `off`.
- Batch groups by billed user (one usage+inflight query per user), applies cumulative reservation in the savepoint loop, and a per-item 402 rolls back only that item.
- A startup/CI assertion verifies the column + partial index exist before `enforce`; missing → loud failure or forced `off` (never silent SUM=0). The OSS server (`org_id=None`) never 402/403s and never touches `quotas`.
- The CLI renders 402/403 human-readably (single + per-item batch), not raw JSON; no task/trial rows commit when admission raises; over-budget retry neither creates a replacement nor supersedes the old row; full suites green with no MissingGreenlet regression.

**Tests — 9 (behaviour):**

- `S5-T1` (edge) `admit_trials` blocks at exactly `limit_usd` (Decimal boundary determinism). Guards Float-vs-numeric cap non-determinism. `[must]`
- `S5-T2` (unit) `billed_user_id is None` raises 403 in `enforce` but only logs in `shadow`. Guards the unattributed-run 403 contract + shadow safety. `[must]`
- `S5-T3` (unit) `quota_mode == off` makes `admit_trials` a full no-op and `org_id is None` (OSS) fail-opens. Guards the kill-switch + OSS-lockout breaker. `[must]`
- `S5-T4` (unit) A missing quota row enforces at `DEFAULT_DAILY_QUOTA_USD` (fail-closed-at-default). Guards the §15 "seeding misses users → silent no-enforcement" breaker (designed away by default-at-read). `[must]`
- `S5-T5` (unit) `inflight_count` counts only non-superseded PENDING/QUEUED/RUNNING/RETRYING with `finished_at IS NULL`, and `reserved = (inflight+count) * reservation`. Guards the reservation overshoot guard. `[must]`
- `S5-T6` (integration) A faithful idempotency replay of a completed key returns the stored response without re-checking quota or creating work. Guards double-charge/duplicate-work on replay. `[must]`
- `S5-T7` (integration) An over-budget create, append, and retry each raise 402/roll back with no rows committed and no supersession; a legacy NULL-billed retry still succeeds. Guards half-created-rows leak + the §17-P1.6 NULL-retry contract. `[must]`
- `S5-T8` (integration) A batch with two same-user items that jointly exceed the cap blocks only the second, querying usage+inflight once per distinct user. Guards the batch = N-checks joint-overshoot breaker. `[must]`
- `S5-T9` (integration) With `enforce` on and the `billed_user_id` column/index missing, startup fails loudly (or forces `off`) instead of fail-opening the SUM to 0. Guards the §14-P0 schema-first silent fail-open. `[must]`

**Rollout:** ship `quota_mode=off` → flip to `shadow` (scrape `quota.would_block` events, notify unlinked authors) → flip to `enforce`. No seed/coverage pre-step — stamping is live from S2 and missing quota rows enforce at `DEFAULT_DAILY_QUOTA_USD`.

## 17. Plan corrections — bake-off round 2 (both agents, verified against code)

Two Codex agents reviewed §16. **🍪 Winner: Agent A** — 3 unique deep-correctness P0s including the estimate-only-cost hole (below), the single highest-impact finding of the whole review. Honorable mention: **Agent B** — the shared combined-backfill P0, the rollout seed/coverage gate, and 3 must-have test gaps; and B delivered reliably while A needed a resume. Apply these before starting S1.

### P0 — correctness, fix before build
1. **[A ✓] Persisted `cost_usd` is NULL for estimate-only trials → quota undercounts.** `HarborOutcome.cost_usd` is `float|None` (`outcome.py:35`); `trial_handler.py:594` stores it verbatim (`:405` sets NULL); `estimate_cost_usd` runs only on READ (`core/helpers.py`, `tasks_query.py`), never persisted. Every trial from a CLI agent that reports tokens-but-no-native-cost has `cost_usd=NULL`, and the quota SUM (stored column) silently skips it. **FIX — expand S1 to "cost completeness":** at every billable terminal persist native cost, else `estimate_cost_usd(...)`, else the floor; `cost_usd` is never NULL for a settled billable trial. Amends S1.4/S1.5; add S1.x "persist estimate when native cost absent."
2. **[A ✓] Cancel floor must be written synchronously in the cancel writer.** `queue.py:222-229` sets FAILED/`finished_at`/`harbor_stage`/`max_attempts` in the cancel API path; the worker `_store_trial_results` early-return (S1.4's site) never fires for a killed worker. **FIX:** apply the no-outcome floor in `queue.py`'s cancel writer before clobbering state; S1.3 still overwrites with a real late outcome. Amends S1.4/S1.6.
3. **[A+B ✓] Backfill must not stamp historical combined rows.** Combined copies carry `origin='oddish'` (`deletion.py:499`) with the idempotency marker dropped (`:690`); S2.9's blanket `origin='oddish' AND billed_user_id IS NULL` stamps them → double-attribution. **FIX:** restrict the backfill to rows with an auditable worker-job marker (real runs have a `worker_job`; combined copies don't); leave ambiguous rows NULL; assert post-backfill. Add S2.x "park/exclude combined cohort" before S2.9; amend the S2.9 predicate + acceptance.
4. **[A ✓] `resolve_billed_user_id` must delegate to `_resolve_experiment_owner_user_id`, not a drifted chain.** The real resolver returns `None` immediately when an explicit github handle is unresolved (`tasks.py:318-320`) — it does NOT fall through to `auth.user_id`. A reimplemented fallthrough would mis-bill unresolved-CI runs to the key owner instead of 403-rejecting. **FIX:** S2.6 calls the real resolver directly; S5.9 asserts unlinked-explicit-github → None → 403. Amends S2.6/S5.9.

### P1
5. **[A+B] Single canonical config.** `settings.py` does not exist — the settings class is `oddish/src/oddish/config.py:838`. Put `pending_trial_reservation_usd` + `default_daily_quota_usd` there, introduced in **S1** (S1.4 needs the floor), read by S4 seeding and S5 admission. Remove S4.3's stale "S2/S3 admission config" ref and S5.1's duplicate. Amends S1.4/S4.3/S5.1.
6. **[A+B] Legacy NULL-billed retry contract.** S5.4 (None→403) contradicts S5.7 (None→fail-open). Resolve: a retry whose superseded trial has `billed_user_id IS NULL` **skips admission entirely** — never enters the 403 branch, creates the replacement NULL. Amends S5.4/S5.7; add the test (was deferred).
7. **[B] S5 rollout must run seed + coverage gate before enforce.** S4 builds `assert_all_active_users_have_quota` as the gate, but S5.14's runbook never runs it (or `seed_quotas`) before flipping `quota_dry_run=False` → uncovered users fail-open silently. **FIX S5.14:** seed → assert coverage==0 → then enforce.
8. **[A] Provisioning S4.5 misreads the code.** The email-resolved path returns `email_users[0]` directly at `provisioning.py:391` — it does NOT go through `get_or_create_user_in_org`. **FIX:** call `ensure_quota` immediately before that direct return. Amends S4.5.

### P2
9. **[B] One `sum_cost_usd`.** S3 (`float`) and S5 (`Decimal`) define it twice → UI/enforcement drift. Single helper returning `Decimal`, imported by S3/S4/S5; test UI usage == admission usage on one fixture. Amends S3.2/S5.3.
10. **[A] Schema-CHECK test can't use `Base.metadata` alone** (CHECK is DB-level; no model `CheckConstraint`). Test via the migrated DB (`pg_constraint`/inspector) or add a model-level `CheckConstraint`. Amends S2.10.
11. **[B] Restore two deferred must-have tests:** auto-probe stamps non-NULL `billed_user_id` (S5.8 done-when requires it); sweep-create bulk-insert arity against Postgres post-migration (S2.8 changes raw SQL). Add to S5/S2 test lists.

## 18. Simplifications applied (supersedes affected parts of §4/§6/§13/§17)

Two decisions approved after tracing the complexity to its root. Both preserve every user-visible behaviour; both **design away** a prod-breaker instead of patching it.

### A — Stamp `billed_user_id` at trial creation; drop the historical backfill
The daily window sums only trials that **finish today** (`finished_at >= start_of_today_utc`); those are all created after stamping ships, so backfilling *past* trials populates data the quota never reads (out-of-scope analytics only). **Do:** stamp `billed_user_id` at creation in the 4 billable paths (sweep bulk-insert `queue.py:595`, append, retry `trials.py:120`, auto-probe `auto_probe.py:105`); combine/import leave it NULL. **Delete:** the backfill job (old S2.9), `TrialOrigin.COMBINED` + its CHECK-replacement migration step, the combined-row "park" step (old §17 P0-3), the batched-`UPDATE` migration. **Designs away:** "backfill double-attributes combined rows." **Residual (accepted):** a pre-stamping trial still running when enforcement flips is NULL-billed → excluded — a one-trial-lifetime cutover blip, empty in practice since S2 and S5 ship days apart. Supersedes §6.2, §13 step 1, §17 P0-3.

### B — Default-at-read; drop physical seeding
**Do:** no quota row ⇒ enforced at `DEFAULT_DAILY_QUOTA_USD` (config); the `quotas` table holds only overrides. Admission and reads use `COALESCE(row.limit, default)`; the admin list LEFT JOINs so rowless members show at the default. **Delete:** `seed_quotas.py`, `ensure_quota` + its 5 provisioning write-sites, the `assert_all_active_users_have_quota` coverage gate. Missing row flips from **fail-open** to **fail-closed-at-default**. **Designs away:** "seeding misses users → silent no-enforcement." Supersedes §4 Q8, §13 step 2, §17 P1-7/P1-8.

### Adopt-now consolidations (no behaviour change)
- **Cancel-floor folds into cost-completeness** — one chain `native → estimate → constant` via one `_apply_cost_fields` helper (§17 P0-1); no separate floor/reserve-vs-settle bookkeeping. Supersedes the standalone S1.4/S1.6 floor.
- **One `quota_mode {off|shadow|enforce}` enum** replaces the kill-switch + dry-run booleans (removes the illegal `enabled=false & dry_run=true` state); rollout is linear `off→shadow→enforce`.
- **One `sum_cost_usd` (Decimal)** and **one `start_of_today_utc`** helper (were duplicated S3/S5).
- **One config home** `oddish/config.py:838` (not the nonexistent `settings.py`) for `pending_trial_reservation_usd`, `default_daily_quota_usd`, `quota_mode`.
- **`resolve_billed_user_id` delegates** to `_resolve_experiment_owner_user_id` (never reimplements the fallback chain).

### Net effect
~56 todos → ~36 · 73 tests → ~27 behaviour tests · deleted: 2 job files, 1 config file, 1 enum member, 1 coverage gate + type, `ensure_quota` + 5 sites, 2 duplicate helpers, 1 flag, the CHECK-replacement migration step, the standalone floor. Columns/tables unchanged (1 new column, 1 new table, 1 partial index). **§16 below is the reduced plan.**

## 19. Post-simplification scan — corrections (verified against code)

A fresh adversarial scan of the simplified plan. The simplifications are sound and the two deleted clusters genuinely erase their prod-breakers, but stamp-at-creation introduces one wiring P0 and leaves two consistency fixes.

**P0 — [new, from §18-A] Stamp-at-creation must be wired; the sweep resolver currently runs AFTER insert.** `create_task_sweep_core` inserts trials (`create_task`→`_bulk_insert_trials`, `queue.py:831`) *before* `_resolve_experiment_owner_user_id` is called (`tasks.py:556`). The data is available earlier — `_resolve_submission_identity` + `_apply_github_attribution` populate `submission.github_username/github_id` before the core call — so the fix is ordering + threading, not missing data. **Do (in S2, not S5):** resolve `billed_user_id` before `create_task_sweep_core`; thread it through `create_task`/`append_trials_to_task` (stamp each row dict, `queue.py:794/1033`) → `_bulk_insert_trials` (add column to `_TRIAL_BULK_INSERT_SQL` list + `unnest CAST(:billed_user_id AS text[])` + params `:644`); thread into `maybe_enqueue_auto_probe` (`auto_probe.py:53`) so probes stamp too; retry uses `old_trial.billed_user_id` (already in hand, trivial). **Miss any path → NULL → silently uncounted → enforcement void.** This is why stamping is **attribution (S2)**, independent of the enforcement check.

**P1-1 — Default-at-read must be applied at all three reads.** The §7 helper is fixed (default, not fail-open). Also apply `COALESCE(row.limit_usd, DEFAULT_DAILY_QUOTA_USD)` in `GET /quotas/me` and the admin `GET /quotas` LEFT JOIN (rowless members show the enforced default, not blank/unlimited). Centralize as `get_effective_limit(org_id, user_id) -> Decimal`. Test: a user with **no** row is blocked at the default.

**P1-2 — `quota_mode` seam.** Stamping moved upstream (P0-1), so it happens in **every** mode; only the *raise* is mode-gated. Corrected: `off` no-ops the **check**, not the stamp (the S5 "`off` = no stamp" wording is wrong). `org_id is None` (OSS server) short-circuits to no-op in all modes before touching a nonexistent quotas table. Reflected in §7.

**P2:** one `sum_cost_usd` returning `Decimal` (compare in Decimal at the cap); `count == 0` fully-reconciled append must not 402 (guarded in §7); `start_of_today_utc` is the calendar-midnight helper, never the rolling `dashboard.py:1318` idiom; drop the origin-CHECK schema assertion (no `COMBINED` to check — schema guard shrinks to "column + partial index exist").

**Confirmed CLEAN (explicit):** combine (`_COMBINE_TRIAL_RESULT_FIELDS` omits `billed_user_id`) and import both leave NULL with zero code change → excluded by the NULL marker regardless of `origin`, so dropping `TrialOrigin.COMBINED` + the CHECK migration is safe; deleting the backfill erases the combined-double-attribution prod-breaker; default-at-read makes `users` the source of truth so no member is silently unenforced; `config.py:838` is importable from both packages; resolver delegation preserves the unresolved-github → None → 403 behaviour.

**Refinement adopted:** stamping → S2 (attribution); S5 = the check + mode-gate only (a smaller, safer enforcement slice; shadow/off still produce correct `billed_user_id` data).

## 20. Pre-build review round 3 — codex bake-off (5 agents) + adjudication

Final fresh-eyes pass immediately before build: 2 simplify + 3 error-hunt codex agents, every kept finding verified against code. **🍪 Winner — the stale-terminal cost gap (§20.1):** cost-completeness (S1) covers `_store_trial_results` + the `queue.py` cancel writer but MISSES two real terminal writers that set `finished_at` without `cost_usd`, so a settled billable trial goes invisible to the quota SUM. Found by tracing terminal paths, not the spec — the whole §14–§19 review missed it. Honorable mentions: batch `QuotaExceeded`→400 contract break with the named regression greening anyway (§20.8), and the residual S2/S5 resolver-threading contradiction (§20.2). These corrections **supersede the affected todos**.

### 20.1 🍪 S1 coverage gap — floor cost on ALL terminal paths (P0, corrective)
Two terminal writers set `finished_at` on a billable trial without `cost_usd`, both independent of `_store_trial_results` AND of the cancel writer:
- **Stale-worker reaper** `oddish/src/oddish/workers/queue/cleanup.py:389-397`: the exhausted-retry `else` branch → `status=FAILED`, `finished_at=... or utcnow()`, `harbor_stage='cancelled'`, `stale_reaped_at` — **no `cost_usd`**. Fires when a worker dies; the dead worker never runs `_store_trial_results`, and this is NOT the cancel-API writer (`queue.py:216`).
- **Retry-supersede** `oddish/src/oddish/core/endpoints/trials.py:151-156`: a non-terminal old trial snapped to `FAILED` + `finished_at` on supersede — no `cost_usd`.

**Fix (amends S1.4/S1.5):** write the floor at **every** terminal that sets `finished_at` on a billable trial. Extract cost resolution into one shared helper reusable by: (a) `_store_trial_results` success path, (b) its cancel early-return, (c) the `queue.py` cancel writer, (d) the `cleanup.py` stale-reap `else` branch, (e) the `trials.py` retry-supersede branch. Skip never-started PENDING rows (no billable slot consumed); a real late outcome still overwrites the floor. **New tests:** `S1-T5` (stale-reap floors `cost_usd`) and `S1-T6` (retry-supersede floors `cost_usd`), both `[must]`. S1 test count 4 → 6.

### 20.2 S2 owns resolver threading; S5 only checks (P0, corrective — resolves §19-P0 vs S2.4/S5.7)
S2.4 says "the resolved value is passed by S5" and S5.7 resolves in the router — contradicting §19-P0 ("resolve in S2"). Resolution: **all `billed_user_id` resolution + threading is S2**; S5 adds only the `admit_trials` check.
- **No `resolve_billed_user_id` wrapper.** Call `_resolve_experiment_owner_user_id` (`backend/api/routers/tasks.py:363-394`; unresolved-explicit-github returns `None` at `:380-382`, before any auth/api-key fallback) directly in the single + batch routers, **after** `_apply_github_attribution`, **before** `create_task_sweep_core`; thread the value through `create_task`/`append_trials_to_task` and into `maybe_enqueue_auto_probe`. Retry uses `old_trial.billed_user_id` (already in hand).
- **§6 citation fix:** the resolver is `tasks.py:363-394` (NOT `304-332`, which is `_lookup_user_by_github_id`/`_resolve_connected_user`); the immediate-`None` return is `:380-382` (NOT `318-320`, a docstring).
- Delete S2.3 (the wrapper todo) and S2.4's "passed by S5 / NULL by default" wording — S2 threads the real resolved value.

### 20.3 Combine/import NULL by omission (simplify)
Drop the explicit `billed_user_id=None` on combine (`deletion.py`) and the import inline comment. The nullable column defaults NULL; the only load-bearing invariant is **never add `billed_user_id` to `_COMBINE_TRIAL_RESULT_FIELDS`**. `S2-T3` remains the guard. (Verify at build that combine constructs `TrialModel` from the explicit field tuple, not a generic `**copied` of the source row.)

### 20.4 S2.7 schema-presence assertion → S5.8 (simplify)
The runtime column+index startup assertion only matters once enforcement reads the SUM. Move it to S5.8 (gated on `quota_mode != off`). S2 keeps the model/migration presence tests (`S2-T1`/`S2-T2`) and the one-line "keep `billed_user_id` out of the compact `load_only`" note.

### 20.5 S2-T4 → wiring test, not resolver semantics (test quality)
Resolver semantics (exact-one linked, unlinked→None, duplicate→None) are already covered by `backend/tests/test_github_linkage_gate.py`. Retarget `S2-T4`: an end-to-end sweep with a CI-style github submission stamps `billed_user_id = PR author` on the inserted trial rows; an explicit-but-unresolved handle stamps NULL. Tests the NEW wiring, not the resolver.

### 20.6 One FE component (simplify)
S3.6 builds `frontend/src/components/quota-admin-form.tsx` in read-only mode; S4.4 extends the same file with draft/save inputs. Delete `quota-admin-card.tsx` from the plan.

### 20.7 Schemas defined once (simplify)
Define `QuotaMemberItem` + `QuotaListResponse` + `QuotaUsageResponse` in S3.4. S4.2 adds only `QuotaUpdateRequest {limit_usd: Decimal | None}`.

### 20.8 Batch = one accumulator + one algorithm; `QuotaExceeded` must ship 402 (simplify + P0 contract)
- Batch (`sweep.py:446-488`, `tasks.py`): pre-resolve billed users + counts, prefetch `used`/`inflight` once per user, keep `reserved_so_far[user]`, call the **same** `admit_trials(count + reserved_so_far)` inside each `begin_nested()`, increment only on success. No batch-specific quota algorithm.
- **Contract fix:** the batch loop preserves only `HTTPException.status_code` (`sweep.py:462-471`) and converts any other exception to per-item **400** (`sweep.py:472-483`). A custom `QuotaExceeded` would therefore ship as 400, breaking the 402 contract while `S5-T8` ("blocks second") still greens. **Fix:** `admit_trials` raises 402/403 as `HTTPException` (or the batch catches `QuotaExceeded`/`Unattributed` and maps to 402/403 **before** the generic 400 handler). Strengthen `S5-T8` to assert `results[1].status_code == 402` with `{message, used_usd, limit_usd, period}`, first item committed, second rolled back.
- **New test:** `S5-T10` auto-probe (`[must]`): under cap, an inserted probe carries the expected non-NULL `billed_user_id`; over cap, the real sweep still succeeds, the probe is skipped, no probe trial/worker_job commits, and a quota-skip log is emitted (guards the `auto_probe.py:121` swallow-all hiding a bypass).

### 20.9 Drop `count == 0` from `admit_trials` core (simplify)
Remove `if count == 0: return` from the §7 pseudocode. Call sites skip admission when `len(new trials) == 0` (fully-reconciled append). Core stays single-purpose.

### 20.10 `S5-T1` Decimal boundary — prove the math (test quality)
`cost_usd` is `Float`; "blocks at exactly `limit_usd`" passes under float for binary-exact fixtures (`1.0==1.0`). Rewrite `S5-T1` with non-binary-exact money: persisted costs `0.10 + 0.20`, `limit=0.3000`, `reservation=0`; assert the path converts via `Decimal(str(x))`/integer cents before the compare; add a just-under-cap companion (`0.10 + 0.19` vs `0.3000` → admitted).

### 20.11 Config wording (simplify)
S1.1: `pending_trial_reservation_usd` feeds S1's cost-completeness floor and S5's reservation — **not** a "default-at-read seed value" (seeding was deleted in §18-B). Drop that phrase.

### Rejected
- Change `cost_usd` to `Numeric` — spec keeps `Float` + Decimal-in-comparison (§6 type note); a hot-column type migration is out of scope.
- "Column/helpers don't exist yet" findings — expected; this is a build plan, not a diff.
