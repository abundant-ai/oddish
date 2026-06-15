import asyncio
import json
import sys
from oddish.db import get_storage_client
from oddish.db.storage import StorageClient


async def main():
    trial_id = sys.argv[1]
    st = get_storage_client()
    base = StorageClient._trial_prefix(trial_id)
    keys = await st.list_keys(base)

    async def show_text(suffix, tail=3000):
        for k in keys:
            if k.endswith(suffix):
                try:
                    t = await st.download_text(k)
                    print(f"\n----- {suffix} ({len(t)} chars) -----")
                    print(t[-tail:])
                except Exception as e:
                    print(f"  err {suffix}:", e)
                return
        print(f"  (no {suffix})")

    # full agent_result + metadata from result.json
    for k in keys:
        if k.endswith("/result.json") and "trials/" in k and "task-" in k:
            d = await st.download_json(k)
            if "agent_result" in d:
                ar = d.get("agent_result") or {}
                print("agent_result:", json.dumps({kk: vv for kk, vv in ar.items() if kk != "rollout_details"}, default=str)[:1500])
                print("exception_info:", json.dumps(d.get("exception_info"), default=str)[:800])
            break

    await show_text("agent/setup/return-code.txt")
    await show_text("agent/setup/stdout.txt", 2000)
    await show_text("agent/claude-code.txt", 4000)


if __name__ == "__main__":
    asyncio.run(main())
