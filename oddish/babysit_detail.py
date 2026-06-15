import asyncio
from sqlalchemy import text
from oddish.db import get_session, init_db

EXP = "d8d31fa9"


async def main():
    await init_db()
    q = text(
        """
        SELECT t.id, t.agent, t.task_id, t.status, t.reward,
               t.output_tokens, t.error_message,
               t.created_at, t.started_at, t.finished_at, t.heartbeat_at,
               EXTRACT(EPOCH FROM (NOW() - t.created_at))/60 AS age_min,
               EXTRACT(EPOCH FROM (NOW() - t.heartbeat_at))/60 AS hb_min
        FROM trials t
        WHERE t.experiment_id = :exp AND t.deleted_at IS NULL
        ORDER BY t.agent, t.status, t.created_at
        """
    )
    async with get_session() as s:
        rows = (await s.execute(q, {"exp": EXP})).mappings().all()

    for r in rows:
        st = (r["status"] or "").lower()
        em = (r["error_message"] or "").replace("\n", " ")[:70]
        age = r["age_min"]
        hb = r["hb_min"]
        age_s = f"{age:5.0f}m" if age is not None else "  -  "
        hb_s = f"{hb:5.0f}m" if hb is not None else "  -  "
        print(
            f"{r['agent'][:7]:7} {r['task_id'][:34]:34} {st:8} "
            f"rew={str(r['reward']):>5} tok={str(r['output_tokens']):>7} "
            f"age={age_s} hb={hb_s} {em}"
        )


if __name__ == "__main__":
    asyncio.run(main())
