# Evaluation and operations tools

These are manually invoked tools, not modules called by the application. Lack of
an application import is not evidence that an operator tool is unused. Before
retiring one, check whether its operational purpose has a replacement.

## Read-only diagnostics

Run the Modal commands from `backend/` using its installed dependencies. Modal is
the cloud runtime that executes the diagnostic with the worker image and secrets.
`MODAL_SECRET_ENVIRONMENT` selects the secret environment (default `main`);
these tools use `oddish-prod` and `oddish-logfire` from that environment. Check
the intended environment before running. Output may contain internal logs.

| Command | Purpose |
| --- | --- |
| `modal run check_alembic_current_modal.py` | Read both database migration-version tables; compare with `uv run alembic heads` in `oddish/` and `backend/`. Does not apply migrations. |
| `modal run probe_docker_failures_modal.py --all-trials --hours 72 --limit 200` | Compare unscored trial state, saved result exceptions, and worker errors. Pending/running trials are not automatically failures; storage-read errors are reported separately. |
| `modal run probe_audit_trail_modal.py --trial-id <id>` | Trace a diagnostic trial through its database row, worker attempts, stored files, and parsed agent output. Downloads artifacts temporarily; does not rerun the trial. |
| `modal run scripts/tail_budget_report.py --eid <experiment-id>` | Measure serialized trajectory sizes and the fraction retained at different proposed byte limits. Token counts are estimates; this does not change reviewer limits. |
| `modal run ops_compare_token_costs.py --since <YYYY-MM-DD> --model <provider/model> --input-rate <rate> --cache-read-rate <rate> --cache-write-rate <rate> --output-rate <rate>` | Compare recorded trial costs with an estimate using explicit USD-per-million-token rates. Does not change costs or reconcile invoices. |

The cost report includes nondeleted completed trials in nondeleted experiments,
filtered to billed or native non-probe/non-combined runs. This is the tool's
comparison population, not a claim to reproduce every dashboard billing filter.

## Preview historical delivery metadata

`python -m oddish.core.ingest.delivery_backfill` prepares a local preview from
the consolidated delivery-research JSON. It preserves source rows, resolves
explicit task IDs against an optional organization inventory, and reports identity
and category conflicts. It does not write database rows or set delivery approval.
The organization inventory it matches against comes from `oddish delivery
inventory`, and a reviewed plan is previewed or applied with `oddish delivery
import-history` (apply is admin-only). Both go through the hosted API; see
[the backfill runbook](delivery-metadata-backfill.md) for scope and tests.

## Rebuild task statistics

From `oddish/`, with the intended database configured:

```sh
uv run python -m oddish.core.backfill_task_version_model_metrics --batch 200 --limit 500
```

This writes aggregate statistics for existing task versions using the current
aggregation code. It does not launch evaluations. Every batch commits separately;
resume with `--after-id <last-logged-version-id>`. Without that argument a rerun
starts at the beginning. Versions with no eligible trials do not stall paging.

## Urgent hotfix release rehearsal

Read-only dry-run of the promote gates (never pushes):

```sh
.github/scripts/promote/rehearse_urgent_hotfix.sh
.github/scripts/promote/rehearse_urgent_hotfix.sh <staging_commit_sha>
```

Procedure, required checks, rollback, and the filled rehearsal record:
[urgent-hotfix-release.md](urgent-hotfix-release.md).

## Evaluation instructions and historical material

Use [the SWE-Marathon runbook](swe-marathon-eval-runbook.md) for the evaluation
workflow. Its existing advice is unchanged by this restoration.
The [August campaign record](archive/swe-marathon-terra-campaign.md) is restored
verbatim from `eval-ops/PLAN.md` before #1628, including its original historical
qualification. The referenced campaign scripts remain in Git history.

## Scope of restoration from PR #1628

The diagnostics above and the statistics rebuild command have repeatable uses
beyond the original incident. Their tests and this index accompany restoration.
The old `eval-ops/` dispatch/delete loops embed campaign paths and experiments;
the historical record is retained without reactivating them. `eval-ops/passk.py`
also hardcodes local paths and duplicates the maintained success-rate estimator.

The `legacy_discover/transfer/validate` scripts implement a specific Sauron import;
the old cost-repricing and queue-key backfills implement historical data repairs.
They remain in Git at `205aa996^` for an explicitly scoped migration or repair,
rather than returning as current operational instructions. The Daytona teardown
test treated any lookup exception as proof of deletion, including network/auth
errors; it is not restored as a reliable test. The egress probe's incident notes
are recoverable there too, but its default sandbox does not reproduce a task's
image and network policy. Obsolete UI routes/components and uncalled internal
helpers are outside this operational restoration.
