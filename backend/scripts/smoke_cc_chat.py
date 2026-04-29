"""End-to-end smoke test for the CC chat feature.

Requires DAYTONA_API_KEY and ANTHROPIC_API_KEY in env. Hits real Daytona
and the real Anthropic API. Designed to be run in CI / pre-deploy, not
on every commit.

Exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_BASE = REPO_ROOT / "jobs"
FIXTURE_EXPERIMENT_ID = "2026-04-26__16-45-36"


async def _run() -> int:
    from api.services.cc_chat.daytona_client import RealDaytonaClient
    from api.services.cc_chat.file_store import LocalFileStore
    from api.services.cc_chat.orchestrator import CCChatOrchestrator

    daytona_key = os.environ.get("DAYTONA_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not daytona_key or not anthropic_key:
        print("skip: DAYTONA_API_KEY or ANTHROPIC_API_KEY missing")
        return 0

    orch = CCChatOrchestrator(
        daytona=RealDaytonaClient(api_key=daytona_key),
        file_store=LocalFileStore(base_path=FIXTURE_BASE),
        anthropic_api_key=anthropic_key,
        auto_stop_minutes=10,
    )

    sid = await orch.start(
        experiment_id=FIXTURE_EXPERIMENT_ID, org_id="smoke"
    )
    print(f"session: {sid}")

    saw_init = False
    saw_result = False
    async for ev in orch.send(
        session_id=sid,
        content="List the trial directories under jobs/ and tell me how many you found.",
    ):
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            saw_init = True
            print(f"init session_id: {ev.get('session_id')}")
        if ev.get("type") == "result":
            saw_result = True
            print(f"result: {ev}")

    await orch.close(session_id=sid)

    if not saw_init:
        print("FAIL: never saw system/init event with session_id")
        return 1
    if not saw_result:
        print("FAIL: never saw result event")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
