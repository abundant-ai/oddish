import asyncio
from sqlalchemy import text
from oddish.db import get_session, init_db

EXP = "d8d31fa9"


async def main():
    await init_db()
    q = text(
        """
        SELECT t.id, t.agent, t.task_id, t.status, t.reward, t.output_tokens,
               t.environment, t.harbor_config
        FROM trials t
        WHERE t.experiment_id = :exp AND t.deleted_at IS NULL
          AND lower(t.status::text) IN ('success','failed')
        ORDER BY t.task_id, t.agent
        """
    )
    async with get_session() as s:
        rows = (await s.execute(q, {"exp": EXP})).mappings().all()
    for r in rows:
        hc = r["harbor_config"] or {}
        ci = hc.get("closed_internet") if isinstance(hc, dict) else None
        print(f"{r['task_id'][:34]:34} {r['agent'][:7]:7} {r['status'].lower():8} "
              f"rew={str(r['reward']):>5} tok={str(r['output_tokens']):>7} env={r['environment']} "
              f"id={r['id']}")


if __name__ == "__main__":
    asyncio.run(main())
