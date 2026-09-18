# Historical delivery metadata backfill

This first implementation turns the September 10 research bundle into a local
preview of task identity, category evidence, and historical delivery membership.
It does not change the Oddish database. The remaining application work is tracked
in [delivery-metadata-todo.md](delivery-metadata-todo.md).

## Inputs

The input is the existing `consolidated-deliveries.json` produced by the delivery
research. Its shipment, pass-rate, ledger, repair, Nexus submission, and Nexus
validation rows retain their original fields and source links in `evidence`.
The planner understands this specific research format, not arbitrary spreadsheets.

The research directory moved to
`/Users/kyle/Desktop/abundant/oddish/delivery-records-research`.
Keep the private research data and generated previews outside the Git repository.
The source snapshot is dated September 10; running the planner on September 16
does not refresh it or establish today's QA or customer outcomes.

An optional inventory confirms that source task IDs belong to the intended
Oddish organization. Export it with the CLI, signed in to that organization:

```sh
oddish delivery inventory --output /absolute/path/inventory.json
```

The command calls `GET /deliveries/task-inventory`, which is scoped to the
signed-in organization by the API (never by a flag) and needs the ordinary
`tasks` scope. The query (`delivery_inventory.export_inventory_core`) runs on
a read session, pages tasks in groups of 500, includes retired tasks, and
filters both task and tag queries by organization. It exports current
IDs/hashes as current evidence only. Categories are collected from legacy
task tags, GitHub metadata, and current structured category tags; differing
values are retained. It does not export credentials, change rows, or enqueue
QA. The CLI refuses to overwrite an existing output file.

Inventory format:

```json
{
  "schema_version": "oddish-task-inventory-v1",
  "org_id": "the-intended-organization",
  "captured_at": "2026-09-16T12:00:00+00:00",
  "tasks": [
    {
      "id": "permanent-task-id",
      "name": "current-name",
      "org_id": "the-intended-organization",
      "categories": []
    }
  ]
}
```

Do not generate an inventory from the research's IDs and present it as an Oddish
export. An inventory establishes membership in a specific organization at a
specific time. The planner is an operator tool; its `--org-id` option is a scope
check, not an authentication or authorization boundary for a hosted API.

## Generate the preview

The planner only needs the Python standard library. From the repository root:

```sh
PYTHONPATH=oddish/src python3.13 -m oddish.core.ingest.delivery_backfill \
  --bundle /absolute/path/consolidated-deliveries.json \
  --org-id <organization-id> \
  --inventory /absolute/path/inventory.json \
  --output-dir /absolute/path/backfill-preview
```

Omit `--inventory` for an evidence-only first pass. It produces no confirmed
task IDs or metadata proposals. The output directory must not already exist.
The generated files are `plan.json`, a readable `README.md`,
`identity-conflicts.json` with each conflicting name group and its source
rows, and `evidence-raw.json`. Only `plan.json` is ever uploaded. It carries,
per source record, just the allowlisted columns the import uses (`FACT_FIELDS`
in the planner: names, IDs, category, customer, batch, dates, membership,
statuses). The verbatim rows, which include teammates' names and free-text
notes, go to `evidence-raw.json` keyed by record ID; keep that file with the
private research data. A record's `content_hash` covers its facts, so an edit
to a note is not a source change.

The plan contains:

- `task_profiles`: groups of source names connected by recorded aliases or
  explicit Oddish IDs, identity status, category assertions, and recipients.
- `metadata_proposals`: source-name/category additions for confirmed IDs;
  category conflicts never produce overwrite proposals. These describe future
  source-backed metadata records, not direct patches to today's `tasks.tags`.
- `delivery_observations`: source batch memberships, with customer-facing names
  and source dates. Current catalog membership and historical-only membership
  remain distinguishable. They are not asserted to be actual shipment attempts.
- `evidence`: stable source record IDs, allowlisted facts, source URLs, and
  content hashes. Repair states, returns, and Nexus statements remain
  attributed here; their wording stays in `evidence-raw.json`.
- `input_file_sha256`: hashes of the exact input bytes read. Embedded source
  checksums are retained from the research; this command does not redownload or
  reverify the original source files.

## Identity and unknown information

Explicit source IDs must exist in the supplied organization inventory. A missing
ID never falls back to a same-name task. Multiple source IDs in one connected
group, or a name that belongs to another inventory task, requires review.
Name-only matches remain candidates. There is no fuzzy matching.

The records do not establish historical shipped version IDs, content hashes,
programs, customer acceptance, or finalization. Those values stay unknown.
Pass-rate version lists and repair-tracker current versions cannot fill them.
The current inventory version also cannot fill them. Existing return and repair
statements are retained without turning them into customer-specific decisions.

History coverage is always `partial` in this phase. A missing customer observation
must never be presented as proof that the customer has never received the task.
Retired tasks can be matched for history; that does not make them selectable for
a new delivery.

## Repeated runs and source changes

Pass `--previous-plan /absolute/path/earlier/plan.json` to compare source evidence.
Record IDs depend on the source collection and logical row key, not row position
or the current value. Identical duplicate rows collapse; conflicting rows with
the same source key fail before output is written. Changed values retain the
source record ID and get a different content hash. Missing source rows are
reported for review; they are not deletion instructions.

This is stable import planning, not a database idempotency guarantee. The future
apply operation still needs uniqueness constraints, an import receipt, and fresh
organization/identity checks. A preview must not become an unrestricted write
payload. Changes to identities or inventory values also need a fresh review.

## Verification

The tests run without server dependencies or a database:

```sh
PYTHONPATH=oddish/src python3.13 -m unittest discover \
  -s oddish/tests -p test_delivery_backfill.py -v
```

They cover explicit IDs and aliases, reused names, conflicting IDs/categories,
organization mismatch, missing IDs, category no-ops, repeated/reordered inputs,
source edits/removals, multiple recipients, unknown historical versions,
existing category extraction, and refusal to replace previous output.
