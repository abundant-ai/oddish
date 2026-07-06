# Quota-enforcement bypass via caller-supplied GitHub identity

> ⚠️ **SUPERSEDED (2026-07-01).** The "fix" described below (§6 — pin billing to the
> authenticated JWT identity) was later **reversed by product decision**: billing now
> follows the resolved owner for *every* caller — a submitted `github_id`/`github_username`
> (github_id takes precedence) is billed to that user, else the API-key owner/submitter.
> `billed_user_id = owner_user_id if owner_user_id is not None else _resolve_created_by_user_id(...)`
> in `backend/api/routers/tasks.py` (single route + batch `_resolve_billed`), with no
> JWT-self short-circuit. The "confused-deputy"/self-bypass in §4 is therefore an
> **accepted** behavior now, not a bug. This doc is kept only as a description of the
> earlier design and the threat model; §6's fix no longer reflects the code.

> A walkthrough of a real bug found (and fixed) in the User Quotas MVP, written
> to be followable by someone who knows HTTP, auth, and SQL but not this codebase.
>
> All links are relative to this file's location (`docs/`), so they resolve when
> clicked. Line numbers are for the state of the branch when this was written.

## TL;DR

The daily-spend quota is enforced against a trial's `billed_user_id`. That subject
was derived from `submission.github_username` / `submission.github_id` — **fields
the client puts in the request body**. So an authenticated user could name someone
else in the body and have the run billed to (and quota-checked against) that other
person. One CLI flag let a capped user keep running *and* silently drain a
colleague's budget. The fix pins billing to the **authenticated** identity for
human callers.

---

## 1. Two different questions

Every `POST /tasks/sweep` forces the server to answer two questions that look
similar but must not be conflated:

| Question | Answered from | Trustworthy? |
|---|---|---|
| **Authn/authz** — who is calling, may they? | the verified Clerk **JWT** (or API key), via `require_auth` | Yes — signed, caller can't forge it |
| **Billing attribution** — whose daily budget does this draw down? | `billed_user_id` on each trial | This is where the bug lived |

The budget check itself is [`admit_trials`](../oddish/src/oddish/core/quota_admission.py) — [`quota_admission.py:65`](../oddish/src/oddish/core/quota_admission.py#L65):

```python
async def admit_trials(
    session: AsyncSession,
    org_id: str | None,
    billed_user_id: str | None,
    count: int,
) -> None:
    mode = settings.quota_mode
    if mode == QuotaMode.OFF or org_id is None or count <= 0:
        return

    if billed_user_id is None:
        if mode == QuotaMode.ENFORCE:
            raise Unattributed()
        _log_would_block(org_id, None, None, None, reason="unattributed")
        return

    effective_limit_usd = await get_effective_limit(session, org_id, billed_user_id)
    used_usd = await sum_cost_usd(session, org_id, billed_user_id, start_of_today_utc())
    reserved_usd = (
        await inflight_count(session, org_id, billed_user_id) + count
    ) * settings.pending_trial_reservation_usd

    if used_usd + reserved_usd >= effective_limit_usd:
        if mode == QuotaMode.ENFORCE:
            raise QuotaExceeded(used_usd, effective_limit_usd)
        _log_would_block(
            org_id, billed_user_id, used_usd, effective_limit_usd, reason="over_budget"
        )
```

Notice: **every** budget lookup keys off `billed_user_id`. Whoever that is, is who
gets charged and rate-limited. So the whole feature's integrity rests on that value
being correct and non-forgeable.

---

## 2. The untrusted input

`github_username` and `github_id` are ordinary client-settable fields on the
submission schema — [`schemas.py:373`](../oddish/src/oddish/schemas.py#L373):

```python
    github_username: str | None = Field(
        None,
        description="GitHub username to attribute this task to (recorded as metadata)",
    )
    github_id: str | None = Field(
        None,
        description="GitHub user id (Clerk provider_user_id) to attribute this task to; immutable across handle renames",
    )
```

The CLI sets `github_username` when you pass `--github-user`. The server only
*auto-fills* it when the caller left it blank, so a **caller-provided value is
preserved** — [`tasks.py:212`](../backend/api/routers/tasks.py#L212):

```python
async def _resolve_submission_identity(
    session: AsyncSession,
    submission: TaskSweepSubmission,
    auth: AuthContext,
) -> None:
    if not submission.github_username:           # <-- only fills when blank
        actor = await _resolve_actor_user(session, auth)
        if actor and actor.github_username:
            submission.github_username = actor.github_username
    ...
```

That is the trust-boundary crossing: a value the attacker controls survives into
the billing decision.

---

## 3. The trace, before the fix

The billing subject was computed by reusing the *dashboard-owner* resolver,
[`_resolve_experiment_owner_user_id`](../backend/api/routers/tasks.py#L363) —
[`tasks.py:363`](../backend/api/routers/tasks.py#L363):

```python
async def _resolve_experiment_owner_user_id(
    session: AsyncSession,
    submission: TaskSweepSubmission,
    auth: AuthContext,
) -> str | None:
    """Primary experiment owner for dashboard Mine (GitHub author beats submitter)."""
    if submission.github_id or submission.github_username:
        user = await _resolve_connected_user(
            session,
            org_id=auth.org_id,
            github_id=submission.github_id,        # <-- caller-controlled
            github_username=submission.github_username,  # <-- caller-controlled
        )
        if user:
            return user.id                          # <-- becomes the billed user
        return None
    if auth.user_id:
        return auth.user_id
    ...
```

`_resolve_connected_user` just maps those caller-supplied fields to an org member —
[`tasks.py:307`](../backend/api/routers/tasks.py#L307):

```python
async def _resolve_connected_user(
    session: AsyncSession, *, org_id: str,
    github_id: str | None, github_username: str | None,
) -> UserModel | None:
    if github_id:
        user = await _lookup_user_by_github_id(session, github_id=github_id, org_id=org_id)
        if user is not None:
            return user
    if github_username:
        return await _lookup_user_by_github_username(
            session, github_username=github_username, org_id=org_id
        )
    return None
```

And the pre-fix `/tasks/sweep` handler fed that straight into billing:

```python
# BEFORE (vulnerable):
billed_user_id = await _resolve_experiment_owner_user_id(session, submission, auth)
...
await create_task_sweep_core(..., billed_user_id=billed_user_id, ...)  # -> admit_trials
```

Chain of custody of the billing subject:

```
request body github_username="bob"   (attacker-controlled)
  -> _resolve_submission_identity  (keeps it; only fills when blank)
  -> _resolve_experiment_owner_user_id
  -> _resolve_connected_user -> Bob's user id
  -> billed_user_id = Bob
  -> admit_trials  checks/charges Bob's budget
  -> trials stored with billed_user_id = Bob
```

The request was **authenticated as Alice** but **billed to Bob**. Authorization and
attribution diverged.

---

## 4. The exploit

Org in `quota_mode = ENFORCE`. Alice has spent her whole \$10/\$10; Bob (same org,
connected handle `bob`, budget untouched) has not.

```
Alice, blocked, runs:   oddish run <task> --github-user bob
```

1. Auth verifies the JWT → "this is Alice." ✅ (correct)
2. `submission.github_username = "bob"` (Alice typed it; not overwritten).
3. `_resolve_experiment_owner_user_id` → `bob` → **Bob's** user id.
4. `billed_user_id = Bob`.
5. `admit_trials(billed_user_id=Bob)` → checks Bob's (empty) budget → **admits**.
6. Trials are created with `billed_user_id = Bob`; their cost lands on Bob's day.

Two harms from one flag:

- **Self-bypass:** Alice's own \$10 cap is irrelevant — she never gets checked
  against it again.
- **Denial-of-budget:** Bob's \$10 is consumed by Alice's runs, so Bob later gets
  `402 QuotaExceeded` on his *own* legitimate work.

No exploit tooling — just a documented CLI flag.

---

## 5. Why it is a bug (the principle)

This is a **confused deputy**: a trusted component (the server) performs a
privileged action (spend from a budget) using its own authority, but decides *whose*
budget based on **untrusted input**. The rule it broke:

> A security- or money-sensitive decision must key off an identity the caller
> **cannot forge** (the authenticated principal) — never off attacker-supplied
> request fields.

`auth.user_id` comes from the cryptographically verified JWT; it is not
caller-controllable. `submission.github_username` is just a string in the body. The
bug used the second where it needed the first.

---

## 6. The fix

Pin billing to the authenticated identity for **human** (JWT) callers; only a
**service principal** (an org API key, where `auth.user_id is None`) may attribute
to a resolved user — that is the intended CI "on behalf of the PR author" flow.

Single route — [`tasks.py:540`](../backend/api/routers/tasks.py#L540):

```python
owner_user_id = await _resolve_experiment_owner_user_id(
    session, submission, auth
)
if auth.user_id is not None:                 # human JWT -> always self
    billed_user_id = auth.user_id
else:                                         # API key (service principal) may attribute
    billed_user_id = (
        owner_user_id
        if owner_user_id is not None
        else await _resolve_created_by_user_id(session, submission, auth)
    )
```

Batch route helper — [`tasks.py:644`](../backend/api/routers/tasks.py#L644):

```python
    async def _resolve_billed(
        session: AsyncSession, submission: TaskSweepSubmission
    ) -> str | None:
        if auth.user_id is not None:
            return auth.user_id
        owner_user_id = await _resolve_experiment_owner_user_id(
            session, submission, auth
        )
        if owner_user_id is not None:
            return owner_user_id
        return await _resolve_created_by_user_id(session, submission, auth)
```

### What it preserves

- **Experiment ownership is untouched.** Billing and the dashboard "Mine" owner are
  now separate concerns: `_stamp_experiment_owner(experiment, owner_user_id, ...)`
  ([`tasks.py:569`](../backend/api/routers/tasks.py#L569)) still uses
  `owner_user_id`, so `--github-user bob` can still make the *dashboard* attribute
  the experiment to Bob — it just can't move money.
- **CI attribution still works.** An API-key (service) submitter can still bill the
  resolved PR author, via `_resolve_created_by_user_id`
  ([`tasks.py:334`](../backend/api/routers/tasks.py#L334)), because a service
  account is a deliberately more-trusted principal than a logged-in human.

### Why `auth.user_id is not None` is the right discriminator

A human Clerk session sets `auth.user_id`; an API key does not (its identity is
`api_key.created_by_user_id`). So the check cleanly separates "a person, bill them"
from "a service account, honor its attribution."

---

## 7. Verification

`oddish/tests/test_backend_task_user_resolution.py` covers this resolution logic and
passes (19 tests). The quota-admission tests
(`oddish/tests/test_quota_admission.py`) exercise `admit_trials` but require a local
Postgres (see the backend DB-test setup), so run them against a real DB.
