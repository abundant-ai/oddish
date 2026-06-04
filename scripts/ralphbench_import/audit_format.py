"""Format-consistency audit across all 20 task manifests + 1499 trial entries.

Checks:
 1. Manifest top-level fields — what's present in every / some / none.
 2. Per-trial field union vs intersection (what's universally present).
 3. Type consistency per field across all entries.
 4. Value normalization: model strings, provider values, status casing,
    reward type, artifact_prefix format, source_oddish_* lineage,
    canonical_attempt_dir presence, exception_classes shape.
 5. Trial directory structure: every trial dir has top-level
    config.json + result.json + one canonical task-* attempt.
"""
from __future__ import annotations
import os, json, boto3
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
dst = boto3.client('s3', aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
    aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'], region_name='us-west-2')

TASKS = [
    'biofabric-rust-rewrite','embedding-eval','excel-clone',
    'find-network-alignments','jax-pytorch-rewrite','kubernetes-rust-rewrite',
    'mastodon-clone','nextjs-vite-rewrite','parameter-golf',
    'post-train-ifeval','ruby-rust-port','rust-c-compiler','rust-java-lsp',
    's3-clone','slack-clone','stripe-clone','trimul-cuda',
    'vliw-kernel-optimization','wasm-simd','zstd-decoder',
]

# Collect everything
manifests = {}
all_entries = []  # list of (task_name, trial_id, entry)
for tn in TASKS:
    m = json.loads(dst.get_object(Bucket='ralphbench-logs', Key=f"{tn}/_manifest.json")['Body'].read())
    manifests[tn] = m
    for tid, e in m['trials'].items():
        all_entries.append((tn, tid, e))

print(f"Total trials: {len(all_entries)} across {len(TASKS)} manifests\n")

# ---- 1. Top-level manifest fields ----
print("=" * 78)
print("1. Manifest top-level fields — presence per task")
print("=" * 78)
top_keys = Counter()
for tn, m in manifests.items():
    for k in m.keys():
        top_keys[k] += 1
print(f"{'field':35s} {'tasks_with':>10s}  type-set")
universal = []
partial = []
for k, n in sorted(top_keys.items(), key=lambda x:-x[1]):
    types = set(type(manifests[tn].get(k)).__name__ for tn in TASKS if k in manifests[tn])
    where = '20/20 UNIVERSAL' if n == 20 else f"{n}/20"
    print(f"  {k:35s} {where:>10s}  {sorted(types)}")
    if n == 20: universal.append(k)
    else: partial.append((k, n))

# ---- 2. Per-trial field union vs intersection ----
print()
print("=" * 78)
print("2. Per-trial entry fields")
print("=" * 78)
field_count = Counter()
field_types = defaultdict(set)
for tn, tid, e in all_entries:
    for k, v in e.items():
        field_count[k] += 1
        field_types[k].add(type(v).__name__)
total = len(all_entries)
print(f"{'field':35s} {'present_in':>12s}  type-set")
universal_trial = []
partial_trial = []
for k, n in sorted(field_count.items(), key=lambda x:-x[1]):
    pct = 100 * n / total
    where = 'ALL' if n == total else f"{pct:.1f}%"
    types = sorted(field_types[k])
    print(f"  {k:35s} {where:>12s}  {types}  (n={n})")
    if n == total: universal_trial.append(k)
    else: partial_trial.append((k, n, pct))

# ---- 3. Value-shape checks ----
print()
print("=" * 78)
print("3. Value-shape checks (key fields)")
print("=" * 78)

# 3a. agent: small enum
agents = Counter(e['agent'] for _,_,e in all_entries)
print(f"agents ({len(agents)} unique): {dict(agents)}")

# 3b. model: small set
models = Counter(e['model'] for _,_,e in all_entries)
print(f"\nmodels ({len(models)} unique):")
for m, n in models.most_common():
    print(f"  {n:>4d}  {m}")

# 3c. provider
provs = Counter(e.get('provider','MISSING') for _,_,e in all_entries)
print(f"\nproviders ({len(provs)} unique):")
for p, n in provs.most_common():
    print(f"  {n:>4d}  {p}")

# 3d. status casing
statuses = Counter(e.get('status','MISSING') for _,_,e in all_entries)
print(f"\nstatus values ({len(statuses)} unique): {dict(statuses)}")

# 3e. reward type/range
rewards = Counter()
for _,_,e in all_entries:
    r = e.get('reward')
    if r is None: rewards['None'] += 1
    elif isinstance(r, int): rewards[f"int:{r}"] += 1
    elif isinstance(r, float):
        if r == 0.0: rewards['float:0.0'] += 1
        elif r == 1.0: rewards['float:1.0'] += 1
        else: rewards['float:other'] += 1
    else: rewards[f"{type(r).__name__}:{r!r}"] += 1
print(f"\nreward shape: {dict(rewards)}")

# 3f. timestamp format
import re
ts_shapes = Counter()
for _,_,e in all_entries:
    for f in ('started_at','finished_at'):
        v = e.get(f)
        if v is None: shape = 'None'
        elif isinstance(v, str):
            # ISO 8601 with TZ?
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:?\d{2}|Z)?', v):
                tz_match = re.search(r'[+-]\d{2}:?\d{2}$|Z$', v)
                tsep = 'T' if 'T' in v else 'space'
                shape = f"ISO-{tsep}-tz" if tz_match else f"ISO-{tsep}-no-tz"
            else: shape = f"other:{v[:25]}"
        else: shape = type(v).__name__
        ts_shapes[(f, shape)] += 1
print(f"\ntimestamp formats:")
for (f, shape), n in sorted(ts_shapes.items()):
    print(f"  {f:14s} {shape:30s}  n={n}")

# 3g. artifact_prefix format
ap = Counter()
for _,_,e in all_entries:
    v = e.get('artifact_prefix','MISSING')
    if v is None or v == 'MISSING': ap['MISSING'] += 1
    elif not isinstance(v, str): ap[type(v).__name__] += 1
    elif not v.startswith('s3://ralphbench-logs/'): ap[f"BAD-PREFIX:{v[:50]}"] += 1
    elif not v.endswith('/'): ap['no-trailing-slash'] += 1
    else: ap['s3://ralphbench-logs/.../  (good)'] += 1
print(f"\nartifact_prefix shape: {dict(ap)}")

# 3h. source_oddish_* presence (for trials I uploaded)
src_keys = ('source_oddish_experiment_id','source_oddish_task_id','source_oddish_trial_id','source_trial_name')
src_complete = 0; src_partial = 0; src_none = 0
for _,_,e in all_entries:
    present = sum(1 for k in src_keys if e.get(k))
    if present == len(src_keys): src_complete += 1
    elif present == 0: src_none += 1
    else: src_partial += 1
print(f"\nsource_oddish_* lineage: complete={src_complete}, partial={src_partial}, none={src_none}")

# 3i. canonical_attempt_dir
canon = Counter()
for _,_,e in all_entries:
    v = e.get('canonical_attempt_dir')
    if v is None: canon['None/missing'] += 1
    elif isinstance(v, str) and v.startswith('task-'): canon['task-* (good)'] += 1
    else: canon[f"other:{v!r}"[:40]] += 1
print(f"canonical_attempt_dir: {dict(canon)}")

# 3j. exception_classes
ec_shapes = Counter()
for _,_,e in all_entries:
    v = e.get('exception_classes', 'MISSING')
    if v == 'MISSING': ec_shapes['MISSING field'] += 1
    elif v is None: ec_shapes['None'] += 1
    elif isinstance(v, list):
        if not v: ec_shapes['[]'] += 1
        else: ec_shapes[f"list:{sorted(v)}"] += 1
    else: ec_shapes[type(v).__name__] += 1
print(f"\nexception_classes shape:")
for s, n in sorted(ec_shapes.items(), key=lambda x:-x[1]):
    print(f"  {n:>4d}  {s}")

# ---- 4. Trial dir structure (sample 1 trial per task) ----
print()
print("=" * 78)
print("4. Sample trial-dir structure (one trial per task)")
print("=" * 78)
def head(key):
    try: dst.head_object(Bucket='ralphbench-logs', Key=key); return True
    except: return False
def check(tn, tid, canon):
    base = f"{tn}/{tid}/"
    return {
        'top_config': head(base+'config.json'),
        'top_result': head(base+'result.json'),
        'canon_config': head(f"{base}{canon}/config.json") if canon else None,
        'canon_result': head(f"{base}{canon}/result.json") if canon else None,
    }
print(f"{'task':24s} {'sample trial':35s}  tc tr cc cr")
for tn in TASKS:
    m = manifests[tn]
    tid = next(iter(m['trials']))
    canon = m['trials'][tid].get('canonical_attempt_dir')
    r = check(tn, tid, canon)
    flags = ''.join('✓' if r.get(k) else ('-' if r.get(k) is None else '✗')
                    for k in ('top_config','top_result','canon_config','canon_result'))
    print(f"  {tn:24s} {tid:35s}  {flags}")

# ---- Summary ----
print()
print("=" * 78)
print("Summary")
print("=" * 78)
print(f"  Universal manifest top-level fields ({len(universal)}): {universal}")
if partial:
    print(f"  Partial top-level fields:")
    for k, n in partial:
        print(f"    {k}: present in {n}/20 tasks")
print(f"\n  Universal trial entry fields ({len(universal_trial)}):")
for k in universal_trial: print(f"    - {k}")
if partial_trial:
    print(f"\n  Partial trial entry fields:")
    for k, n, pct in partial_trial:
        print(f"    {k}: present in {pct:.1f}% ({n}/{total}) of trials")
