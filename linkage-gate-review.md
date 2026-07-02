# Review: github_id linkage gate — is this design right for the goal?

Branch `feat/github-id-linkage-gate` (PR #559), feeding Quotas PR #542.
Sources: my own read of the code (marked **[verified]**) + Codex review (marked **[codex]**).

## The goal, from first principles

You want exactly three things:

1. **Connect** — a GitHub account maps to one org user row (`users.github_id`).
2. **Gate** — a CI run passing a `github_id` that maps to *no* connected account → **error**, always (independent of quota mode).
3. **Attribute** — the quota PR bills each run to the user that `github_id` resolves to.

Not a goal: anti-spoofing. Anyone may pass any `--github-id`; the gate only checks *linkage*, not *ownership*.

First principles: a gate has two requirements — it must **fire where it can't be skipped**, and its predicate must **match the stated rule exactly**. Everything else (how the column gets filled, how stale data refreshes) is supporting machinery.

## Verdict

**The storage, capture, and transport layers are right. The gate itself is not — it fails both gate requirements.** And ~40% of the diff is machinery not on the critical path for your three goals.

| Layer | Appropriate? |
|---|---|
| `users.github_id` column + org-unique + index | ✅ needed |
| Clerk `provider_user_id` capture at login | ✅ needed |
| `--github-id` CLI → schema → backend transport | ✅ needed (CI hands you `github.actor_id` natively) |
| Resolution (`_resolve_connected_user`) | ⚠️ wrong predicate for the gate (bug 1) |
| Enforcement | ❌ doesn't exist server-side (bug 2) |
| `/github/linkage` endpoint | ⚠️ UX-only; skippable, TOCTOU |
| Reconciler backfill | ➖ droppable *if* a lazy catch-up exists (see note) |
| `clerk_user_id` unique-drop + preview fingerprint | ➖ multi-org cargo, not gate work (not buggy — **[codex]** checked) |

## Bugs

**1. Handle fallback defeats the gate** **[verified]**
`_resolve_connected_user` ([task_submission.py](backend/api/routers/task_submission.py)): if the `github_id` lookup misses, it silently falls back to the handle. So `actor_id=<unlinked>&handle=alice` answers **linked=true**. Your stated rule is "error if the passed github_id isn't connected" — this answers the wrong question whenever a handle also rides along (and CI always sends both). Fix: when a `github_id` is supplied, resolve by id **only**.

**2. There is no server-side gate at all** **[verified]**
[tasks.py:230](backend/api/routers/tasks.py#L230) creates the sweep, *then* resolves the owner at [tasks.py:245](backend/api/routers/tasks.py#L245); unresolved → owner stays `NULL` and the sweep **commits anyway**. The only rejection today is the GitHub Action choosing to call `GET /github/linkage` first — optional, skippable, and racy (TOCTOU between the check and the submit). The gate must be a 403 in `/tasks/sweep` + `/tasks/sweep/batch` *before* `create_task_sweep_core`.

**3. Clerk outage reads as "not linked"** **[verified]**
`fetch_github_identity_from_clerk` returns an empty identity on HTTP error *and* when `CLERK_SECRET_KEY` is unset ([provisioning.py](backend/auth/provisioning.py)). The linkage endpoint then answers `linked=false`. For a gate, this "fail-open" is actually **fail-closed against the user**: a legitimately-linked-but-unbackfilled user gets their CI blocked during a Clerk outage, with a message blaming them. Distinguish "couldn't verify" from "not linked".

**4. Soft-delete relink deadlock** **[verified — not in Codex's list]**
`uq_users_org_github_id` spans soft-deleted rows, `_set_github_id_if_absent`'s clash check deliberately includes them, but every lookup filters `is_active == True`. Sequence: user leaves org (row soft-deleted, still holds the id) → rejoins (new row) → new row can **never** claim their github_id (clash-skip, warning log only) → the gate rejects them **forever**. Needs a release-on-soft-delete or clash-with-inactive-row steal rule.

**5. All-or-nothing refresh write** **[verified]**
`_apply_org_identities` ([github_linkage.py](backend/api/routers/github_linkage.py)) writes every refreshed identity in one transaction; one constraint failure rolls back *all* of them, then the endpoint re-resolves from stale state → false reject for users whose refresh had actually succeeded.

## Suspicious (flagged, judgment calls)

- **Three write paths for one column** (login-fill, reconciler backfill, linkage-endpoint refresh) — and the backfill exists only because login-fill early-returns for users with a cached handle. One catch-up mechanism is genuinely required (login-fill alone leaves old users `NULL` forever **[codex]**), but you need *one*, not two. Pick: keep the reconciler backfill (simple, off the request path) and make `/github/linkage` a plain read.
- **GET with side effects + org-wide fan-out**: every linkage miss fans out to Clerk for *all* org members, no cooldown/cache. A busy org with one unlinked CI contributor re-triggers this on every run — Clerk rate-limit burn.
- **Quota precedence vs. billing invariant**: quota S2 resolves `billed_user_id` as `github_id → handle → submitter → api-key owner`, i.e. a caller-supplied github identity **outranks the authenticated submitter**. That looks like it conflicts with the invariant that a JWT user is always billed to self and `--github-user` must never redirect billing (quota-bypass). Verify on the quota branch before merge.
- **Idempotency-hash special-casing** (unset `github_id` dropped from the hash): a deploy-compat shim, fine, but it's extra surface not required by the three goals.
- Minor asymmetry: id lookup uses `.first()`, handle lookup enforces exact-one. Harmless under the unique constraint, but unexplained.

## Smallest correct shape

1. Keep: column + org-unique, Clerk capture, `--github-id` transport, reconciler backfill.
2. Fix the resolver: `github_id` supplied → id-only lookup (handle fallback only when *no* id was sent).
3. Add the real gate: 403 in `/tasks/sweep`(+batch) before sweep creation when a supplied `github_id` resolves to no active org user. Message: *"GitHub account &lt;id&gt; isn't connected to an oddish user in this org — sign in at &lt;url&gt; and link GitHub, then rerun."* Independent of `quota_mode`.
4. Demote `/github/linkage` to a plain read (nice pre-flight UX for the Action; not the authority).
5. Fix bugs 3–5 (outage vs. not-linked distinction, soft-delete steal rule, per-user refresh writes) — or consciously accept them and document.
