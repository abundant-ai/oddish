# cc_chat Daytona snapshot (pre-baked claude-code + harbor)

Every cc_chat sandbox runs `npm install -g @anthropic-ai/claude-code` and
`pip install harbor` at provision time. Since sandboxes are ephemeral, that
~minute of install happens on **every new chat** — it's the dominant chunk of
the "long initial buffer" before the agent responds.

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
# Dockerfile.cc-chat
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
daytona snapshot create cc-chat-base-v1 --dockerfile Dockerfile.cc-chat
# verify
daytona snapshot list | grep cc-chat-base-v1
```

(Equivalently: start one sandbox, run the install commands above, and snapshot
it — whichever your Daytona account supports.)

## Point the backend at it

Set the env var on the Modal deployment (the backend reads it via
`settings.cc_chat_daytona_snapshot`, env prefix `ODDISH_`):

```
ODDISH_CC_CHAT_DAYTONA_SNAPSHOT=cc-chat-base-v1
```

That's all the code needs — `RealDaytonaClient` passes it to
`CreateSandboxFromSnapshotParams.snapshot`, and `install()` no-ops because the
tools already exist. Unset the var to fall back to the default image + install.

## Versioning

Bump the snapshot name (`-v2`, …) and update the env var whenever claude-code
or harbor needs upgrading; old chats are ephemeral so nothing pins the old one.
Keep the harbor version here in sync with `claude_code_runtime.py`.
