"""Read-only probe: do ledger trials with has_reward=false actually have a
reward file we're not detecting (reward.json / reward-float.txt / etc.), or are
they genuinely reward-less (harness errors / incompletes)?

Samples N such trials, lists each trial's S3 objects once, and reports which
reward-file variants (if any) are present -- so we know whether the transfer's
reward reader needs fallbacks beyond reward.txt. Writes nothing.

Usage:
    modal run backend/scripts/legacy_reward_probe.py                  # 500 false-reward trials
    modal run backend/scripts/legacy_reward_probe.py --sample 1000
    modal run backend/scripts/legacy_reward_probe.py --only-false False  # sample ALL trials
"""

from __future__ import annotations

from pathlib import Path

import modal

_parents = Path(__file__).resolve().parents
REPO = _parents[2] if len(_parents) > 2 else Path("/")

app = modal.App("oddish-legacy-reward-probe")
image = (
    modal.Image.debian_slim(python_version="3.13")
    .apt_install("git")
    .add_local_dir(str(REPO / "oddish"), remote_path="/oddish", copy=True, ignore=[".venv/", ".git/"])
    .run_commands("pip install uv", "cd /oddish && uv pip install --system '.[worker]'")
)
secret = modal.Secret.from_name("oddish-prod", environment_name="main")
aws_secret = modal.Secret.from_name("sauron-legacy")

DEFAULT_BUCKET = "abundant-github-workflows-bucket"


@app.function(image=image, secrets=[secret, aws_secret], timeout=1800)
async def probe(sample: int, only_false: bool) -> None:
    import asyncio
    import random
    from collections import Counter

    import aioboto3
    import asyncpg
    from oddish.config import settings

    conn = await asyncpg.connect(settings.asyncpg_url, statement_cache_size=0)
    where = "has_reward = false" if only_false else "TRUE"
    rows = [dict(r) for r in await conn.fetch(
        f"SELECT s3_prefix, agent_key FROM leg_trial_ledger WHERE {where}")]
    await conn.close()
    total_pop = len(rows)
    random.shuffle(rows)
    rows = rows[: max(1, sample)]
    print(f"population({where})={total_pop}  sampling={len(rows)}")

    region = settings.s3_region or "us-east-1"
    session = aioboto3.Session()
    sem = asyncio.Semaphore(32)

    filename_hist: Counter = Counter()   # distinct reward-file basenames seen
    with_any = 0
    none_baseline = 0
    none_real = 0

    def is_baseline(a: str) -> bool:
        return a.startswith("nop") or a.startswith("oracle")

    async with session.client("s3", region_name=region) as s3:
        pg = s3.get_paginator("list_objects_v2")

        async def check(r: dict):
            async with sem:
                names = set()
                async for page in pg.paginate(Bucket=DEFAULT_BUCKET, Prefix=r["s3_prefix"]):
                    for o in page.get("Contents", []):
                        base = o["Key"].rsplit("/", 1)[-1]
                        if "reward" in base.lower():
                            names.add(base)
                return r["agent_key"], names

        results = await asyncio.gather(*(check(r) for r in rows))

    for agent_key, names in results:
        if names:
            with_any += 1
            for n in names:
                filename_hist[n] += 1
        else:
            if is_baseline(agent_key):
                none_baseline += 1
            else:
                none_real += 1

    n = len(results)
    none_total = none_baseline + none_real
    print("=" * 60)
    print(f"sampled={n}")
    print(f"  with a reward file (any variant): {with_any}  ({100*with_any/n:.1f}%)")
    print(f"  with NO reward file at all:       {none_total}  ({100*none_total/n:.1f}%)")
    print(f"      of those: baseline(nop/oracle)={none_baseline}  real-agent={none_real}")
    print("reward-file variants found (basename -> count):")
    for name, c in filename_hist.most_common():
        print(f"  {c:>6}  {name}")
    print("-" * 60)
    if with_any == 0:
        print("VERDICT: has_reward=false is GENUINELY reward-less -> transfer's "
              "reward=None/FAILED is correct. No reward-format fix needed.")
    else:
        print("VERDICT: some false-reward trials DO carry a reward file "
              f"({100*with_any/n:.1f}%) -> add those variants to the transfer's "
              "reward reader (fallback order).")


@app.local_entrypoint()
def main(sample: int = 500, only_false: bool = True) -> None:
    probe.remote(sample=sample, only_false=only_false)
