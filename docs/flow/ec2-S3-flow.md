# EC2 S3 Flow: Orphan Reconciliation

## Runtime flow

1. `cleanup_orphaned_queue_state` asks the registered EC2 backend for a
   deployment-scoped inventory before opening the shared cleanup transaction.
2. The backend materializes its namespaced AWS credentials, resolves the AWS
   account once with STS, and pages instances carrying both protected
   `oddish:managed=true` and current `oddish:deployment` tags. Network work has
   bounded connect, read, retry, and overall timeouts.
3. The cleanup transaction performs the normal stale-worker reap, then loads
   only the `TRIAL` worker jobs relevant to the inventory's worker-job IDs,
   trial IDs, and EC2 handles. Heartbeat freshness is calculated against
   PostgreSQL `NOW()` so Modal and database clock skew cannot change a verdict.
4. The pure orphan policy produces one logged and metered verdict per instance:
   preserve, terminate, or refuse ownership.
5. Termination candidates are added to the existing provider/external-ID target
   set. The database transaction commits before any provider teardown begins.
6. Post-commit teardown calls the registered EC2 backend. It re-describes each
   instance, verifies the protected account, deployment, and managed tags, and
   only then calls `TerminateInstances`.
7. Temporary AWS profile material is removed after all teardown attempts,
   including error paths. Cleanup continues when inventory or an individual
   teardown call fails, while emitting an explicit error metric and log.

## Decision policy

- The startup grace period is fixed at 30 minutes.
- The hard maximum instance age is fixed at 14 hours and is inclusive.
- Wrong managed, deployment, or account ownership is always refused before age
  is considered.
- A fresh running worker linked to the exact EC2 handle preserves the instance.
- A fresh running worker for the same trial preserves an unlinked startup
  instance only when that worker has no EC2 handle or its handle is absent from
  the current inventory. This preserves a newly launched replacement before its
  handle is persisted without preserving an older retry instance after the
  replacement is known.
- Within the startup grace period, otherwise unowned instances are preserved.
- After grace, missing owners, terminal owners, stale owners, and handle
  mismatches are termination candidates.
- At or beyond 14 hours, an owned managed instance is a termination candidate
  even if its worker still appears live.

## Suspicious-part sweep

- Inventory account identity is resolved once per snapshot and reused by every
  decision and ownership check in that batch.
- Raw liveness SQL excludes soft-deleted rows and requires `kind = 'TRIAL'` and
  `subject_table = 'trials'`; the ID/handle match alternatives are explicitly
  parenthesized.
- Sequential teardown is idempotent: instances already shutting down or
  terminated are a no-op, and a sweep deduplicates targets. Rare concurrent
  actors can both observe a running instance and call AWS termination; EC2's
  termination API is idempotent, so the lifecycle is deliberately at-least-once
  rather than claiming a cross-process exactly-once guarantee.
- Malformed or missing AWS launch timestamps are refused during inventory with
  a visible metric instead of being silently assigned an unsafe age.

## Verification

The combined provider, execution parity, routing, hosted policy, cancellation,
retry, stale-worker, cleanup, and reconciliation suite completed with 335 passed
and 4 skipped tests. A live AWS canary still requires operator-provided AMI,
network, key-pair, and IAM credentials.
