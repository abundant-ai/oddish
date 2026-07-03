# Oddish account-merge plan (github + google + CLI → one user) — v7 (in-Action linkage gate, Codex-cleaned)

## Goal
A human's web identity (Google/GitHub via Clerk) and their CI/CLI experiment runs
resolve to ONE user row, so PR-driven experiments attribute correctly.

## Grounded current state (corrected by Codex)
- Clerk is the only IdP. Google AND GitHub are already external_accounts on one Clerk user.
- `UserModel` (backend/models.py:87-153): id, clerk_user_id, org_id, role, email,
  github_username (mutable, indexed, NOT unique — :122/:152), attribution_cache. NO github_id, NO google_sub.
- ✅ BLOCKER #1 RESOLVED (Codex grep): clerk_user_id is NOT globally unique at DB head. Migration
  c4d5e6f7a8b9 added a unique index; h4i5j6k7l8m9_drop_unique_clerk_user_id.py:21-26 DROPPED it and
  recreated it non-unique; head s5t6u7v8w9x0 unchanged after. Provisioning upserts on
  (clerk_user_id, org_id) (provisioning.py:249-254/280-288). ⇒ ONE HUMAN = N USER ROWS, ONE PER ORG.
  Merge is strictly within-org. github_id uniqueness = UniqueConstraint(org_id, github_id), NOT global.
- ⚠️ LATENT BUG to fix as part of this: models.py:100-102 still declares clerk_user_id unique=True,
  disagreeing with the migrated schema. Preview builds via create_all from the model, so preview would
  silently RE-IMPOSE the dropped unique constraint. Remove unique=True (see Workstream A0).
- CLI/CI auth: API-key AuthContext has no user_id, BUT created_by attribution can fall back to
  api_keys.created_by_user_id (auth/__init__.py:132-140, tasks.py:168-171/283-288).
- Experiments-PR: submission.github_username (unverified) → _lookup_user_by_github_username
  (tasks.py:231-247) → owner stamp. Miss returns None, _stamp_experiment_owner no-ops; the
  __unattributed__ sentinel is applied LATER by dashboard_owner_backfill after age/convergence
  (tasks.py:319-321/350-358, dashboard_owner_backfill.py:359-366).
- ⚠️ There is NO experiment-submission workflow in this repo's .github/workflows (only preview,
  deploy, migrations, base-image, changelog). The submitting Action lives in a SEPARATE experiments
  repo. No OIDC reaches the backend today (only daily-changelog.yml has id-token:write, unrelated).
  pr-preview.yml is preview-deploy only and already excludes forks (head.repo == repo, :18/:263).
- ⚠️ UNVERIFIED FROM REPO (confirm externally): (a) the experiment-submitting Action lives in a
  separate repo — only its ABSENCE here is proven; (b) Google is actually a linked Clerk
  external_account — code only ever inspects oauth_github (provisioning.py:26-37).

## Locked decisions
1. No new first-party OAuth. Clerk owns linking; Oddish reads github identity from it.
2. Auth model = in-Action linkage gate, NO server-side crypto/OIDC. The experiments-repo Action checks
   the pusher (github.actor / actor_id) is a CONNECTED (Clerk-linked) Oddish user before pushing; not
   connected → push fails (Q12). ACCEPTED RISK: a direct API-key call to /tasks/sweep bypasses the gate.
3. Core matches on github_username (handle); immutable github_id is an OPTIONAL correctness upgrade.
4. CLI stays an org-scoped service principal (env-var API key); the human is identified by github.actor
   at push, not by the CLI.
5. Ownership is COSMETIC ('Mine' display) — correctness comes from the push gate, not from guarding the
   owner field. attribution_cache + dashboard_owner_backfill stay as-is. Author-resolution never JIT-creates.
6. 'Connected' is the SAME predicate as 'resolvable owner': EXACTLY ONE active UserModel in the org with
   that handle/id. Ambiguous (2+) or zero → not connected → push fails / no owner.
(Per-question answers in 'Decisions locked (Rounds 1-2)' below.)

## Workstreams

### A0. Fix latent schema bug (do first, standalone)
- Remove unique=True from UserModel.clerk_user_id (models.py:100-102) so create_all-built preview
  envs match the migrated head (h4i5j6k7l8m9 dropped it). Pre-existing bug; fix before adding github_id.
- Necessary + sufficient for FUTURE preview rebuilds; NO prod migration needed (DB already non-unique).
  RISK: previews built BEFORE this fix may already carry the unique index — force a preview rebuild or
  add a drop-index guard if a preview branch can be reused.

### A1. github_id source — ✅ RESOLVED (Clerk docs)
- Clerk Backend ExternalAccount exposes provider_user_id = "unique ID of the user in the provider".
  In the raw REST JSON (Oddish uses httpx, no SDK) the field is snake_case `provider_user_id`, typed
  STRING (github returns its numeric id as a string). No GitHub-REST/OIDC fallback needed.

### A2. Data model + Clerk extraction
- Add `UserModel.github_id TEXT NULL`, indexed; UniqueConstraint(org_id, github_id) (multi-org real).
  TEXT not BIGINT: provider value is a string, equality-match only, no parse/overflow assumptions.
  Cross-org lookups must always be org-scoped — never resolve on bare github_id.
- EXTEND fetch_github_identity_from_clerk (provisioning.py:41-60, :26-37) to also read
  acct["provider_user_id"] for the oauth_github external_account and thread it through.

### B. Backfill (standalone job, NOT migration-embedded)
- One-time + repeatable job populating github_id from Clerk for users with github_username.
- MUST be a callable job, not data baked into an Alembic migration: preview envs bootstrap via
  create_all + stamp-heads (bootstrap_preview_db.py:110-148) and SKIP migration data steps.
- Add throttle/backoff: current Clerk fetch is one GET /v1/users/{id} per user, no retry
  (provisioning.py:48-57). Batch + rate-limit.

### C. Attribution resolution
- 'Connected' = EXACTLY ONE active UserModel in the org with the pusher's handle/id. The owner lookup
  and the linkage endpoint (§E) MUST share this predicate. Today owner uses singular scalar_one_or_none
  (_lookup_user_by_github_username, tasks.py:231-247) which ERRORS on duplicate handles; a plural variant
  (tasks.py:250-274) confirms duplicates are real. Fix: both treat 2+ matches as 'not connected' → no owner.
- owner = that single matched user, resolved at /tasks/sweep. ⚠️ CRITICAL: owner is correct ONLY if the
  submitted github_username EQUALS the actor the Action checked — see §E (Action must force it).
- Staleness: stored github_username refreshes from Clerk only when missing (provisioning.py:125-147), so
  it can be stale. Linkage endpoint should refresh the matched user from Clerk before returning linked.
- keep created_by fallback to api_keys.created_by_user_id (service acct).
- Optional minor hardening: WEB path prefers submission.github_username over auth.user_id (tasks.py:311/323)
  → a web user could mislabel ownership. Low stakes (cosmetic); cheap to flip. Optional.
- Ambiguity rule: never resolve on bare github_id across orgs (dashboard.py:85-115 is org-scoped).

### D. API / schema / CLI plumbing (github_id end-to-end)
- TaskSweepSubmission + payload builders only send github_username today — add github_id:
  oddish/src/oddish/schemas.py:373-376, cli/run.py:750-761, cli/api.py:973-975.
- User-facing contracts expose handle only — add github_id + link-status:
  backend/api/schemas.py:26-35, api/routers/orgs.py:102-111, frontend/src/lib/types.ts:355-363.

### E. Push gate — in the experiments-repo GitHub Action (no OIDC, no backend verify)
- The Action reads github.actor_id (primary, once github_id ships) or github.actor (handle; rename risk).
  NOTE github.actor = initial trigger; a re-run's github.triggering_actor may differ — accepted (Q11).
- Pre-flight: call the backend linkage endpoint; CONNECTED → push; NOT-connected/ambiguous → FAIL the
  job with 'link your GitHub at oddish.app' (Q12).
- ⚠️ BIGGEST RISK (Codex): the Action must SUBMIT THE SAME identity it CHECKED. Set --github-user
  "$GITHUB_ACTOR" from the GitHub context and do NOT let repo-controlled config / a CLI flag override it,
  else the gate checks one person and owner stamps another. (schemas.py:373, cli/run.py:750, cli/api.py:973
  carry only self-supplied github_username today.)
- Backend endpoint = NET-NEW (none exists; /users is an admin-only list, orgs.py:88-112). Shape:
  GET /github/linkage?handle=<actor> (or ?actor_id=) → {linked: bool, user_id?}. Auth = org API key via
  existing bearer (auth/__init__.py:124-140); require APIKeyScope.READ so tasks/full keys pass by hierarchy.
  Return linked ONLY on EXACTLY ONE active match; refresh that user from Clerk first (staleness).
- NOT server-side enforced (accepted risk). No OIDC token / JWKS / audience / ci_provenance.
- FAIL-OPEN (design Q A): the gate is best-effort, NOT a security boundary. Endpoint can't reach Clerk
  to refresh → answer from the stale DB value (don't hard-fail). Action can't reach the endpoint →
  ALLOW the push. Consequence: during a Clerk/endpoint outage the gate is fully open (any actor can
  push) — acceptable, strictly smaller than the already-accepted API-key bypass. No break-glass needed.

### F. Identity sync
- Today refresh is lazy at auth/provisioning (provisioning.py:125-147, auth/__init__.py:203-248).
  Decide: keep lazy, or add a Clerk webhook to update github_id/handle on external_account change.
- NOTE: existing clerk_webhooks.py:186-264 handles org/membership events ONLY — no external-account
  handler exists. Webhook route = net-new handler + svix verification for the github-link event.

### G. Tests
- Linkage endpoint: exactly-one / ambiguous(2+) / zero / stale-handle cases. Action failure-path
  (not-connected → job fails). Owner lookup shares the exactly-one predicate. Action forces
  --github-user to github.actor. Preview bootstrap + (optional) github_id backfill. attribution_cache
  coexistence. (Existing handle/cache tests: test_dashboard_attribution.py:225-238,
  test_provisioning_attribution_cache.py:24-38.)

### H. Linkage UX
- Dashboard 'link GitHub' prompt for users where github_username is null (core); PR comment on
  not-connected pushes. User API/FE expose only github_username today (api/schemas.py:26-35,
  types.ts:353-363) — add an explicit link-status field only if richer UX is needed.

### (dropped) Ownership backfill coherence — see Q17
- Ownership is cosmetic (privilege gate is at task PUSH, not the owner field), so attribution_cache +
  dashboard_owner_backfill stay AS-IS. No provenance flag, no reclaim restriction, no legacy cutoff.

## Rollout
0. Fix clerk_user_id unique=True latent bug (A0).
1. Backend linkage-check endpoint (is github user X connected in org Y?) — reuses existing lookup.
2. Experiments-repo Action: pre-flight linkage check on github.actor; FAIL the push if not connected (Q12).
3. Linkage UX: 'link GitHub' prompt + PR-comment so contributors connect once (Q13).
OPTIONAL (correctness only, anytime — NOT security):
4. Add nullable github_id (TEXT) + extend Clerk extraction; backfill; switch lookup handle→github_id to
   survive handle renames. Surface link-status in user APIs/FE.
DROPPED vs v5: OIDC verifier, VerifiedPrincipal, ci_provenance payload, backfill-coherence + legacy
cutoff (workstream I), forced fail-closed ownership.

## Guardrails
- Author-resolution NEVER JIT-creates a user (JIT default is ADMIN in preview/personal org).
- The experiments repo's own write-access IS the allowlist (only people who can trigger its Action can push).
- Core matches on github_username; github_id > github_username precedence applies ONLY after the optional
  github_id column ships. Ambiguous (2+ active matches) → not connected (push fails).

## Decisions locked (Rounds 1-2)
- clerk_user_id NOT unique → within-org merge, UniqueConstraint(org_id, github_id), one human = N rows.
- github_id source = Clerk provider_user_id (string); column TEXT. NOTE: github_id is now an OPTIONAL
  correctness upgrade (handle rename/recycle), NOT security-critical — lookup may stay on github_username.
- AUTH MODEL (revised, final): NO server-side cryptographic auth, NO OIDC. The experiments-repo GitHub
  Action checks the pusher (github.actor / actor_id, GitHub-provided) is a CONNECTED (Clerk-linked)
  Oddish user before pushing tasks; not connected → push fails. The gate lives in the Action.
- ACCEPTED RISK (explicit, signed off by owner): a direct /tasks/sweep call with the org API key
  bypasses the Action gate. Accepted — API key is trusted/admin-minted. Revisit for QUOTA (real spend).
- Design Q A: FAIL-OPEN on endpoint/Clerk outage (Action allows push; endpoint degrades to stale DB).
  Gate is best-effort attribution, not a security boundary. No break-glass flag needed (already open).
- Q11: owner = the linked user matching the pusher (github.actor); re-run → re-runner divergence accepted.
- Q12: the Action FAILS the push when the pusher isn't connected (reject, not run-unattributed).
- Q13: every contributor logs into oddish.app once to link GitHub via Clerk (the 'force OAuth').
- Q14: CLI stays env-var ODDISH_API_KEY (no `oddish login`).
- Q15: github-link freshness = lazy refresh on web auth (no new webhook).
- Q16: the gate's JOB = AUTHORIZE PUSHING NEW TASKS via the experiments-repo Action. Display ownership
  ('Mine') stays cosmetic. QUOTA is planned (see TODOs) — note: secure quota likely needs server-side.
- Q17: attribution_cache stays AS-IS — low stakes (ownership is cosmetic, grants nothing). No reclaim
  restriction / provenance flag. Workstream I dropped.
- Q18: N/A — gate is forward-only on NEW pushes; historical rows are not re-judged. No cutoff needed.
- Q19: enforce (org_id, github_id) unique; on collision reject 2nd link + manual review.
- Q20: do NOT add created_by/owner dual-tracking. The Action's linkage preflight verifies the triggering
  GitHub Actions actor == a linked user; owner = that pusher. One identity, checked at the gate.
  (Existing created_by stays as-is.)

## TODOs / planned (not this phase)
- QUOTA (planned): per-user/per-org budget enforced at task PUSH. ⚠️ The push gate is in-Action and
  bypassable by a direct API-key call, so SECURE quota likely needs a server-side check — revisit the
  accepted-risk decision when quota lands and real spend is on the line.
- Optional later: gate delete/cancel-own + visibility on the linked owner.
