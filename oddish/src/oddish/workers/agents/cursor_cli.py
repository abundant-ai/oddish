"""Oddish Cursor CLI wrapper: disable Cursor's provider-side WebFetch tool.

Cursor's WebFetch/WebSearch tools execute on Cursor's cloud over the same API
channel as its model calls, so container network policy can never reach them
(verified: under network_mode=no-network, shell curl fails "could not resolve
host" while the agent's webFetch still pulls github). Cursor's own documented
lever is a permissions *deny* in its config file -- "deny takes precedence over
allow", so it overrides --yolo's allow-all. There is NO tool-exclusion CLI flag
(the previous `--exclude-tools ...` approach was a no-op: cursor-agent silently
ignores the unknown flag and kept its web tools). So we merge
`permissions.deny = ["WebFetch(*)"]` into the agent's own ~/.cursor/cli-config.json
after install and before the run.

Proven: with this deny in place, a cursor-cli trial that previously solved a
closed-book task by fetching the upstream source made ZERO webFetch/webSearch
calls and failed honestly instead.
"""

from __future__ import annotations

from typing import Any

from harbor.agents.installed.cursor_cli import CursorCli

# Merge (never overwrite) the deny into cursor's own config so its defaults and
# the schema-required `permissions.allow` array are preserved. Runs as the agent,
# so ~ resolves to the home where `cursor-agent --version` wrote cli-config.json.
_DENY_WEBFETCH_CMD = r"""python3 - <<'PY' || true
import json, os
try:
    p = os.path.expanduser("~/.cursor/cli-config.json")
    try:
        with open(p) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    perms = cfg.setdefault("permissions", {})
    perms.setdefault("allow", [])
    deny = perms.setdefault("deny", [])
    if "WebFetch(*)" not in deny:
        deny.append("WebFetch(*)")
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w") as f:
        json.dump(cfg, f, indent=2)
    print("oddish: denied WebFetch(*) in", p)
except Exception as e:
    # Fail OPEN: never break a cursor trial because the deny-write failed. Worst
    # case the web tool stays enabled (the pre-existing behavior), not a crash.
    print("oddish: WebFetch deny skipped:", e)
PY"""


class OddishCursorCli(CursorCli):
    """Stock Cursor CLI with its provider-side WebFetch tool denied via config."""

    def __init__(
        self,
        *args: Any,
        disable_web_tools: bool = False,
        **kwargs: Any,
    ) -> None:
        self._oddish_disable_web_tools = disable_web_tools
        super().__init__(*args, **kwargs)

    async def install(self, environment: Any) -> None:
        # super().install() runs `cursor-agent --version`, which creates the
        # default cli-config.json; merge the deny in right after, as the agent.
        await super().install(environment)
        if self._oddish_disable_web_tools:
            await self.exec_as_agent(environment, command=_DENY_WEBFETCH_CMD)
