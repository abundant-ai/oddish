# Daytona sandbox snapshot (pre-baked claude-code + harbor)

Every analyzer sandbox runs `npm install -g @anthropic-ai/claude-code` and
`pip install harbor` at provision time. Since sandboxes are ephemeral, that
~minute of install happens on **every new sandbox**.

Building a snapshot with both tools already installed turns
`ClaudeCodeRuntime.install()` into two cheap existence checks (it skips any
tool that's already present), cutting provisioning to a few seconds.

## What the snapshot must contain

The runtime checks for these exact paths/imports and only skips the install
when they're satisfied (see `claude_code_runtime.py`):

- `test -x /home/daytona/.npm-global/bin/claude` succeeds, **and**
- `python -c 'import harbor'` succeeds (harbor `0.5.0`).

So the install must run as the `daytona` user with the same npm prefix the
runtime uses (`/home/daytona/.npm-global`).

## Build it

Using a Dockerfile on top of Daytona's default sandbox image:

```dockerfile
# Dockerfile.sandbox
FROM daytonaio/sandbox:latest          # or your org's current base

USER daytona
ENV NPM_PREFIX=/home/daytona/.npm-global
RUN mkdir -p "$NPM_PREFIX" \
 && npm config set prefix "$NPM_PREFIX" \
 && npm install -g @anthropic-ai/claude-code \
 && pip install --user --quiet harbor==0.5.0
```

Create the named snapshot from it (Daytona CLI):

```bash
daytona snapshot create sandbox-base-v1 --dockerfile Dockerfile.sandbox
# verify
daytona snapshot list | grep sandbox-base-v1
```

(Equivalently: start one sandbox, run the install commands above, and snapshot
it — whichever your Daytona account supports.)

## Point the backend at it

Set the env var on the Modal deployment (env prefix `ODDISH_`):

```
ODDISH_AGENT_DAYTONA_SNAPSHOT=sandbox-base-v1
```

The analyzer resolves `settings.analyzer_snapshot`, which reads
`ODDISH_AGENT_DAYTONA_SNAPSHOT` first and falls back to the legacy
`ODDISH_CC_CHAT_DAYTONA_SNAPSHOT` (named for the removed cc_chat feature;
prod still sets it, so the fallback keeps existing deployments working).
`RealDaytonaClient` passes the name to
`CreateSandboxFromSnapshotParams.snapshot`, and `install()` no-ops because the
tools already exist. Unset both vars to fall back to the default image +
install at provision time.

## Versioning

Bump the snapshot name (`-v2`, …) and update the env var whenever claude-code
or harbor needs upgrading; sandboxes are ephemeral so nothing pins the old
one. Keep the harbor version here in sync with `claude_code_runtime.py`.

The analyzer agent has no use for harbor, but a leaner analyzer-only image
would not help: `install()` checks claude-code and harbor independently, so it
would still attempt harbor's `pip install` on every sandbox.
