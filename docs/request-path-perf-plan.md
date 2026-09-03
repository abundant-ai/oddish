# Request-path performance plan

Status: 2026-09-03. Builds on PR #1467 (merged: experiment page queries).
Phase 0 (PR #1471, region pin) is **dropped**: Modal bills pinned functions at
1.75x, which the team decided not to pay. Phase 1 is implemented on branch
`perf/request-path-phase1`; the measured statement counts are in section 4.


## 1. Summary

One dashboard request makes 13 to 22 database statements, one after another, and
most Modal containers sit 50 to 220 ms from the Supabase pooler in AWS us-east-2.
That product is the 2 to 4 second floor on every page. Measured on 2026-09-03,
same code and traffic on every container:

| Round trip to the database | API containers | `GET /tasks/{id}` median |
|---|---|---|
| 4 ms (Ohio) | 1 | 0.14 s |
| 12 to 31 ms (Chicago, New York, Virginia) | ~10 | 0.4 s |
| 52 to 63 ms (Seattle, Phoenix) | ~42 | 1.45 s |
| 79 to 100 ms | ~71 | 2.2 to 2.7 s |
| 110 to 220 ms | ~25 | worse |

The plan attacks the two factors in order, cheapest and widest first:

1. **Cost per trip** (Phase 0, PR #1471): dropped, see status above. The trip
   cost stays wherever Modal places the container, which makes the trip count
   the only lever this plan pulls.
2. **Trips per request** (Phase 1, one backend PR, net about zero lines): 20 statements
   become 5 by reading outside transactions, caching two kinds of config, and
   fixing one auth cache that misses 80 to 90% of the time.

3. **Bytes and storage probes** (Phase 2): stop shipping every version's trials and
   stop asking storage four questions before reading one file.
4. **Hops** (Phase 3, behind a flag): let the browser call the backend directly and
   retire 102 proxy files (about 4,000 lines).
5. **Structural** (Phase 4, only if the numbers still say so): content-addressed
   task-file layout, the three heavy queries, a shared identity cache.

Target end state per request: at most 5 database statements, each 5 to 25 ms away,
one network hop from the browser. Expected medians (backend, from the containers
that are already close today, then minus the statements Phase 1 removes):

| Route | Today (far containers) | After Phase 0 (measured on close containers) | After Phases 1 to 3 (estimate) |
|---|---|---|---|
| `/tasks/{id}` | 2,175 ms | 508 | ~250 |
| `/tasks/browse` | 2,001 | 451 | ~300 |
| `/trials/{id}` | 794 | 180 | ~100 |
| `/tasks/{id}/detail` | 4,913 | 1,108 | ~700 |
| `/experiments/{id}/open` | 3,142 | 934 | ~700 |
| `/tasks/{id}/files` | 4,084 | 1,292 | ~600 |
| `/tags` | 3,126 | 1,015 | ~900 (query work, Phase 4) |
| `/tasks/{id}/open` | 3,198 | 1,733 | ~1,600 (query work, Phase 4) |

Phase 3 additionally removes the 0.3 to 0.6 s the browser spends going through the
Vercel function on every call.

## 2. How a request works today, and how it should

Today, `GET /tasks/{id}` on a container 90 ms from Ohio:

```
browser -> Vercel edge (sfo1) -> Vercel function (iad1) -> Modal ingress (Virginia) -> container
                                                                                         |
   statement                         who issues it                              trips     |
   ------------------------------    ---------------------------------------    -----    v
   ";"  (pool pre-ping)              SQLAlchemy pool_pre_ping                     1
   BEGIN                             get_session()                                1
   -- auth cache miss (80-90% of dashboard calls) --
   ";" BEGIN, 2-4 SELECT, COMMIT     get_or_create_user_from_clerk                5-7
   SELECT task                       get_task_status_core                         1
   SELECT experiments                selectinload                                 1
   SELECT trials (all versions)      selectinload, filtered in Python later       1
   SELECT worker_jobs x2             fetch_visible_worker_jobs                    2
   SELECT queue info                 fetch_trial_queue_info                       1
   SAVEPOINT, 3 SELECT, RELEASE      load_cost_exclusions                         5
   COMMIT                            get_session()                                1
   ROLLBACK (on pool return)         SQLAlchemy reset-on-return                   1
                                                                                ------
                                                                                20-22
   x 90 ms  =  1.8-2.0 s of waiting, before the database does any real work
```

Target, same request:

```
browser -> Modal ingress (Virginia) -> container in us-east / us-central
                                          |
   ";"  (pre-ping)                        1
   SELECT task + experiments              1
   SELECT trials (current version only)   1
   SELECT worker_jobs (UNION ALL)         1
   SELECT queue info                      1
                                        -----
                                          5   x 12 ms = 60 ms of waiting
   auth: token verified locally, identity from cache
   cost exclusions: in-process cache, refreshed once a minute
```

## 3. Principles

1. **Change the cost of a trip before the count of trips.** Phase 0 makes every later
   phase cheaper to reason about and turns each remaining trip from 90 ms into 12.
2. **Every phase ships alone and reverts alone.** A flag, an env var, or a one-line
   revert. No phase depends on a later one.
3. **Keep both session kinds; make misuse loud.** `get_session()` stays the write
   path. `get_read_session()` becomes the read path, and it gains a guard that
   raises if anything tries to flush through it, so a read handler that grows a
   write fails in tests instead of autocommitting silently.
4. **One cache utility, many caches.** The backend has at least six hand-rolled TTL
   caches (auth, dashboard slices, trajectory, archive bytes, attribution, load
   snapshot). New caches use one small `TTLCache`; existing ones migrate only when
   touched. Local first; the existing `DashboardSharedCacheBackend` (Modal Dict)
   pattern is the cross-container option when a hit rate proves it necessary.
5. **Guardrails are code.** Statement budgets per endpoint live in tests. Container
   placement is a telemetry attribute. Round-trip regressions page someone.
6. **Delete the proxy only after the direct path has soaked.** The 102 files come
   out after the flag has been on in production for two weeks.

## 4. Phases

### Phase 0. Place the API next to the database (dropped)

PR #1471 pinned the API function to `us-east,us-central`. Modal bills a pinned
function at 1.75x, and the team decided the multiplier is not worth it, so the
pin is not going in. The container-to-database distance therefore stays
wherever Modal puts each container (4 to 220 ms); everything below reduces how
many times a request pays it. `oddish.modal_region` / `oddish.modal_cloud`
resource attributes (Phase 1g) make the distance visible per container.

### Phase 1. Fewer statements per request (implemented: `perf/request-path-phase1`)

Statements per core on a seeded task with three trials, steady state (cost
exclusions cached), measured 2026-09-03 with
`oddish/tests/test_statement_budgets.py`:

| Core | Before | After |
|---|---|---|
| `get_task_status_core` (`GET /tasks/{id}`) | 11 | 5 |
| `get_trial_response_for_org_core` (`GET /trials/{id}`) | 9 | 3 |
| `get_task_detail_core` (`GET /tasks/{id}/detail`) | 15 | 9 |
| `browse_tasks_core` (`GET /tasks/browse`) | 4 | 4 |
| `get_experiment_open_core` | 4 | 4 |
| `get_experiment_trial_page_core` | 7 | 2 |

On top of that, every switched GET no longer pays `BEGIN`, `COMMIT` and the
reset `ROLLBACK` (three round trips per session), and dashboard requests stop
paying the auth database session on 80 to 90% of calls. Each item is
independent; they land as one PR because the verification metric is shared
(statements per request).


**1a. Reads outside transactions.** Switch the 85 pure-read GET handlers (the
sweep found 87 using `get_session()`, one that writes, and two that reach a
savepoint, see 1b) plus the three "load then detach" helpers
(`_get_authorized_trial`, `_get_detached_public_trial`,
`_detached_public_trial_with_display_names`) from `get_session()` to
`get_read_session()`. Saves `BEGIN`, `COMMIT`, and the `ROLLBACK` on return.

- Keep `get_policy` in `tags.py` on `get_session()`: it lazily inserts a default
  policy and commits.
- Add the write guard: in `get_read_session()`, register a `before_flush` listener
  on that session that raises `RuntimeError("read session flushed")` when
  `session.new`, `session.dirty` or `session.deleted` is non-empty. About 8 lines.
- Downstream: the soft-delete filter is keyed on the Session class, unaffected.
  `session.expunge` and `session.get` work in autocommit. READ COMMITTED already
  gives no cross-statement snapshot inside a transaction, so nothing loses
  consistency it had.
- Size: ~90 one-line substitutions plus the guard. Existing suites cover the
  handlers.

**1b. One helper for "optional table" reads.** Three places implement the same
"savepoint so a not-yet-migrated table does not poison the transaction" pattern:
`load_cost_exclusions`, `_bump_total_or_zero` in `orgs.py` (reached by the quota
GETs), and `effective_limits_by_org_user_all_orgs` in `quotas.py` (reached by
admin `get_costs`). Under autocommit, `SAVEPOINT` is rejected, so these three block
the read-session switch for their callers. Extract one helper in
`oddish/db/optional_read.py`:

```python
async def read_optional_table(session, stmt):
    """Run a SELECT that may target a table this deploy has not migrated yet."""
    guard = nullcontext() if is_autocommit(session) else session.begin_nested()
    try:
        async with guard:
            return list(await session.scalars(stmt))
    except ProgrammingError as exc:
        if not is_missing_table(exc):
            raise
        return None
```

Replace the three copies (+20 / -25). Then the quota GETs and `get_costs` move to
`get_read_session()` too. Longer term, once the schema has been stable for a
release, the guard can go entirely; that is a separate one-line decision per site.

**1c. Cost exclusions read once a minute.** `load_cost_exclusions(session)` keeps
its signature so its ten call sites do not change. Inside: a module-level
`TTLCache` entry (`ODDISH_COST_EXCLUSIONS_CACHE_SECONDS`, default 60, `0` disables),
one `UNION ALL` over the three tables instead of three statements, and
`invalidate_cost_exclusions()` called from the three admin routers that edit the
lists. Five statements become zero on a hit, one on a refresh.

- Downstream: spend labels on other containers can lag an edit by up to 60 s. The
  admin who made the edit sees it immediately on the container that served the
  write (local invalidation). Document on the admin page.
- Size: +35 / -18, tests +35.

**1d. Auth: identity from cache, role and email from the token.** The token is
verified locally on every request. The database lookup only translates Clerk ids
to internal ids; `get_or_create_user_in_org` already overwrites the stored role
from the token's `org_role` claim on every miss, so the claim is the source of
truth today.

- On a cache hit, build the context with
  `user_role=resolve_role(org_role, cached.user_role)` and
  `user_email=email or cached.user_email`.
- Give Clerk entries their own TTL, `ODDISH_AUTH_IDENTITY_TTL_SECONDS`, default
  900. API-key entries keep 60 s: revoking a key must bite within a minute and
  there is no token expiry to lean on.
- Keep the existing `invalidate_cached_clerk_auth` hooks. The deletion flow already
  documents that other containers hold entries until TTL; with 900 s that window
  grows on paper, but the real bound is Clerk: once the account is deleted, no new
  token is minted and verification fails within the token's ~60 s lifetime.
- Expect the share of dashboard requests that pay an auth database session to
  drop from 0.8 to 0.9 to below 0.1 (Logfire: `db.query_count` greater than
  `handler.db.query_count`).
- Size: +14 / -2, tests +40. If the hit rate stays below 90% after a week, Phase 4c.

**1e. Worker jobs in one statement.** The May 14 `UNION ALL` was reverted because
`select(aliased(WorkerJobModel, subq))` did not map back to ORM entities. The
helper already converts rows into the plain `VisibleWorkerJob` dataclass, so
select the columns, not the entity: `union_all(active_cols, recent_cols)` and
build `VisibleWorkerJob(*row)`. Two statements become one on every task, trial
and experiment fetch. +20 / -25, tests +20.

**1f. Two small ones.** `get_trial_trajectory_summary` reuses its first session
for the refresh-trial read (no storage I/O on that path to release it for).
`list_task_trials` folds `TaskModel.org_id == org_id` into the listing query it
already joins tasks in, and 404s on an empty result only after a cheap existence
check. +8 / -6.

**1g. Guardrails.**

- `backend/tests/test_statement_budgets.py`: wrap each core call in
  `begin_request_timing()` and assert `timing.query_count` against a budget for
  `get_task_status_core`, `browse_tasks_core`, `get_trial_response_for_org_core`,
  `get_task_detail_core`, the experiment `open` and `trial-page` cores. Budgets
  are the post-Phase-1 numbers plus one. About 60 lines. This is the mechanism
  that keeps "five statements" true next quarter.
- `oddish.observability`: add `MODAL_REGION` and `MODAL_CLOUD_PROVIDER` as
  resource attributes (about 6 lines) so placement is a column in Logfire.
- Logfire alert: any API container whose `BEGIN;` median over 15 minutes exceeds
  30 ms. Configuration, not code.

**Verify Phase 1.** `handler.db.query_count` average per route (targets:
`/tasks/{id}` 11.4 to ~4, `/tasks/{id}/detail` 21 to ~9, `/trials/{id}` 3.3 to ~2);
`BEGIN;` spans per minute across the API fleet down by at least half; auth
database share below 0.1.

### Phase 2. Bytes and storage probes (one backend PR)

**2a. `/tasks/{id}` sends the current version only.** Replace
`selectinload(TaskModel.trials)` plus the `set_committed_value` hack with one
explicit trial query filtered on `task.current_version_id`, `superseded_by_trial_id
IS NULL`, and `kind != 'qa_eval'`. Same statement count, a fraction of the bytes
for tasks with many versions or retries. `get_task_status_trials` stays for the
callers that pass a different version. +14 / -8, tests +25. A compact column set
(`load_only`) is a follow-up once the CLI's use of `result` on this route is
confirmed.

**2b. Storage: ask once.** Serving one task file today does up to six sequential
storage calls (HEAD archive, HEAD manifest, GET manifest, HEAD expanded key, HEAD
archive again, GET). Two changes in `oddish/db/storage.py`:

- Cache the "manifest matches archive" answer per `(task_id, version, archive_key)`
  for 60 s in the shared `TTLCache`; the overwrite path calls
  `invalidate_task_layout(task_id, version)` locally. The manifest exists to stop
  a stale expanded tree being served after a re-upload; the cache bounds that
  window to 60 s on other containers, which the re-upload flow already tolerates
  for the archive cache.
- Try the expanded key with a GET and fall back to the archive on 404, instead
  of a HEAD followed by a GET.
- `list_objects_all(prefix, max_keys=None)` stops paginating once `max_keys` is
  reached; `list_trial_files` passes its `limit`.
- Warm the Clerk JWKS in `app.startup` and retry the fetch once on connect error,
  so a cold container's first request does not 503.

Size: +50 / -25, tests +60. Verify: `/tasks/{id}/files/{path}` and
`/trials/{id}/trajectory` handler time minus SQL time (the storage share) roughly
halves.

### Phase 3. The browser talks to the backend (frontend + CORS, flagged)

**Design.** Today every browser call goes browser, Vercel edge, Vercel function,
Modal ingress, container; the function verifies the Clerk session, mints a
backend token, and forwards. That hop costs 0.3 to 0.6 s per call and breaks the
trace between browser and backend. Server-rendered pages already call the backend
directly from `page.tsx` with `getClerkToken`; the browser can do the same.

- `frontend/src/lib/api.ts`: `fetcher` maps `/api/<rest>` to
  `${NEXT_PUBLIC_API_URL}/<rest>` when `NEXT_PUBLIC_API_DIRECT=1`, attaches
  `Authorization: Bearer <token>` from Clerk's client `getToken({ template })`
  (the SDK caches tokens for their lifetime), and keeps the `/api/...` string as
  the SWR key so no cache key changes. Public share pages send no token.
- A tiny `apiFetch` for the 39 bare `fetch("/api/...")` mutation sites; each is a
  one-line edit.
- Backend: `CORS_ALLOWED_ORIGIN_REGEX` (about 8 lines in `_get_cors_origins`) so
  per-branch Vercel preview origins work; the five routes whose proxies set
  `Cache-Control` set it from the backend instead (about 10 lines).
- Frontend observability: add the API URL to the fetch instrumentation's
  `propagateTraceHeaderCorsUrls` so browser spans link to backend spans.
- Env: `NEXT_PUBLIC_CLERK_JWT_TEMPLATE` (the template name is currently
  server-only), `NEXT_PUBLIC_API_DIRECT`.

**Rollout.** Flag on in staging and previews first (the e2e workflow already runs
the backend at `127.0.0.1:8000` with `NEXT_PUBLIC_API_URL` set; the default CORS
list covers localhost). Then production. After two weeks with the flag on, delete
the 84 passthrough and 18 light-logic route files, `backend-response.ts`, and the
server-side token cache in `backend-config.ts` that only they used. Keep the 13
routes with real logic (NDJSON and binary streams, the zip upload, the Logfire
relay, the two with `ServerTimingCollector`) until each is migrated on its own.

**Size.** +120 lines now; -4,000 at deletion.

**Downstream.** Cookies are no longer needed for API calls; `allow_credentials`
stays for the routes that still proxy. `Server-Timing`, rate-limit and
`Oddish-Submit-Concurrency` headers are already in `expose_headers`. The edge
middleware's `traceparent` on `/api/*` responses disappears; the propagated
browser trace replaces it.

**Verify.** HAR: browser wait minus backend total from ~0.45 s to under 0.1 s.
Logfire: `oddish-frontend-edge` middleware spans for `/api/*` stop.

### Phase 4. Structural, only if the numbers still say so

- **4a. Content-addressed expanded layout.** The expand worker writes
  `tasks/{id}/v{N}-files/{archive-hash}/`; readers derive the prefix from the
  database-selected archive key and simply try the object. No manifest, no
  probes, no staleness window. Legacy fallback for unexpanded versions, one
  backfill job. About 120 lines across `task_expand_handler.py` and `storage.py`.
- **4b. The three heavy queries.** `/tags` runs one query that scans
  `tag_assignments` across every organization; `/tasks/{id}/open` runs three
  large CTEs at ~750 ms each; the experiment `open` summary aggregates per
  request (stored counters were already noted as a #1467 follow-up). These are
  query work, not trips; each is its own PR with its own EXPLAIN.
- **4c. Shared identity cache.** If 1d's hit rate stays under 90%, route the
  identity map through the existing `DashboardSharedCacheBackend` (Modal Dict) so
  a cold container inherits the fleet's entries.
- **4d. Pool and pooler.** With statement volume down by half or more, revisit
  `pool_pre_ping`, `pool_recycle=300`, and Supavisor session versus transaction
  mode with data rather than folklore.

## 5. Downstream effects register

| Change | What could break | Guard |
|---|---|---|
| Region pin | No capacity in the pinned regions: slower cold starts, requests queue | Two regions; `buffer_containers=16` unchanged; alert on cold-start p95 |
| Read sessions | A GET that writes would autocommit statement by statement | `before_flush` guard raises; the one known writer stays on `get_session()` |
| Read sessions | `SAVEPOINT` rejected in autocommit | The three savepoint sites move to `read_optional_table` first (1b) |
| Cost-exclusion cache | Spend labels stale up to 60 s on other containers | Local invalidation on write; TTL env-configurable; `0` disables |
| Identity cache TTL 900 s | Local deactivation not seen on other containers until TTL | Clerk token expiry (~60 s) remains the hard bound; hooks unchanged |
| Worker-jobs UNION | Entity mapping (the May 14 failure) | Select columns, not entities; test asserts dataclass rows |
| Manifest cache | Stale expanded file for up to 60 s after a re-upload | Local invalidation from the overwrite path; Phase 4a removes the window |
| Direct browser calls | Preview origins rejected by CORS | Origin regex; flag stays off until staging and previews pass e2e |
| Direct browser calls | Token minting moves to the browser | Clerk SDK caches; template name exposed via `NEXT_PUBLIC_` env |
| Proxy deletion | A route with hidden logic removed by mistake | Only the 84 + 18 classified files; the 13 real ones stay; two-week soak |

## 6. Sequencing

| PR | Contents | App lines | Depends on | Verification |
|---|---|---|---|---|
| ~~#1471~~ | Phase 0 region pin (dropped: 1.75x Modal multiplier) | - | - | - |
| Phase 1 | 1a to 1g (branch `perf/request-path-phase1`) | see `git diff --stat` | nothing | statements per route; auth share < 0.1 |

| Phase 2 | 2a, 2b | ~+70 / -35 | nothing | storage share of handler time halves |
| Phase 3 | direct calls behind flag | ~+120 | CORS env per environment | browser overhead < 0.1 s |
| Phase 3 cleanup | delete proxies | -4,000 | two-week soak | e2e green |
| Phase 4 | as justified | per item | data from 1 to 3 | per item |

Reviewer checklist for Phase 1: every switched handler is in the sweep's
pure-read list; the write guard test passes; the three savepoint sites use the
helper; statement-budget tests assert the new numbers, not the old ones.

## 7. Measurement appendix

Per-container round trip (the placement probe):

```sql
SELECT substr(service_instance_id,1,8) AS inst, count(*) AS begins,
       round(approx_percentile_cont(duration, 0.5)*1000) AS begin_p50_ms
FROM records
WHERE service_name = 'oddish-backend' AND deployment_environment = 'production'
  AND span_name = 'BEGIN;' AND start_timestamp >= now() - interval '30 minutes'
GROUP BY service_instance_id ORDER BY begin_p50_ms DESC LIMIT 50
```

Statements per route and the auth share:

```sql
SELECT r.http_route, count(*) AS n,
  round(avg((p.attributes->>'db.query_count')::float),1) AS avg_total_q,
  round(avg((p.attributes->>'handler.db.query_count')::float),1) AS avg_handler_q,
  round(avg(CASE WHEN (p.attributes->>'db.query_count')::float
                  > (p.attributes->>'handler.db.query_count')::float THEN 1.0 ELSE 0.0 END),2)
    AS share_with_auth_queries
FROM records r
JOIN records p ON p.trace_id = r.trace_id AND p.span_name = 'backend.request.phases'
   AND p.start_timestamp >= now() - interval '1 hour'
WHERE r.service_name = 'oddish-backend' AND r.deployment_environment = 'production'
  AND r.http_route IS NOT NULL
  AND r.span_name NOT LIKE '%http send%' AND r.span_name NOT LIKE '%http receive%'
  AND r.start_timestamp >= now() - interval '1 hour'
GROUP BY r.http_route ORDER BY n DESC LIMIT 30
```

Note: attribute keys containing `auth` are scrubbed by Logfire; use the two query
counts above rather than `auth_total.duration_ms`.

Per-route latency by container distance (the "after Phase 0" preview):

```sql
WITH rtt AS (
  SELECT service_instance_id, approx_percentile_cont(duration, 0.5)*1000 AS begin_ms
  FROM records
  WHERE service_name = 'oddish-backend' AND deployment_environment = 'production'
    AND span_name = 'BEGIN;' AND start_timestamp >= now() - interval '3 hours'
  GROUP BY service_instance_id
)
SELECT r.http_route,
  CASE WHEN t.begin_ms < 30 THEN 'close' WHEN t.begin_ms < 70 THEN 'mid' ELSE 'far' END AS tier,
  count(*) AS n, round(approx_percentile_cont(r.duration, 0.5)*1000) AS p50_ms
FROM records r JOIN rtt t ON t.service_instance_id = r.service_instance_id
WHERE r.service_name = 'oddish-backend' AND r.deployment_environment = 'production'
  AND r.http_route IS NOT NULL
  AND r.span_name NOT LIKE '%http send%' AND r.span_name NOT LIKE '%http receive%'
  AND r.start_timestamp >= now() - interval '3 hours'
GROUP BY r.http_route, tier ORDER BY r.http_route, tier LIMIT 100
```

## 8. What this plan deliberately does not do

- Parallelize statements inside a request. Each request holds one pooled
  connection and the pooler caps the fleet; fewer statements beats concurrent ones.
- Add Redis or another store. The local `TTLCache` plus the existing Modal Dict
  backend cover every cache in this plan.
- Rewrite the heavy queries in the same PRs as the trip work. They are separate
  problems with separate evidence (EXPLAIN plans), so they get separate PRs.
- Change the workers' placement. Same distance problem, different scale and cost;
  decide after Phase 0's numbers land.
