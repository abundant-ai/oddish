# AgentENV / AENV Backend

Oddish can run Harbor trials on AgentENV through Harbor's existing `e2b`
environment. AgentENV implements the E2B-compatible API, so no Harbor task
format changes are required.

## Host Requirements

AgentENV itself must run on Linux hosts with:

- Linux kernel 6.8+
- `/dev/kvm`
- privileged runtime access for Firecracker, ublk, networking, and OverlayBD
- an AgentENV gateway reachable from Oddish workers

This is not a good fit for normal Modal sandboxes because they do not expose
nested KVM. Use Modal for the Oddish control plane/workers, and point them at an
external AgentENV cluster for the sandbox runtime.

## Oddish Configuration

Set these on the worker/backend deployment:

```bash
export ODDISH_AGENTENV_API_URL=http://agentenv-gateway.agentenv-system:8000
export ODDISH_AGENTENV_SANDBOX_URL=$ODDISH_AGENTENV_API_URL
export ODDISH_AGENTENV_API_KEY=e2b_000000

export E2B_API_URL=$ODDISH_AGENTENV_API_URL
export E2B_SANDBOX_URL=$ODDISH_AGENTENV_SANDBOX_URL
export E2B_API_KEY=$ODDISH_AGENTENV_API_KEY
export E2B_ACCESS_TOKEN=$ODDISH_AGENTENV_API_KEY
```

`ODDISH_AGENTENV_API_URL` opt-in registers the backend in Oddish. Harbor still
sees the provider as `e2b`, so runs use:

```bash
oddish run -d terminal-bench@2.0 -a codex -m gpt-5.5 --env e2b
```

## Deployment Shape

Recommended first deployment:

1. Bring up AgentENV single-node or Kubernetes on KVM-capable hosts.
2. Keep the AgentENV API on a trusted private network or put it behind an auth
   proxy. AgentENV's README currently warns that the API has no built-in
   authorization.
3. Configure Oddish Modal workers with the environment variables above.
4. Submit an explicit `--env e2b` smoke trial.
5. Capture whether Harbor's E2B SDK path exposes sandbox IDs reliably enough for
   hung-run teardown. Oddish includes best-effort AgentENV DELETE support when it
   receives an external sandbox id.

## Current Scope

This first pass gives us Harbor/Modal compatibility and capability routing. It
does not yet expose AgentENV-native pause/resume/fork controls as Oddish replay
APIs. Those should be layered separately on trial snapshots/trajectory metadata
once we decide the replay data model.
