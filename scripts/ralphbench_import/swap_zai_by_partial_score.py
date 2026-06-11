"""Re-rank cells with >5 strict-valid candidates using the user's intended
secondary key: partial_score (desc). When partial_score isn't available
for a trial (verifier doesn't emit metrics.json), fall back to
trajectory_bytes (desc), then recency.

Swaps:
  kubernetes-rust-rewrite:  drop -257 (partial=0.0)   → add -248 (partial=0.776)
  parameter-golf:           drop -468 (partial=0.0)   → add -463 (partial=1.0)
  rust-java-lsp:            drop -277 (partial=0.0)   → add -287 (partial=0.962)
  trimul-cuda:              drop -595 (partial=0.425) → add -602 (partial=0.911)
"""
import os, sys, json, boto3, psycopg2
from datetime import datetime, timezone
from collections import Counter

sys.path.insert(0, '/workspace/scripts/ralphbench_import')
from import_zai_marathon import (
    make_zai_dst, upload_one_trial, build_entry,
    classify_network_policy, fetch_partial_score,
)
from augment_cd4dd88f import make_src, list_keys, pick_canonical

SRC_BUCKET = os.environ['ODDISH_S3_BUCKET']
DST_BUCKET = 'zai-swe-marathon'

SWAPS = [
    ('kubernetes-rust-rewrite', 'kubernetes-rust-rewrite-bae0f616',
     'kubernetes-rust-rewrite-bae0f616-257',
     'kubernetes-rust-rewrite-bae0f616-248'),
    ('parameter-golf', 'parameter-golf-61e11605',
     'parameter-golf-61e11605-468',
     'parameter-golf-61e11605-463'),
    ('rust-java-lsp', 'rust-java-lsp-0a9c67d6',
     'rust-java-lsp-0a9c67d6-277',
     'rust-java-lsp-0a9c67d6-287'),
    ('trimul-cuda', 'trimul-cuda-fc34aaba',
     'trimul-cuda-fc34aaba-595',
     'trimul-cuda-fc34aaba-602'),
]

def delete_trial_dir(dst, task_name, tid):
    """Delete every key under <task_name>/<tid>/ in the zai bucket."""
    keys = []
    for page in dst.get_paginator('list_objects_v2').paginate(
        Bucket=DST_BUCKET, Prefix=f"{task_name}/{tid}/"):
        for o in page.get('Contents',[]):
            keys.append({'Key': o['Key']})
    for i in range(0, len(keys), 1000):
        dst.delete_objects(Bucket=DST_BUCKET,
            Delete={'Objects': keys[i:i+1000], 'Quiet': True})
    return len(keys)

def fetch_trial_row(cur, rid):
    cur.execute("""SELECT id, task_id, task_version_id, agent, model, provider,
                          reward, status, attempts, max_attempts, started_at, finished_at,
                          input_tokens, cache_tokens, output_tokens, cost_usd,
                          has_trajectory, name, trial_s3_key
                   FROM trials WHERE id=%s""", (rid,))
    return cur.fetchone()

def build_trial_dict(src, row, task_name):
    (rid, tid, vid, agent, model, prov, reward, status, att, mx, sa, fa,
     it, ct, ot, cu, ht, name, s3key) = row
    prefix = (s3key or f"tasks/{tid}/trials/{rid}/")
    if not prefix.endswith('/'): prefix += '/'
    # exception classes (from result.json)
    try:
        res = json.loads(src.get_object(Bucket=SRC_BUCKET, Key=prefix+'result.json')['Body'].read())
        excs = set()
        for ev in (res.get('stats',{}).get('evals',{}) or {}).values():
            excs.update((ev.get('exception_stats') or {}).keys())
    except Exception:
        excs = set()
    # trajectory bytes
    traj = 0
    for k, sz in list_keys(src, prefix):
        if k.endswith('agent/trajectory.json'):
            traj = max(traj, sz)
    return {
        'id': rid, 'agent': agent, 'model': model, 'raw_model': model,
        'provider': prov, 'reward': reward, 'status': str(status).lower(),
        'attempts': att, 'max_attempts': mx,
        'started_at': sa.isoformat() if sa else None,
        'finished_at': fa.isoformat() if fa else None,
        'input_tokens': it, 'cache_tokens': ct, 'output_tokens': ot,
        'cost_usd': cu, 'has_trajectory': ht, 'source_trial_name': name,
        'trial_s3_key': prefix,
        'trajectory_bytes': traj,
        'exception_classes': sorted(excs),
        'selection_note': (
            f"Re-ranked by partial_score (user-intended secondary key) on "
            f"{datetime.now(timezone.utc).isoformat()}. Original closed-task "
            f"import used trajectory_bytes as the tiebreaker; this trial was "
            f"swapped in because it has a higher verifier partial_score than "
            f"the previously-included trial."
        ),
    }, prefix, vid

def main():
    src = make_src()
    dst = make_zai_dst()
    url = os.environ['ODDISH_DATABASE_URL'].replace('postgresql+asyncpg://','postgresql://')
    cur = psycopg2.connect(url + '?sslmode=require').cursor()

    for task_name, task_id, drop_rid, add_rid in SWAPS:
        print(f"\n=== {task_name}: drop {drop_rid} → add {add_rid} ===")

        # 1. Read current manifest
        m = json.loads(dst.get_object(Bucket=DST_BUCKET,
            Key=f"{task_name}/_manifest.json")['Body'].read())
        old_drop_entry = m['trials'].get(drop_rid)
        if not old_drop_entry:
            print(f"  drop trial {drop_rid} not in manifest, skipping")
            continue
        old_drop_partial = old_drop_entry.get('verifier_partial_score')
        old_drop_traj = old_drop_entry.get('trajectory_bytes')

        # 2. Delete the dropped trial dir
        n_keys = delete_trial_dir(dst, task_name, drop_rid)
        print(f"  deleted {n_keys} keys for {drop_rid}")

        # 3. Upload the new trial
        row = fetch_trial_row(cur, add_rid)
        trial, prefix, vid = build_trial_dict(src, row, task_name)
        task_hash = task_id.rsplit('-',1)[-1]
        sel_meta = {'task_id': task_id, 'task_name': task_name,
                    'task_hash': task_hash, 'task_version_id': vid}
        src_keys = list_keys(src, prefix)
        canon_src = pick_canonical(src, prefix, src_keys)
        policy, allow_n, _ = classify_network_policy(src, prefix, canon_src)
        canonical, _ = upload_one_trial(src, dst, sel_meta, trial, task_name)
        score = fetch_partial_score(src, prefix, canonical)
        entry = build_entry(trial, sel_meta, canonical, task_name,
                            partial_score=score, network_policy=policy,
                            network_allowlist_size=allow_n)
        print(f"  uploaded {add_rid} (canonical={canonical}, partial={entry.get('verifier_partial_score')})")

        # 4. Update manifest: drop old, add new
        del m['trials'][drop_rid]
        m['trials'][add_rid] = entry
        def _idx(t):
            tail = t.rsplit('-',1)[-1]
            return (int(tail) if tail.isdigit() else 1<<30, t)
        m['trials'] = dict(sorted(m['trials'].items(), key=lambda kv: _idx(kv[0])))
        m['n_trials'] = len(m['trials'])
        pc = Counter((e['agent'], e['model']) for e in m['trials'].values())
        m['pair_counts'] = {f"{a}|{md}": n for (a,md), n in pc.items()}
        m['n_pass'] = sum(1 for e in m['trials'].values() if e.get('reward') == 1.0)
        if m.get('is_closed_internet_task'):
            m['n_timeout_failure'] = sum(1 for e in m['trials'].values()
                if e.get('reward') == 0.0 and
                set(e.get('exception_classes',[])) == {'AgentTimeoutError'})
            m['n_clean_fail'] = sum(1 for e in m['trials'].values()
                if e.get('reward') == 0.0 and
                not set(e.get('exception_classes',[])))
        m.setdefault('additional_imports',[]).append({
            'imported_at': datetime.now(timezone.utc).isoformat(),
            'source_oddish_experiment_id': 'e0ef4703',
            'task_version_id_at_snapshot': vid,
            'n_trials_added': 0,
            'n_trials_swapped': 1,
            'pairs_added': [],
            'note': (f"Partial-score-aware re-rank swap: dropped {drop_rid} "
                     f"(partial={old_drop_partial}, traj={old_drop_traj}) and "
                     f"added {add_rid} (partial={entry.get('verifier_partial_score')}, "
                     f"traj={entry.get('trajectory_bytes')}). Cell still 5/5."),
        })
        # Tombstone log so external reconcilers don't restore the dropped trial
        m.setdefault('intentionally_tombstoned_trials', []).append({
            'trial_id': drop_rid,
            'tombstoned_at': datetime.now(timezone.utc).isoformat(),
            'reason': (f'Replaced by {add_rid} during partial-score-aware '
                        f're-rank. The dropped trial is valid by the cell-filter '
                        f'rule but ranks below {add_rid} on partial_score.'),
        })

        body = json.dumps(m, indent=2, default=str).encode('utf-8')
        dst.put_object(Bucket=DST_BUCKET, Key=f"{task_name}/_manifest.json",
                       Body=body, ContentType='application/json')
        print(f"  manifest updated (n_trials={m['n_trials']} n_pass={m['n_pass']})")

if __name__ == '__main__':
    main()
