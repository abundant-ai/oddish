# Registry-auth e2e runbook (DinD, Modal + Daytona)

End-to-end test for per-run container-registry credentials (PR #422). Proves the
inner Docker-in-Docker daemon pulls a **private** image only when a credential is
supplied via `--registry-login`. Written for an AI agent to re-run unattended.

Design code: [registry-auth.md](registry-auth.md). The login shim lives in
[`patches.py`](../oddish/src/oddish/workers/harbor/patches.py) and wraps both
`_DaytonaDinD` and `_ModalDinD`.

## Why it must be a docker-compose task

The credential is written to `/root/.docker/config.json` inside the **inner DinD
daemon** before `docker compose build`/`up`. A single-container task does not pull
through that daemon, so it would not exercise the feature. The task below forces
DinD by shipping `environment/docker-compose.yaml`, and makes the *only* pull a
private one by having `main`'s Dockerfile do `FROM <private ghcr image>` — no
Docker Hub dependency to confound the result.

## Prerequisites

1. **A live API.** Either prod or a PR preview. Preview URL pattern:
   `https://abundant-ai-preview--oddish-pr-<N>-api.modal.run`. After pushing the
   PR branch, confirm the `pr-preview.yml` run is green and `GET /docs` → 200.
   The preview's API and worker share one env, so the Fernet key matches on both
   sides (encryption ↔ decryption) automatically.
2. **An oddish API key** for that API.
3. **A GHCR PAT with `read:packages`** that can pull the private test image
   (`ghcr.io/abundant-ai/experiments/*`). A plain `gh` token (repo/read:org) is
   **not** enough — it 401s. Verify:
   ```bash
   IMG=abundant-ai/experiments/h2o-import-sql-exec-gate
   B64=$(printf '%s' "$GHCR_USERNAME:$GHCR_TOKEN" | base64)
   TOK=$(curl -s -H "Authorization: Basic $B64" \
     "https://ghcr.io/token?service=ghcr.io&scope=repository:$IMG:pull" \
     | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
   curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $TOK" \
     -H "Accept: application/vnd.oci.image.index.v1+json" \
     "https://ghcr.io/v2/$IMG/manifests/latest"   # expect 200
   ```
4. **Secrets in a gitignored file** `/Users/charles/oddish/.env.local` (sourced,
   never echoed):
   ```sh
   ODDISH_API_KEY=...
   ODDISH_API_URL=https://abundant-ai-preview--oddish-pr-<N>-api.modal.run
   GHCR_USERNAME=...
   GHCR_TOKEN=...                # ghcr PAT, read:packages
   GHCR_IMAGE=ghcr.io/abundant-ai/experiments/h2o-import-sql-exec-gate:latest
   ```

The CLI runs as `uv run oddish ...` from `/Users/charles/oddish/oddish`.
Load secrets in each shell with: `set -a; . /Users/charles/oddish/.env.local; set +a`.

## The test task

Scaffold a compose/DinD task whose `main` base IS the private image:

```
<task>/task.toml
<task>/instruction.md
<task>/environment/Dockerfile          # FROM <private ghcr image>
<task>/environment/docker-compose.yaml # services: main: command sleep infinity
<task>/solution/solve.sh               # touch /app/DONE
<task>/tests/test.sh                   # reward.txt = 1 if /app/DONE else 0
```

- `task.toml`: a normal task with `[verifier]`, `[agent]`, `[environment]`
  (`build_timeout_sec` ~1200). **Do not** set `[environment].docker_image` — that
  switches Harbor to the prebuilt path; leaving it unset uses the build path,
  which builds `main` from `environment/Dockerfile` (where the private `FROM`
  pull happens).
- `environment/Dockerfile`:
  ```dockerfile
  FROM ghcr.io/abundant-ai/experiments/h2o-import-sql-exec-gate:latest
  WORKDIR /app
  ```
- `environment/docker-compose.yaml` (presence triggers DinD; merges with
  Harbor's generated `main: build:` — do **not** set a build context here, it
  would resolve relative to Harbor's compose dir, not the task env dir):
  ```yaml
  services:
    main:
      command: ["sh", "-c", "sleep infinity"]
  ```
- `tests/test.sh` must write the reward, or the verifier raises
  `RewardFileNotFoundError` after a fully successful run:
  ```bash
  #!/usr/bin/env bash
  mkdir -p /logs/verifier
  if [ -f /app/DONE ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
  exit 0
  ```

A ready-made copy is kept at `/Users/charles/oddish-worktrees/ghcr-compose-e2e`.

## Step 1 — negative control (no creds → 401)

```bash
cd /Users/charles/oddish/oddish
set -a; . /Users/charles/oddish/.env.local; set +a
unset ODDISH_DOCKERHUB_USERNAME ODDISH_DOCKERHUB_TOKEN
TASK=/Users/charles/oddish-worktrees/ghcr-compose-e2e
uv run oddish run "$TASK" -a claude-code -m claude-haiku-4-5 -e daytona \
  --n-trials 1 --background --no-watch
```

Poll `uv run oddish status <task_id>` until the trial leaves `environment setup`.
It will fail and go `retrying`. Pull logs and confirm the cause is auth:

```bash
uv run oddish pull <task_id> --logs --no-files --type task -o /tmp/neg
grep -rhiE "load metadata|401|unauthorized|compose build failed" /tmp/neg
```

Expected (the failure the feature fixes):
```
#3 [internal] load metadata for ghcr.io/abundant-ai/experiments/h2o-import-sql-exec-gate:latest
#3 ERROR: failed to authorize: failed to fetch anonymous token: ... 401 Unauthorized
RuntimeError: docker compose build failed: ...
```

Cancel the retries: `yes | uv run oddish cancel <task_id>`.

## Step 2 — positive runs on both backends (parallel)

Run one per backend (`-e daytona`, `-e modal`). These are independent; run them as
parallel subagents. The GHCR token has no commas, so the comma-separated value is
safe.

```bash
cd /Users/charles/oddish/oddish
set -a; . /Users/charles/oddish/.env.local; set +a
TASK=/Users/charles/oddish-worktrees/ghcr-compose-e2e
uv run oddish run "$TASK" -a claude-code -m claude-haiku-4-5 -e <daytona|modal> \
  --n-trials 1 \
  --registry-login "registry=ghcr.io,username=$GHCR_USERNAME,token=$GHCR_TOKEN" \
  --background --no-watch
```

Poll status; identify *your* trial (highest trial index). SUCCESS = it moves past
`environment setup` into agent execution. Pull logs for evidence:

```bash
uv run oddish pull <task_id> --logs --no-files --type task -o /tmp/pos
grep -rhiE "load metadata|401|Building compose|Starting compose|DONE" /tmp/pos
```

Expected (authenticated — no 401, build and up succeed):
```
Building compose services inside DinD sandbox...
Starting compose services inside DinD sandbox...
```
The agent then runs in the container, creates `/app/DONE`, and the verifier writes
`reward.txt`. Once env setup is confirmed, `yes | uv run oddish cancel <task_id>`
also exercises teardown (the shim removes `/root/.docker/config.json`).

## Pass criteria

| Run | Backend | Expectation |
|-----|---------|-------------|
| Negative (no `--registry-login`) | daytona | `docker compose build` fails with `401 Unauthorized` on the private `FROM` |
| Positive | daytona | build + up succeed (no 401), agent executes |
| Positive | modal | build + up succeed (no 401), agent executes |

The negative→positive flip on the **same task** with the **only change being the
credential** is the proof. Both DinD strategies (`_DaytonaDinD`, `_ModalDinD`) are
covered.

## Notes / gotchas

- All `oddish run`/`status`/`cancel`/`pull` reuse one stable `task_id` derived from
  the task dir; each `oddish run` appends a new experiment + trial. Old cancelled
  trials stay in the list — always act on the newest trial index.
- `oddish cancel` is interactive; pipe `yes |` in automation.
- Without a credential the trial retries env setup up to 6× — cancel after the
  first attempt confirms the 401 to avoid wasted sandbox spins.
- `ODDISH_REGISTRY_AUTH_KEY` must be identical on the API and worker. In a single
  Modal app (prod or a preview) it is, since both read the same env; a decrypt
  failure logs a loud `error` and the trial runs unauthenticated.
- The credential never lands in `trials.harbor_config`; it rides the encrypted
  `worker_jobs.payload.registry_auth_enc` and is scrubbed when the row reaches a
  terminal state.
