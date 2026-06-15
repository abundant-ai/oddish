"""Precise provider auth/402 detector.

Scans agent logs (claude-code.txt) and result.json of finished trials for
genuine MiniMax/Moonshot API auth/billing failures, ignoring task-prompt text.
"""
import asyncio
import re
from sqlalchemy import text
from oddish.db import get_session, init_db, get_storage_client
from oddish.db.storage import StorageClient

EXP = "d8d31fa9"

# Precise markers that only appear in a *provider API error response*,
# not in arbitrary task prompts.
# Narrow to markers that appear in a *provider API error event*, not in
# agent-written task source (e.g. a stripe-clone implementing 401 responses).
MARKERS = [
    r'"api_error_status"\s*:\s*"?(4|5)\d\d',   # non-null HTTP error status in CC result event
    r'"is_error"\s*:\s*true.*?(payment required|insufficient|invalid api key|unauthorized)',
    r"payment required",
    r"insufficient (balance|quota|credit)",
    r"account balance",
    r"(api\.minimax\.io|api\.moonshot\.ai)[^\n]{0,80}\b(401|402|403)\b",
]
RX = re.compile("|".join(MARKERS), re.IGNORECASE)


async def main():
    await init_db()
    q = text(
        """
        SELECT t.id, t.agent, t.task_id, t.status, t.output_tokens
        FROM trials t
        WHERE t.experiment_id = :exp AND t.deleted_at IS NULL
          AND ( lower(t.status::text) = 'failed'
                OR (lower(t.status::text)='success' AND COALESCE(t.output_tokens,0) < 2000) )
        ORDER BY t.created_at DESC
        """
    )
    async with get_session() as s:
        rows = (await s.execute(q, {"exp": EXP})).mappings().all()

    st = get_storage_client()
    hits = []
    print(f"Scanning {len(rows)} failed/no-op trials for provider auth/402...")
    for r in rows:
        tid = r["id"]
        base = StorageClient._trial_prefix(tid)
        try:
            keys = await st.list_keys(base)
        except Exception as e:
            print(f"  {tid}: list err {e}")
            continue
        found = []
        for k in keys:
            if k.endswith("claude-code.txt") or k.endswith("/result.json"):
                try:
                    txt = await st.download_text(k)
                except Exception:
                    continue
                for m in RX.finditer(txt):
                    seg = txt[max(0, m.start() - 40):m.start() + 60].replace("\n", " ")
                    found.append((k.replace(base, ""), seg))
        if found:
            hits.append((tid, r["agent"], r["status"], found[:3]))
    if not hits:
        print("No genuine provider auth/402 markers found. Keys OK.")
    else:
        print(f"\n!!! {len(hits)} trials with possible auth/402 markers:")
        for tid, ag, sstatus, fs in hits:
            print(f"  {tid} [{ag}] {sstatus}")
            for kk, seg in fs:
                print(f"     {kk}: ...{seg}...")


if __name__ == "__main__":
    asyncio.run(main())
