# Batched task file loading

Task and experiment drawers request the first page of root, solution, tests,
and environment in one request with bounded small-file previews. Other file bodies
load when selected. Task links retain `version`, `drawer`, `finding`, `taskPane`,
`taskFile`, and `taskLines`; trial tab/file/line addresses remain independent.

For the conventional task-page tree, four directory requests become one. Private
experiment drawers previously requested recursive listings with inline file bodies;
they now request the same bounded directory batch and read the selected body directly.
Trial artifact routes also share organization approval and trial lookup in one
database session, then release the connection before reading storage.

## API contract

`GET /tasks/{id}/files?version=7&directories=&directories=solution&directories=tests&directories=environment&recursive=0&inline=0&presign=0&limit=100&previews=true`

The hosted, standalone, and `/public/experiments/{token}/tasks/{id}/files`
routes accept the same opt-in parameters. The public route still verifies token
membership before resolving the requested version. Ordinary calls retain their
existing JSON/NDJSON responses and recursive CLI download behavior.

The batch response contains `task_id`, resolved `version`, `source_hash`, and
`directories`, a mapping from normalized relative directory paths to the existing
non-recursive listing shape. Each page retains `files`, `dirs`, `cursor`, and
`truncated`. The empty key addresses root. Continue a directory with the existing
`prefix=...&cursor=...&recursive=0` request, retaining its version and content hash.

There are at most eight requested directories and at most 1,000 entries per
page (the browser requests 100). Duplicate normalized paths are listed once.
Absolute/traversal paths are rejected before storage I/O. Batch requests must set
`recursive`, `inline`, and `presign` false and cannot specify `prefix`, `cursor`,
or `stream=true`.

Storage resolves the selected archive or expansion once per request. Published
immutable expansions skip legacy validation as before; mutable legacy layouts
retain their archive/manifest checks. Archive-only batches load/parse the archive
once. Directory LIST operations run concurrently against that selected source.

With `previews=true`, directory batches add `content` to at most 16 small files
from those pages, prioritizing `instruction.md`, then sorting by path. Each preview
is at most 32 KiB and all previews together at most 256 KiB. Archive text is reused;
expanded members are read concurrently with a one-second deadline per member.
Failed, binary, oversized, and unselected members retain on-demand reads.
`previews=true` without `directories` is rejected.

Hosted definition listings and body reads combine organization approval and exact
task/version selection in one SQL statement for ordinary credentials. Bound
analysis credentials retain their additional resource checks. The database
connection is released before storage work. Warm publisher-owned immutable
`vN-revisions/<32-hex-token>/` archives reuse their cached ETag without HEAD while
archive bytes remain cached; legacy mutable paths still revalidate.

## Browser behavior

`useTaskFileTree` holds directory data and pagination in SWR, including data loaded
after the initial batch. Cache keys separate user, organization, task API URL,
version, and known content hash. Public tokens are part of their separate URLs.
A response that started before panel metadata is also registered under its known
hash, so the next opening can reuse that exact revision.

The browser reuses listings for 30 seconds and refreshes older entries. The
existing panel poll detects in-place overwrites; a continuation page from another
hash refreshes the initial inventory instead of mixing revisions. Late pages
write only their captured cache key, including after navigation to another version.

Task-name hover or keyboard focus starts one directory/preview prefetch after 150 ms.
Rapid movement replaces the pending intent; an opening consumes the same SWR
request. Prefetch does not write browser history or select a file.
Hidden trial task panes do not initiate a file batch on trial selection.

An explicit URL-selected file reads immediately, even while its tree is pending,
or when it falls outside the first page. A requested line beyond the initial
100 KB preview, or on its potentially incomplete final line, automatically loads
the remaining file; the completed body writes
only to the file/version cache key captured when that request started.
Receiving a file's size from the directory listing does not restart its body read.
The selected file tracks its own response hash independently of its directory
listing. A late matching panel hash leaves both its request key and painted
content in place; a differing hash requests the new contents. This applies
whether the body or panel metadata arrives first, including full-file reads.
Existing wrapper-directory discovery, ancestor expansion, per-directory pagination,
and missing historical evidence
messages remain. Incoming file addresses set selection directly; they do not echo
as user selection callbacks. This prevents a cached version transition from clearing
the file or its line range during Back/Forward navigation.

## Verification

The storage tests assert four concurrent LIST calls, one legacy validation, zero
validation HEADs for a published immutable expansion, one archive load for an
archive-only batch, per-directory cursors, and rejection before I/O of invalid paths.

Backend integration tests use disposable PostgreSQL and cached API identities.
Trial artifact reads use one connection checkout and release it before deliberately
blocked storage I/O. Revoking organization approval rejects the following request
before storage. Public batch tests retain share membership and exact-version scope.

From `frontend/`, run:

```sh
node --test tests/file-list-revision.test.ts tests/task-file-sections.test.ts tests/task-files-panel-actions.test.ts
E2E_REVIEW_FIXTURES=1 pnpm exec playwright test -c playwright.files.config.ts
```

The browser suite runs real components against a local fixture app on port 3217;
it needs no Clerk credentials or production API. It covers batch reuse, hover/focus,
missing historical files, Back/Forward/reload, line selection, account/org isolation,
late continuation pages, overwrite invalidation, and older server responses.
The dashboard CI workflow runs `file-loading.spec.ts` and `user-ui-layout.spec.ts`
separately with `E2E_REVIEW_FIXTURES=1` and `playwright.files.config.ts`.
The default authenticated-app configuration
excludes `file-loading.spec.ts`, whose `/file-cache` route and seeded task IDs
exist only in the fixture app. CI uploads this suite's `fixture-test-results`.

Observed on 2026-09-10 against implementation commit `fd40bf898`:

| Check | Result |
| --- | --- |
| Storage, task/public API, metrics, missing objects, CLI downloads, exact trial attempts | `161 passed` |
| Authorization session reuse, approval revocation, bound analysis keys, summary routes | `81 passed` |
| Exact task-file source resolution against PostgreSQL | `1 passed` |
| Structured log forwarding, standalone routes, public sharing/export | `16 passed` |
| Browser suite, including delayed file bodies and partially previewed line links | `41 passed (44.0s)` |
| File revision, section classification, and task action unit tests | `18 passed` |
| Frontend TypeScript and ESLint on changed production components/hooks | Passed |

Ruff passes the changed Python modules and tests except the standalone server
module's 16 pre-existing `E402` import-order findings, verified unchanged against
the base commit. PostgreSQL tests used a disposable local database; browser tests
used the fixture API. No production data or latency measurement was used for
these checks.

Production latency improvements require a new recording. Compare the same task
IDs and historical versions with empty and populated caches, recording time to
selected-file content, batch completion, SQL/checkouts, storage operations, and
unused prefetched requests. The tests establish less duplicated work and preserved
behavior; they do not establish a production latency percentage.

Definition bundle follow-up verification (2026-09-10): 63 storage/endpoint tests,
22 authorization/session tests, 21 bound-credential restriction tests,
40 source/storage regression tests, and 17 browser
file-loading tests passed. PostgreSQL tests checked one SQL statement, approval
revocation, missing versions, and another organization's task. The browser test
switched between bundled files with zero individual body reads. TypeScript and
ESLint passed. These are local checks with mocked storage, not staging latency
measurements.
