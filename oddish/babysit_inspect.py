import asyncio
import json
import sys
from oddish.db import get_storage_client
from oddish.db.storage import StorageClient


async def inspect(trial_id):
    st = get_storage_client()
    base = StorageClient._trial_prefix(trial_id)
    keys = await st.list_keys(base)
    print(f"\n=== {trial_id} ===")
    print("prefix:", base)
    # summarize keys: count screenshots, list non-screenshot keys
    shots = [k for k in keys if "screenshot" in k]
    others = [k for k in keys if "screenshot" not in k]
    print(f"  {len(keys)} keys total; {len(shots)} screenshots")
    for k in others:
        print("  key:", k.replace(base, ""))
    # try result.json (the per-trial one, not the experiment summary)
    for k in keys:
        if k.endswith("result.json"):
            try:
                d = await st.download_json(k)
                if "verifier_result" not in d and "agent_result" not in d:
                    continue
                print("  -- result.json (trial) --")
                vr = d.get("verifier_result")
                print("    verifier_result:", json.dumps(vr)[:500])
                ei = d.get("exception_info")
                if ei:
                    print("    exception_info:", json.dumps(ei)[:400])
                ar = d.get("agent_result")
                if isinstance(ar, dict):
                    print("    agent_result keys:", list(ar.keys()))
            except Exception as e:
                print("  result.json read err:", e)
    # network-resolution.json (closed-internet)
    for k in keys:
        if "network-resolution" in k:
            try:
                d = await st.download_json(k)
                print("  network-resolution.json:", json.dumps(d)[:600])
            except Exception as e:
                print("  netres read err:", e)
    # exception.txt
    for k in keys:
        if k.endswith("exception.txt"):
            try:
                t = await st.download_text(k)
                print("  exception.txt:", t[:400])
            except Exception as e:
                print("  exc read err:", e)


async def main():
    for tid in sys.argv[1:]:
        await inspect(tid)


if __name__ == "__main__":
    asyncio.run(main())
