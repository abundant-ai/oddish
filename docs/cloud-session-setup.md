# Cloud session setup (Claude Code on the web)

How to give a cloud Claude Code session the things the eval runbooks assume.
The short version: **secrets come from the environment's variable config, code
comes from attached repos, and everything else is rebuilt by the SessionStart
hook.** Nothing is copied from a laptop.

## What a fresh cloud container actually has

A cloud session runs in an ephemeral container: this repo is cloned fresh, the
container is reclaimed when the session ends, and anything not committed is
lost. It starts with no virtualenv, no `~/.env`, no shell profile, no agent
CLIs, and no credentials beyond what the environment injects.

That is why runbook steps written against a laptop fail verbatim. Most of them
do not need porting — they need translating:

| Runbook input (laptop)                    | Cloud equivalent                                                        |
| ----------------------------------------- | ----------------------------------------------------------------------- |
| `~/oddish/oddish/.venv/bin/oddish`         | `<repo>/oddish/.venv/bin/oddish` — built by the hook, and on `PATH`      |
| `~/.grok/bin/grok`                         | installed in-session on demand (see below)                              |
| `XAI_API_KEY` via `~/.env`                 | set on the environment; the hook writes it back into `~/.env`           |
| `~/.oddish` credentials                    | **does not exist** — see below                                          |
| `~/cyberpipeline/*.sh` and similar         | must live in a git repo and be attached to the session                  |

### There is no `~/.oddish` credentials file

The CLI reads credentials from the environment only — `ODDISH_API_KEY` and
`ODDISH_API_URL`, resolved in `oddish/src/oddish/cli/config.py`
(`get_api_key`, `get_api_url`). There is no credentials file, no `oddish login`,
and no dotfile to copy. Setting the two environment variables is the whole of
CLI auth, locally and in the cloud alike.

## 1. Secrets: set them on the environment

Environment variables are configured per environment at
[claude.ai/code](https://claude.ai/code) → Environments → *(your environment)* →
environment variables. They are injected into every session that environment
starts, so this is a one-time setup rather than a per-session step.

Set what the work needs:

| Variable            | Needed for                                                   |
| ------------------- | ------------------------------------------------------------ |
| `ODDISH_API_KEY`    | every `oddish` command that talks to the hosted API           |
| `ODDISH_API_URL`    | only to target a non-default API (preview, self-hosted)       |
| `XAI_API_KEY`       | the grok CLI, and any script that authenticates to xAI        |
| `ANTHROPIC_API_KEY` | CUA verifiers on the open-internet tasks                      |
| `OPENAI_API_KEY` / `META_API_KEY` | vendor routes that read them                  |

Two things these variables are *not* for:

- **Trial credentials.** A vendor key used by an agent inside a trial must be a
  **Modal secret** on the worker function, not a session variable — see the
  runbook's prereqs. A key set here is available to the session driving the
  eval, not to the sandboxes running it.
- **The log-bucket export.** Those AWS credentials are short-lived STS tokens
  (~1h) with their own refresh cycle, so the hook deliberately leaves `AWS_*`
  alone. Export them per-session as the runbook describes.

## 2. Code: attach the repo it lives in

A cloud session can only see repositories attached to it. Scripts that live
only on a laptop — a `~/cyberpipeline` working directory, `author_prompt.sh`,
`ship.sh` — are unreachable no matter how the environment is configured. Push
them to a repo first; then a session can be given access to it, and can clone it
alongside this one.

The eval runbook's background-agent prompt already assumes this: it asks for
access to `abundant-ai/oddish`, `abundant-ai/harbor`, and
`abundant-ai/swe-marathon`. Add whichever repo holds the pipeline scripts to
that list.

## 3. Everything else: the SessionStart hook

`.claude/hooks/session-start.sh` runs when a cloud session starts and:

- runs `uv sync --frozen --extra server` in `oddish/`, producing
  `oddish/.venv/bin/oddish` (the `--extra server` matches AGENTS.md; without it
  the test suite cannot import `sqlalchemy`)
- puts that venv, `~/.grok/bin`, and `~/.local/bin` on `PATH`
- writes the configured credentials into `~/.env` for scripts that `source` it,
  in a marked block so anything else in the file survives
- prints a status line per input, and names any missing secret

It is remote-only (`CLAUDE_CODE_REMOTE`), idempotent, and always exits 0 — a
partial bootstrap degrades a session rather than blocking it. It never invents
a credential: if a variable is not set on the environment, the hook says so.

To enable it, register it in `.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/session-start.sh"
          }
        ]
      }
    ]
  }
}
```

The hook takes effect for sessions started after that lands on the default
branch.

### Installing the grok CLI

The hook reports the grok CLI but does not install it: it is a network install
most sessions never need. When a task does need it:

```bash
curl -fsSL https://x.ai/cli/install.sh -o /tmp/grok-install.sh
bash /tmp/grok-install.sh
export PATH="$HOME/.grok/bin:$PATH"
```

This is the same installer `OddishGrokBuildAgent.install()` runs inside Harbor
sandboxes (`oddish/src/oddish/workers/agents/grok_build.py`). Note the
distinction: that in-sandbox install is what trials use, and it happens whether
or not the CLI is present in the session. You only need it in the session
itself if *you* are driving grok directly.

## Verifying

The hook's own output is the check — it prints one line per input at session
start. To re-run it by hand:

```bash
CLAUDE_CODE_REMOTE=true .claude/hooks/session-start.sh
```
