"""Full audit of every task manifest in s3://ralphbench-logs/.

For each task: n_trials, all (agent, model) cells with counts + passes,
experiment_id(s), task_version(s), shortfalls, overrides used, missing
required fields, and exception_classes diversity.
"""
from __future__ import annotations
import os, json, boto3
from collections import Counter, defaultdict
dst = boto3.client('s3', aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
    aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'], region_name='us-west-2')

# 20 tasks (skip 'reward-hacking' which is a tooling prefix, and 'trash').
TASKS = [
    'biofabric-rust-rewrite','embedding-eval','excel-clone',
    'find-network-alignments','jax-pytorch-rewrite','kubernetes-rust-rewrite',
    'mastodon-clone','nextjs-vite-rewrite','parameter-golf',
    'post-train-ifeval','ruby-rust-port','rust-c-compiler','rust-java-lsp',
    's3-clone','slack-clone','stripe-clone','trimul-cuda',
    'vliw-kernel-optimization','wasm-simd','zstd-decoder',
]
assert len(TASKS) == 20

REQUIRED_FIELDS = {'agent','model','provider','reward','status','attempts',
                   'max_attempts','task_version_id','started_at','finished_at',
                   'origin','artifact_layout','artifact_prefix'}

print(f"{'task':24s} {'n':>4s} {'cells':>5s} {'5/5':>4s} {'<5':>4s} {'pass':>4s} {'ovr':>4s} {'exp':6s}  experiments / task_versions / shortfalls / overrides")
totals = Counter()
for tn in TASKS:
    try:
        m = json.loads(dst.get_object(Bucket='ralphbench-logs', Key=f"{tn}/_manifest.json")['Body'].read())
    except Exception as e:
        print(f"  {tn:24s}  MISSING MANIFEST: {e}")
        continue
    n = m['n_trials']; trials = m['trials']
    assert n == len(trials), f"{tn}: n_trials={n} but len(trials)={len(trials)}"
    eids = m.get('experiment_ids') or ([m['experiment_id']] if m.get('experiment_id') else [])
    versions = m.get('task_versions') or ([m['task_version_id']] if m.get('task_version_id') else [])
    pc = Counter((e['agent'], e['model']) for e in trials.values())
    n_pass = sum(1 for e in trials.values() if e.get('reward') == 1.0)
    cells = len(pc)
    full = sum(1 for v in pc.values() if v == 5)
    short_pairs = [(p, v) for p, v in pc.items() if v < 5]
    over_pairs  = [(p, v) for p, v in pc.items() if v > 5]
    overrides = [tid for tid, e in trials.items() if e.get('override_validity')]
    # Field completeness
    missing_fields = defaultdict(int)
    for tid, e in trials.items():
        for f in REQUIRED_FIELDS:
            if f not in e or e[f] is None:
                missing_fields[f] += 1
    # Exception class breakdown
    exc_count = Counter()
    for e in trials.values():
        for c in (e.get('exception_classes') or []) or ['(clean)']:
            exc_count[c] += 1
    short_str = ', '.join(f"{p[0]}/{p[1].split('/')[-1]}={v}/5" for p, v in sorted(short_pairs))
    over_str  = ', '.join(f"{tid}({e['agent']}/{e['model'].split('/')[-1]})"
                          for tid in overrides
                          for e in [trials[tid]])
    print(f"  {tn:24s} {n:>4d} {cells:>5d} {full:>4d} {len(short_pairs):>4d} {n_pass:>4d} {len(overrides):>4d} {len(eids):>4d} v={len(versions)}  exps={eids}  vers={versions}")
    if short_pairs:
        print(f"  {'':24s}  SHORT: {short_str}")
    if over_pairs:
        print(f"  {'':24s}  OVER:  {over_str}")
    if overrides:
        print(f"  {'':24s}  OVERRIDES: {over_str}")
    # Exception breakdown
    nonclean = {k:v for k,v in exc_count.items() if k != '(clean)' and not k.startswith('Agent')}
    if nonclean:
        print(f"  {'':24s}  non-clean/non-AgentTimeout exception classes: {dict(nonclean)}")
    totals['n_trials'] += n
    totals['n_pass'] += n_pass
    totals['cells_total'] += cells
    totals['cells_full'] += full
    totals['cells_short'] += len(short_pairs)
    totals['overrides'] += len(overrides)
    totals['tasks'] += 1

print()
print(f"=" * 78)
print(f"TOTALS across {totals['tasks']} tasks:")
print(f"  trials:    {totals['n_trials']}")
print(f"  passes:    {totals['n_pass']}")
print(f"  cells:     {totals['cells_total']}  ({totals['cells_full']} at 5/5, {totals['cells_short']} short)")
print(f"  overrides: {totals['overrides']}")
