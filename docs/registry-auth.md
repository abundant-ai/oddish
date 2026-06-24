# Per-run container-registry credentials

## The problem

Multi-service Harbor tasks ship a `docker-compose.yaml` that pulls public images
(`postgres`, `redis`, the clone services, …). Those pulls happen inside the
trial sandbox's **inner Docker-in-Docker daemon**, which is unauthenticated.
Docker Hub rate-limits anonymous pulls per source IP, so trials fail at
environment setup with:

```
docker compose up failed: ...
postgres Error toomanyrequests: You have reached your unauthenticated pull rate
limit. https://www.docker.com/increase-rate-limit
```

The oddish runner is shared, so its egress IP burns through the anonymous quota
quickly. The fix is to let **whoever triggers the run** supply **their own**
registry login so the daemon pulls authenticated.

## Design goals

- **Per-user, not shared.** The token is never a global oddish/Modal secret;
  everyone brings their own (a Docker Hub PAT, a GHCR token, …).
- **Never stored in the durable trial config.** It does not go in
  `trials.harbor_config`.
- **Secure across the queue.** oddish decouples submission from execution (the
  worker claims `worker_jobs` rows from Postgres), so the token must cross the
  database. It does so **Fernet-encrypted** and rides the transient per-trial
  `worker_jobs.payload`.
- **Logged off after the run.** The sandbox's `~/.docker/config.json` is removed
  on teardown. The encrypted ciphertext stays on the transient `worker_jobs` row
  while retries (which re-claim the same row) can re-authenticate, and is scrubbed
  from the payload as soon as that row reaches a terminal state (SUCCESS / FAILED /
  CANCELLED).
- **Uniform across every trigger path.** All paths funnel through
  `POST /tasks/sweep`, so one field carries the credential everywhere.

## Lifecycle

```
 supply                       transit (encrypted)            use                         scrub
┌────────────────────┐   ┌──────────────────────────┐   ┌────────────────────────┐   ┌──────────────────────┐
│ oddish run          │   │ POST /tasks/sweep         │   │ worker: TrialJobHandler │   │ Harbor DinD .stop()   │
│  --registry-login   │──▶│  registry_auth (SecretStr)│──▶│  decrypt → ContextVar   │──▶│  rm ~/.docker/config  │
│ ODDISH_DOCKERHUB_*  │   │  encrypt → worker_jobs    │   │ Harbor DinD wait→login: │   │ worker: reset var +   │
│ API registry_auth   │   │  .payload.registry_auth_enc│  │  write ~/.docker/config │   │  scrub payload row    │
└────────────────────┘   └──────────────────────────┘   │  before compose build/up│   └──────────────────────┘
                                                          └────────────────────────┘
```

Code map:

| Stage   | Where |
|---------|-------|
| Supply (CLI/env) | `oddish/src/oddish/cli/run.py` (`--registry-login`), `oddish/src/oddish/registry_auth.py` (`parse_registry_login`) |
| Supply (API) | `RegistryAuth` + `registry_auth` field on `TaskSweepSubmission`/`TaskSubmission` in `oddish/src/oddish/schemas.py` |
| Encrypt + enqueue | `oddish/src/oddish/queue.py` (`_encrypt_submission_registry_auth`, `_trial_job_payload`) |
| Crypto | `oddish/src/oddish/registry_auth.py` (`encrypt_credentials`/`decrypt_credentials`, Fernet) |
| Decrypt + publish | `oddish/src/oddish/workers/jobs/handlers.py` (`TrialJobHandler.run`) |
| Authenticate daemon | `oddish/src/oddish/workers/harbor_patches.py` (write `~/.docker/config.json` after daemon-ready, before `compose build`/`up`; `rm` on teardown) — applied to both `_DaytonaDinD` and `_ModalDinD` |

The daemon login is done by **writing `/root/.docker/config.json`** (base64
`user:token` under the right registry key), not by running `docker login`, so
the raw token is never placed in process argv. The config blob is passed to the
sandbox exec via an env var and base64-decoded in-VM.

## How to supply credentials (every trigger path)

> Use a Docker Hub **access token (PAT)**, not your account password:
> <https://app.docker.com/settings/personal-access-tokens> → *Generate* with
> at least *Public Repo Read-only* scope.

### 1. Local `oddish run`

```bash
# Explicit flag (repeatable; registry defaults to docker.io)
oddish run ./my-task -a codex -m openai/gpt-5.5 \
  --registry-login "username=$DOCKERHUB_USER,token=$DOCKERHUB_PAT"

# …or via environment (read automatically)
export ODDISH_DOCKERHUB_USERNAME="$DOCKERHUB_USER"
export ODDISH_DOCKERHUB_TOKEN="$DOCKERHUB_PAT"
oddish run ./my-task -a codex -m openai/gpt-5.5

# A private (non-Hub) registry:
oddish run ./my-task -a codex \
  --registry-login "registry=ghcr.io,username=$GH_USER,token=$GH_PAT"
```

### 2. Experiments repo (`/oddish` PR comment)

The `oddish-experiment.yml` workflow already runs `oddish run` with
`ODDISH_API_KEY` in the **Submit to Oddish** step. Add two lines to that step's
`env:` (and add the `DOCKERHUB_*` repo/org **Actions secrets**). No change to the
command or manifest is needed — `oddish run` reads the env automatically:

```yaml
      - name: Submit to Oddish
        env:
          ODDISH_API_KEY: ${{ secrets.ODDISH_API_KEY }}
          ODDISH_DOCKERHUB_USERNAME: ${{ secrets.DOCKERHUB_USERNAME }}
          ODDISH_DOCKERHUB_TOKEN: ${{ secrets.DOCKERHUB_TOKEN }}
        run: |
          ...
```

### 3. harbor-forge-v2 (`/full`, `/light`, `/cheat`)

Same pattern. `run-full-trials.yml` / `run-light-trials.yml` /
`run-cheat-trials.yml` install oddish from this repo and submit with
`ODDISH_API_KEY`. Add to the submit step's `env:`:

```yaml
        env:
          ODDISH_API_KEY: ${{ secrets.ODDISH_API_KEY }}
          ODDISH_DOCKERHUB_USERNAME: ${{ secrets.DOCKERHUB_USERNAME }}
          ODDISH_DOCKERHUB_TOKEN: ${{ secrets.DOCKERHUB_TOKEN }}
```

(harbor-forge installs oddish via `uv tool install …#subdirectory=oddish`, so it
picks up `--registry-login` / `ODDISH_DOCKERHUB_*` automatically once this lands.)

### 4. Direct API (`POST /tasks/sweep`)

```jsonc
{
  "task_id": "…",
  "configs": [{ "agent": "codex", "model": "openai/gpt-5.5", "n_trials": 1 }],
  "registry_auth": [
    { "registry": "docker.io", "username": "alice", "token": "dckr_pat_…" }
  ]
}
```

`registry_auth` is **write-only**: it is never returned by any read endpoint and
is masked (`SecretStr`) in any server-side `model_dump`/log.

## Security model

- **In transit:** TLS to the API; the token is plaintext only in the request
  body and in worker process memory during the run.
- **At rest:** Fernet-encrypted before it touches Postgres. The data-protection
  key is oddish-managed (`ODDISH_REGISTRY_AUTH_KEY`, or derived from
  `ODDISH_DATABASE_URL` when unset) — this is **not** the user's token, just the
  envelope key shared by the API (encrypt) and worker (decrypt). Because the
  derived fallback is only as secret as the database URL, **set
  `ODDISH_REGISTRY_AUTH_KEY`** in the `oddish-prod` Modal secret in production so
  the envelope key is independent of (and rotatable apart from) the database; the
  worker logs a warning while running on the derived key.
- **Not in the trial config:** lives only on `worker_jobs.payload`, never on
  `trials.harbor_config`.
- **Idempotency:** excluded from the client idempotency key, so rotating the
  token does not split an otherwise-identical resubmit into duplicate trials.
- **Cleanup:** on sandbox teardown the shim removes `/root/.docker/config.json`
  (the daemon is logged off); the worker resets the context var. The encrypted
  ciphertext remains on the transient `worker_jobs` row only while retries can
  re-claim it, and is scrubbed from the payload once the row reaches a terminal
  state (SUCCESS / FAILED / CANCELLED). Use short-lived Docker Hub PATs and revoke
  them in the Hub UI when no longer needed.
- **Scope:** applies to multi-service (docker-compose / DinD) tasks on the
  Daytona and Modal backends. Single-container tasks don't pull through the inner
  daemon and are unaffected.
- **Misconfiguration is loud:** if the worker can't decrypt the credential
  (a present-but-undecryptable blob — almost always `ODDISH_REGISTRY_AUTH_KEY`
  differing between the API and worker, or rotated after submit), it logs an
  `error` saying the trial will run unauthenticated, rather than failing only
  later with an opaque `toomanyrequests`. Keep the key identical on both sides
  (set it explicitly in `oddish-prod` to avoid relying on the derived default).

### Known limitations

- The `~/.docker/config.json` content is handed to the sandbox exec as a base64
  blob via an env var. On the **Daytona** backend that value transits the
  sandbox-VM command line (`env ODDISH_DOCKERCFG_B64=… sh -c …`), so it is
  visible to `ps` *inside that single-tenant, ephemeral VM* (never on the oddish
  host). It is base64, not encryption, but the raw `user:token` is double-wrapped
  (base64 of a JSON that itself holds base64 `user:token`) and the file is
  written `0600` and removed on teardown. oddish does not log the command or the
  env value; failure logs redact the plaintext token. If your registry token is
  high-value, prefer a short-lived, narrowly-scoped PAT and revoke it after use.

## Testing

### Unit

```bash
cd oddish/oddish
pytest tests/test_registry_auth.py -q
```

Covers: registry-key normalization, `~/.docker/config.json` rendering, encrypt/
decrypt round-trip (token absent from ciphertext), resilient decrypt, CLI/env
parsing + de-dup (including tokens that contain commas), `RegistryAuth` rejecting
empty username/token, idempotency-key exclusion, `SecretStr` masking, and the
queue helpers (ciphertext built, plaintext token never in it).

The login shim itself is exercised against a fake DinD strategy in the same
spirit — confirm the write command contains no raw token and the base64 blob
decodes to the expected auths, and that teardown removes the config.

### End-to-end (reproduce the original failure, then fix it)

1. Pick a multi-service task that pulls Docker Hub images (e.g.
   `netty-maxcontentlength-oom-guard`, which pulls `postgres`, `sentry`, …).
2. **Reproduce:** run it without credentials and, ideally, from an IP that has
   already burned its anonymous quota — environment setup fails with
   `toomanyrequests`.
3. **Fix:** re-run with a Docker Hub PAT:
   ```bash
   ODDISH_DOCKERHUB_USERNAME=$USER ODDISH_DOCKERHUB_TOKEN=$PAT \
     oddish run --task <task_id> --experiment <exp> -a codex -m openai/gpt-5.5
   ```
   The trial should clear *Environment Setup* (no `toomanyrequests`).
4. **Verify cleanup:** the worker log shows
   `Authenticated DinD daemon for registries: https://index.docker.io/v1/`, and
   after the run the `worker_jobs` row for the trial has no `registry_auth_enc`
   key and `trials.harbor_config` never contained the token.

### CI dry-run

The experiments workflow supports `dry_run: true` (resolve tasks + build
`sweep.yaml` without submitting). Add the `DOCKERHUB_*` secrets, run a normal
`/oddish` (or `/full`) on a compose task, and confirm the trial passes
environment setup where it previously hit the rate limit.
