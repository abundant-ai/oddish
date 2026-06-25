---
name: sauron-cli
description: "Use when working with the Sauron CLI to authenticate, list benchmark runs, summarize run status, fetch scoped S3 credentials, pull run artifacts, or inspect and download Sauron exports."
---

# Sauron CLI

## Operating Rules

- Treat Sauron run state as live. Query the CLI before answering about current runs or exports.
- The CLI is read-only for run inspection and artifact pulls. Do not modify Sauron data or GitHub branches as part of this skill.
- Never print, persist, or commit bearer tokens, cookies, temporary AWS credentials, or share tokens. If credentials must be emitted for the user, warn that stdout contains secrets.
- Prefer browser-backed CLI token auth from `sauron auth login`. Use cookie auth only when CLI token auth is unavailable, and prefer `--cookie-file` over raw cookies.
- Use absolute output paths for artifact pulls and export downloads.

## Quick Orientation

1. Locate and inspect the CLI:

```bash
command -v sauron
sauron --help
```

If working inside the Sauron app repo, `pnpm sauron --help` is also valid. The package exposes `bin/sauron`, and local development usually runs through `tsx cli/index.ts`.

2. Remember the run path shape:

```text
<org>/<repo>/<pr>/<run>
```

Examples:

```text
abundant-ai/nov-5-export/pr-123/run-456
abundant-ai/nov-5-export/123/run-456
```

Plain numeric PRs are normalized to `pr-<number>`.

3. Default production base URL:

```text
https://www.abundant.observer
```

Use `--base-url <url>` or `SAURON_BASE_URL` for local or preview deployments.

## Authentication

Check auth first when the user asks to access private runs:

```bash
sauron auth status
```

Preferred login:

```bash
sauron auth login
sauron auth status
```

`sauron auth login` starts a temporary localhost callback server, prints an authorize URL, and opens that URL in the user's browser by default. The browser flow goes through `/api/cli/auth/authorize`, uses the user's Clerk session, asks the user to confirm "Authorize CLI", then redirects a Sauron-issued CLI token back to the local callback.

If the browser cannot be opened automatically, use:

```bash
sauron auth login --no-open
```

Then open the printed URL manually in a browser where the user can sign in to Sauron. For slow sign-in flows, increase the callback wait:

```bash
sauron auth login --timeout 300
```

After success, the CLI stores the token for the current base URL under `~/.config/sauron/auth.json` with `0600` permissions. Verify with `sauron auth status` before running `ls`, `pull`, `creds`, or `exports`. `SAURON_API_TOKEN` or `--api-token` can provide a token directly when browser login is unavailable.

Share-token access is read-only and run-scoped:

```bash
SAURON_SHARE_TOKEN=<token> sauron auth status <org>/<repo>/<pr>/<run>
sauron --share-token <token> status <org>/<repo>/<pr>/<run>
```

Cookie auth is a fallback for Clerk-backed access:

```bash
chmod 600 /absolute/path/to/cookies.txt
sauron --cookie-file /absolute/path/to/cookies.txt auth status
```

Avoid `--cookie` and `SAURON_COOKIE` unless there is no safer option because they can leak through shell history or process environments.

## Common Workflows

### List PRs Or Runs

Use `ls` to discover valid run paths:

```bash
sauron ls abundant-ai/nov-5-export --prs
sauron ls abundant-ai/nov-5-export --pr 123
sauron ls abundant-ai/nov-5-export --limit 50
sauron ls abundant-ai/nov-5-export --json
```

`ls` requires user auth, not just a share token.

### Summarize A Run

Use `status` for pass/fail totals, agent summaries, experiment metadata, and optional task matrix details:

```bash
sauron status abundant-ai/nov-5-export/pr-123/run-456
sauron status abundant-ai/nov-5-export/pr-123/run-456 --full
sauron status abundant-ai/nov-5-export/pr-123/run-456 --json
```

Prefer `--json` when scripting, comparing agents, extracting pass rates, or feeding another tool.

### Pull Run Artifacts

Use `pull` to download S3 artifacts through scoped temporary credentials returned by Sauron:

```bash
sauron pull abundant-ai/nov-5-export/pr-123/run-456 \
  --out /absolute/path/to/sauron-pull
```

Useful filters:

```bash
sauron pull <run-path> --out /absolute/path/to/pull --agent codex --task my-task
sauron pull <run-path> --out /absolute/path/to/pull --attempt 1
sauron pull <run-path> --out /absolute/path/to/pull --logs --trajectory --analysis --results
sauron pull <run-path> --out /absolute/path/to/pull --task-files
```

Agent and task filters accept plain substring matches or `*` wildcards. If no category flags are set, all matching artifacts are pulled. Every pull writes `sauron-pull-manifest.json`; inspect it for `filesWritten`, `filesSkipped`, and `errors`.

### Get Scoped S3 Credentials

Use `creds` only when another AWS-aware tool needs direct S3 access:

```bash
sauron creds <run-path> --format env
sauron creds <run-path> --format json
```

These are temporary AWS credentials scoped to the run prefixes. Treat the output as secret material and do not paste it into final answers unless explicitly requested.

### Inspect And Download Exports

Existing Sauron exports are handled through `exports`:

```bash
sauron exports list <run-path>
sauron exports status <export-id>
sauron exports status <export-id> --contents
sauron exports download <export-id> --out /absolute/path/to/export.zip
```

Use `exports download` for already-created export archives. This skill should not create new exports unless the user explicitly asks and the CLI exposes a create command in the checked version.

## Troubleshooting

- `No auth configured`: run `sauron auth login`, pass `--api-token`, or set `SAURON_SHARE_TOKEN` for run-scoped status access.
- `requires user auth`: share tokens are insufficient for `ls`, `creds`, `pull`, and exports; use CLI token or cookie auth.
- `Expected run path`: ensure the path has exactly `<org>/<repo>/<pr>/<run>`.
- `Sauron did not return S3 credentials`: verify user auth and run access with `sauron auth status <run-path>`.
- Empty or partial pulls: inspect `sauron-pull-manifest.json`, rerun with narrower `--agent`, `--task`, `--attempt`, or category flags, and check manifest errors.
