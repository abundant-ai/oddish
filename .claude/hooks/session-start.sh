#!/bin/bash
# SessionStart bootstrap for Claude Code on the web.
#
# Cloud sessions get a fresh container with the repo cloned and nothing else:
# no virtualenv, no agent CLIs, no ~/.env, no shell profile. This script
# reconstructs the pieces the eval runbooks assume, so a session can run
# `oddish` without a manual setup round-trip first.
#
# It never invents credentials. Secrets come from the environment's variable
# config (claude.ai/code -> Environments); this script only bridges them to the
# places local tooling looks for them, and reports what is missing.
#
# See docs/cloud-session-setup.md.
set -uo pipefail

# Local machines already have a venv, a shell profile, and the agent CLIs on
# PATH. Only the disposable remote container needs rebuilding.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

REPO_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
ENV_HOME="${HOME:-/root}"

# Credentials bridged into ~/.env for scripts that `source` it. AWS is
# deliberately absent: the log-bucket creds are short-lived STS tokens with
# their own refresh cycle (runbook section 6), not session-long config.
BRIDGED_VARS=(
  ODDISH_API_KEY
  ODDISH_API_URL
  XAI_API_KEY
  XAI_API_KEYS
  ANTHROPIC_API_KEY
  OPENAI_API_KEY
  META_API_KEY
)

status_lines=()
note() { status_lines+=("$1"); }

# --- oddish CLI ------------------------------------------------------------
# Replaces the local ~/oddish/oddish/.venv that runbooks reference by path.
# --extra server matches the documented setup (AGENTS.md, "Local Development");
# without it the test suite cannot import sqlalchemy.
venv_bin="$REPO_ROOT/oddish/.venv/bin"
if command -v uv >/dev/null 2>&1; then
  sync_log="$(cd "$REPO_ROOT/oddish" && uv sync --frozen --extra server 2>&1)" || \
    sync_log="$(cd "$REPO_ROOT/oddish" && uv sync --extra server 2>&1)"
  if [ -x "$venv_bin/oddish" ]; then
    note "oddish CLI      ok        $venv_bin/oddish"
  else
    note "oddish CLI      FAILED    uv sync did not produce the venv"
    printf '%s\n' "$sync_log" | tail -20 >&2
  fi
else
  note "oddish CLI      FAILED    uv not on PATH"
fi

# --- grok CLI --------------------------------------------------------------
# Reported, not installed: pulling and running a remote installer at session
# start is a network dependency the session cannot audit. Install it in-session
# when a task needs it -- docs/cloud-session-setup.md has the command.
if [ -x "$ENV_HOME/.grok/bin/grok" ]; then
  note "grok CLI        ok        $ENV_HOME/.grok/bin/grok"
else
  note "grok CLI        absent    see docs/cloud-session-setup.md to install"
fi

# --- ~/.env ----------------------------------------------------------------
# Bridges configured secrets to the file local scripts source. Rewrites only
# its own marker block so anything hand-added during the session survives.
env_file="$ENV_HOME/.env"
begin="# >>> oddish cloud bootstrap >>>"
end="# <<< oddish cloud bootstrap <<<"
bridged=()
block="$begin"$'\n'"# Generated at session start from the environment's variable config."
for var in "${BRIDGED_VARS[@]}"; do
  if [ -n "${!var:-}" ]; then
    block+=$'\n'"export $var=${!var@Q}"
    bridged+=("$var")
  fi
done
block+=$'\n'"$end"

if [ -f "$env_file" ] && grep -qF "$begin" "$env_file" 2>/dev/null; then
  kept="$(awk -v b="$begin" -v e="$end" \
    'index($0,b){s=1} !s{print} index($0,e){s=0}' "$env_file")"
else
  kept="$(cat "$env_file" 2>/dev/null)"
fi
umask 077
{ [ -n "$kept" ] && printf '%s\n' "$kept"; printf '%s\n' "$block"; } > "$env_file"
chmod 600 "$env_file"

if [ ${#bridged[@]} -gt 0 ]; then
  note "~/.env          ok        ${#bridged[@]} var(s): ${bridged[*]}"
else
  note "~/.env          empty     no known credentials set on this environment"
fi

# --- session env -----------------------------------------------------------
# One export, venv first: ~/.local/bin carries its own pytest/ruff/black/mypy,
# which would shadow the venv's if it were prepended afterwards.
#
# Written at most once. SessionStart also fires on resume, clear, and compact,
# and CLAUDE_ENV_FILE persists across those firings, so an unguarded append
# would stack a duplicate prefix onto PATH every time the session compacted.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  path_prefix=""
  [ -d "$venv_bin" ] && path_prefix="$venv_bin:"
  path_line="export PATH=\"$path_prefix$ENV_HOME/.grok/bin:$ENV_HOME/.local/bin:\$PATH\""
  if ! grep -qxF "$path_line" "$CLAUDE_ENV_FILE" 2>/dev/null; then
    printf '%s\n' "$path_line" >> "$CLAUDE_ENV_FILE"
  fi
fi

# --- report ----------------------------------------------------------------
# SessionStart stdout joins the session context, so the agent starts knowing
# what it has. Names and states only -- never values.
echo "Cloud bootstrap (docs/cloud-session-setup.md):"
printf '  %s\n' "${status_lines[@]}"

missing=()
for var in ODDISH_API_KEY XAI_API_KEY; do
  [ -z "${!var:-}" ] && missing+=("$var")
done
if [ ${#missing[@]} -gt 0 ]; then
  echo "  missing secrets: ${missing[*]}"
  echo "  -> set them on this environment at claude.ai/code (Environments ->"
  echo "     this environment -> environment variables), then start a new session."
fi

# Always succeed: a partial bootstrap should degrade the session, not block it.
exit 0
