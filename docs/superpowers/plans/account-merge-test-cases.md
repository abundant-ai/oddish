# Account-merge / in-Action linkage gate — candidate test cases (for prioritization)

Plan: account-merge-plan.md (v7). System under test = A0 schema fix + backend linkage endpoint
(GET /github/linkage) + experiments-repo Action preflight + owner resolution at /tasks/sweep.

## Two invariants under test
- INV1 identity consistency: identity_checked == identity_submitted == identity_owned.
- INV2 predicate parity: "connected?" (endpoint) == "resolvable owner?" (/tasks/sweep) for same input.

## Core matrix (connection state x identity consistency)
1. Exactly one active linked user, actor matches, fresh handle → SUBMIT, owner = that user. [happy]
2. Zero matching users (unlinked actor) → FAIL push + link prompt. [wrong user]
3. 2+ active users share handle (ambiguous) → FAIL, never pick one. [INV2 duplicate trap]
4. One match but user deactivated/inactive → FAIL (active-only).
5. Match exists but in a DIFFERENT org than the API key → FAIL (org-scoped).

## Identity consistency (INV1 — spoof surface)
6. Action checks actor `alice`, repo config submits `--github-user bob` → force alice; owner=alice never bob.
7. Workflow YAML literal --github-user overriding $GITHUB_ACTOR → ignored.
8. Case mismatch actor `Octocat` vs stored `octocat` → normalized match → SUBMIT.
9. Handle with @prefix / whitespace → normalized before compare.

## Staleness & recycle (handle is mutable)
10. User renamed handle; stored stale; actor=new handle → endpoint refreshes from Clerk → match → SUBMIT.
11. Same but refresh skipped (regression) → stale miss → FAIL. [guards refresh requirement]
12. Recycle: alice renames away from `foo`; bob now `foo` & linked → handle-only resolves to bob. Correct?

## Actor semantics
13. PR by alice, re-run by bob → github.actor (alice) vs triggering_actor (bob) — pick one, test it.
14. Bot/cron actor (dependabot[bot], scheduled, GitHub App) → not a linked human → FAIL.
15. Empty/null actor → FAIL safe, never crash.

## Endpoint robustness / availability coupling
16. API key scope: READ ok, TASKS/FULL ok (hierarchy), none/invalid → 401, wrong-org → org-scoped.
17. Clerk unreachable during refresh → fail check or answer stale? [design]
18. Linkage endpoint down/timeout from Action → all pushes fail? fail-open/closed? [design]
19. 50 parallel pushes hammering endpoint + Clerk → throttle, no rate-limit storm.

## Other side of predicate + bypass
20. Duplicate handle reaches /tasks/sweep directly → scalar_one_or_none must NOT 500; no-op owner.
21. Bypass (accepted risk): direct API-key submit, not-connected handle → no owner, no error, task created.
22. Endpoint says connected but sweep lookup disagrees → parity test, both call same helper.

## A0 / schema
23. Fresh preview create_all → no unique index on clerk_user_id; two rows same clerk_user_id across orgs.
24. Preview built BEFORE fix (stale index) → second insert fails → guards force-rebuild caveat.
25. Provisioning upsert on (clerk_user_id, org_id) works without global unique.

## End-to-end
26. Full happy: linked alice → PR → check → submit → shows under alice's Mine.
27. Full self-heal: unlinked bob → FAIL → PR comment → bob links on web → re-run → submits → attributed.

## FINAL — Codex-ranked selection (build these)
Tier-1 must-have:
  3  duplicate active handle → singular lookup scalar_one_or_none RAISES MultipleResultsFound
     (500s) TODAY (tasks.py:247); target = not-connected, never 500. [endpoint+unit]
  6  checked alice but submitted bob → reject before owner stamp (main spoof surface). [Action-e2e]
  1  exactly-one active, actor matches → SUBMIT, owner correct (happy path). [endpoint]
  22 endpoint predicate == sweep predicate (no divergence). [unit]
  20 duplicate handle on /tasks/sweep must NOT 500. [unit]
  17 fail-open: Clerk refresh fails (provisioning.py:51-58 → (None,None)) → answer from stale DB,
     NOT unlinked. [endpoint] (Codex-added)
  21 policy guard: direct API-key bypass still creates task, owner just None — Action gate must NOT
     become server-side enforcement. [unit] (Codex-added)
  4  inactive user with handle → not a match (active-only filter, tasks.py:241-245). [unit] (Codex-added)
Tier-2:
  16 linkage endpoint API-key scope/auth (READ ok, hierarchy, wrong-org). [endpoint]
  10 stale stored handle refreshed from Clerk before deciding (needs dedicated path, see M2). [endpoint]
  5  same handle different org → reject (org-scoped). [endpoint]
  23 model drops global unique on clerk_user_id (xfail until A0 — still unique=True today). [unit]
  25 live DB already allows same clerk_user_id across orgs (head dropped the unique). [migration]
NOTE: case 6 is Action-side (external repo) → skip, not a backend test. M3 (stamp None-safety) is
already guarded by tasks.py:350-351 + existing test_stamp_experiment_owner.py — keep as a 1-line ref.

MISSING cases Codex added (build these too):
  M1 preview FINGERPRINT only hashes table/column names (bootstrap_preview_db.py:57-94) → dropping
     unique=True does NOT bust the trust stamp → reused preview silently keeps old constraint.
     (This supersedes weak case 24.)
  M2 linkage endpoint must NOT reuse ensure_user_github_identity for refresh — it early-returns when
     github_username already set (provisioning.py:125-137/149-159); endpoint needs its OWN refresh path.
  M3 _stamp_experiment_owner is NEVER called with None on exact-one→None/ambiguous (tasks.py:319-321/
     350-358) → else attribution silently cleared.
  M4 API-key AuthContext.user_id is optional (auth/types.py:23-33) → endpoint authorizes by org+scope,
     never infers a human identity from the key.

DROP/DEFER: 2,4 (fold into 1); 7 (redundant w/6); 8,9 (one normalization unit); 12 (no github_id col);
  13,14,15,19,26,27 (need Action repo or other design answers); 24 (replaced by M1).

RESOLVED design Q A: FAIL-OPEN. Endpoint→Clerk refresh fails ⇒ answer from stale DB (don't block).
Action→endpoint call fails ⇒ allow the push. Now testable:
  17 Clerk unreachable during refresh → endpoint answers from stale DB, no hard-fail. [endpoint]
  18 endpoint down/timeout from Action → Action ALLOWS push (fail-open). [Action-e2e]
  10 still valid for the Clerk-UP path: refresh runs, new handle matches. [endpoint]

## Open design questions the edges force
A. Endpoint/Clerk down → fail-open or fail-closed (+break-glass)?
B. Bot/scheduled pushes → allowlist service accounts?
C. Handle recycle → ship github_id now vs optional-later?
D. Re-run attribution → github.actor vs triggering_actor?
E. Is ambiguous handle (2+ active, same handle, one org) even reachable in current data?
