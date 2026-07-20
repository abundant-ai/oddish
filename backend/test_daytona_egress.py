"""One-off: test sandbox egress from a Daytona sandbox.

The Claude Code agent-install step runs, INSIDE a Daytona sandbox,
``curl -fsSL https://claude.ai/install.sh | bash`` which pulls the binary from
``downloads.claude.ai``. Probe trials are failing with::

    curl: (28) Failed to connect to downloads.claude.ai port 443 ... Couldn't connect to server

That is a network-reachability failure, and the network that matters is the
*Daytona sandbox's*, not your laptop or the Modal worker. This script spins up a
plain ephemeral Daytona sandbox in the same org/region and curls the relevant
hosts so you can see exactly which egress is blocked — without running Harbor.

Caveat: a plain sandbox uses the default image, not Harbor's docker:dind trial
sandbox. Egress allowlists are org/region-level, so this reflects the same
policy; if you need the exact DinD context, run a real probe instead.

    cd backend
    .venv/bin/python test_daytona_egress.py
"""

from __future__ import annotations

import asyncio
import pathlib

from daytona import AsyncDaytona, CreateSandboxFromSnapshotParams, DaytonaConfig

# Hosts to probe. downloads.claude.ai is the one the install pulls from;
# registry.npmjs.org tells us whether the npm fallback path would work; the
# others are controls to distinguish "this host blocked" from "all egress dead".
HOSTS = [
    "https://downloads.claude.ai/install.sh",
    "https://claude.ai/install.sh",
    "https://registry.npmjs.org/",
    "https://api.anthropic.com/",
    "https://www.google.com/",
]


def _load_api_key() -> str:
    """Read DAYTONA_API_KEY from the environment or backend/.env.local."""
    import os

    if os.environ.get("DAYTONA_API_KEY"):
        return os.environ["DAYTONA_API_KEY"]
    env_path = pathlib.Path(__file__).parent / ".env.local"
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("DAYTONA_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("DAYTONA_API_KEY not found in env or backend/.env.local")


async def main() -> None:
    daytona = AsyncDaytona(DaytonaConfig(api_key=_load_api_key()))
    print("Creating ephemeral Daytona sandbox...")
    sbx = await daytona.create(CreateSandboxFromSnapshotParams(ephemeral=True))
    print(f"  sandbox id: {sbx.id}\n")
    try:
        for host in HOSTS:
            # -sS quiet but show errors; --max-time bounds the hang; print the
            # HTTP code + connect/total timing, or the curl exit code on failure.
            cmd = (
                "curl -sS -o /dev/null "
                "-w 'HTTP %{http_code}  connect=%{time_connect}s total=%{time_total}s' "
                f"--max-time 30 {host} 2>&1 || echo \"  -> curl exit $?\""
            )
            res = await sbx.process.exec(cmd)
            print(f"{host}\n  exit={res.exit_code}  {res.result}\n")
    finally:
        print("Deleting sandbox...")
        await daytona.delete(sbx)
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
