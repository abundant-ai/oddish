"""Bucket-wide exception-class audit.

For every trial uploaded by this PR (source_oddish_experiment_id in
{cd4dd88f, cd8c33d8}), re-read its top-level result.json from ralphbench-
logs and confirm the exception_stats are either empty or only
AgentTimeoutError.

Entries with override_validity == True are intentionally allowed to carry
other exception classes (a user-explicit acceptance recorded in the
manifest entry's override_reason). They're reported separately.
"""
import os, json, boto3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import sys

dst = boto3.client('s3', aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
    aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'], region_name='us-west-2')
VALID = {'AgentTimeoutError'}
ALL = ['biofabric-rust-rewrite','embedding-eval','find-network-alignments',
       'jax-pytorch-rewrite','kubernetes-rust-rewrite','nextjs-vite-rewrite',
       'parameter-golf','ruby-rust-port','rust-c-compiler','rust-java-lsp',
       'stripe-clone','trimul-cuda','vliw-kernel-optimization','wasm-simd','zstd-decoder',
       'slack-clone','mastodon-clone','s3-clone']

def check(tname, tid):
    try:
        res = json.loads(dst.get_object(Bucket='ralphbench-logs',
            Key=f"{tname}/{tid}/result.json")['Body'].read())
        excs = set()
        for ev in (res.get('stats',{}).get('evals',{}) or {}).values():
            excs.update((ev.get('exception_stats') or {}).keys())
        return tid, sorted(excs)
    except Exception:
        return tid, ['__ERROR__']

def main():
    bad_no_override = []
    overrides = []
    counts = Counter()
    for tname in ALL:
        m = json.loads(dst.get_object(Bucket='ralphbench-logs',
            Key=f"{tname}/_manifest.json")['Body'].read())
        cd_ids = [tid for tid,e in m['trials'].items()
                  if e.get('source_oddish_experiment_id') in ('cd4dd88f','cd8c33d8')]
        with ThreadPoolExecutor(max_workers=16) as pool:
            futs = {pool.submit(check, tname, tid): tid for tid in cd_ids}
            for fut in as_completed(futs):
                tid, excs = fut.result()
                entry = m['trials'][tid]
                counts[entry.get('source_oddish_experiment_id')] += 1
                bad = set(excs) - VALID
                if bad:
                    if entry.get('override_validity'):
                        overrides.append((tname, tid, excs, entry.get('override_reason')))
                    else:
                        bad_no_override.append((tname, tid, excs,
                                                entry.get('source_oddish_experiment_id')))

    print(f"Trials uploaded by this PR: {dict(counts)}")
    print(f"Forbidden-exception trials WITH override flag (intentional): {len(overrides)}")
    for t,tid,e,reason in overrides:
        print(f"  - {t}/{tid}  excs={e}")
        if reason:
            print(f"      reason: {reason[:160]}")
    print(f"Forbidden-exception trials WITHOUT override flag: {len(bad_no_override)}")
    for t,tid,e,exp in bad_no_override:
        print(f"  ! {t}/{tid}  excs={e}  source_exp={exp}")
    sys.exit(1 if bad_no_override else 0)

if __name__ == '__main__':
    main()
