"""Validate the cd4dd88f augmentation in ralphbench-logs."""
import os, json, boto3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
dst = boto3.client('s3', aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
    aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'], region_name='us-west-2')
TARGETS = ['biofabric-rust-rewrite','embedding-eval','find-network-alignments',
           'jax-pytorch-rewrite','kubernetes-rust-rewrite','nextjs-vite-rewrite',
           'parameter-golf','ruby-rust-port','rust-c-compiler','rust-java-lsp',
           'stripe-clone','trimul-cuda','vliw-kernel-optimization','wasm-simd','zstd-decoder']
VALID_EXC = {'AgentTimeoutError','VerifierTimeoutError'}
NEW_PAIRS = {('claude-code','anthropic/claude-opus-4-8'),
             ('gemini-cli','gemini/gemini-3.5-flash')}

def head(key):
    try:
        dst.head_object(Bucket='ralphbench-logs', Key=key)
        return True
    except Exception:
        return False

def check_exc(task_name, tid):
    try:
        res = json.loads(dst.get_object(Bucket='ralphbench-logs', Key=f"{task_name}/{tid}/result.json")['Body'].read())
        excs = set()
        for ev in (res.get('stats',{}).get('evals',{}) or {}).values():
            excs.update((ev.get('exception_stats') or {}).keys())
        return tid, sorted(excs)
    except Exception as e:
        return tid, ['__ERROR__:'+str(e)[:50]]

overall_ok = True
new_total = 0
print(f"{'task':30s} {'n_trial':>7s} {'new':>4s} {'multi':>5s}  pairs (n)")
for tname in TARGETS:
    m = json.loads(dst.get_object(Bucket='ralphbench-logs', Key=f"{tname}/_manifest.json")['Body'].read())
    new_entries = {tid: e for tid, e in m['trials'].items()
                   if e.get('source_oddish_experiment_id') == 'cd4dd88f'}
    new_total += len(new_entries)
    pair_counts = Counter((e['agent'], e['model']) for e in new_entries.values())
    pair_str = ', '.join(f"{a.split('/')[-1]}/{md.split('/')[-1]}={n}" for (a,md),n in sorted(pair_counts.items()))
    print(f"  {tname:30s} {m['n_trials']:>7d} {len(new_entries):>4d} {str(m.get('is_multi_version','')):>5s}  {pair_str}")

    # Check artifacts for each new entry
    missing_cfg = missing_res = missing_canon = 0
    bad_excs = []
    for tid, e in new_entries.items():
        base = f"{tname}/{tid}/"
        if not head(base+'config.json'): missing_cfg += 1
        if not head(base+'result.json'): missing_res += 1
        canon = e.get('canonical_attempt_dir')
        if canon and not head(base+canon+'/result.json'): missing_canon += 1
    if missing_cfg or missing_res or missing_canon:
        print(f"    !! missing files: cfg={missing_cfg} res={missing_res} canon={missing_canon}")
        overall_ok = False
    # Sample-check exception_stats on a few of the new entries
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(check_exc, tname, tid) for tid in new_entries]
        for fut in as_completed(futs):
            tid, excs = fut.result()
            bad = set(excs) - VALID_EXC
            if bad:
                bad_excs.append((tid, sorted(bad)))
    if bad_excs:
        print(f"    !! forbidden exception classes: {bad_excs[:3]}")
        overall_ok = False
    # Verify pairs are only the two new ones
    bad_pairs = [p for p in pair_counts if p not in NEW_PAIRS]
    if bad_pairs:
        print(f"    !! unexpected pairs in new entries: {bad_pairs}")
        overall_ok = False

print()
print(f"Total new cd4dd88f entries across all targets: {new_total}")
print('OVERALL:', 'OK' if overall_ok else 'ERRORS')
