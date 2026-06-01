"""Strict selection for the cd4dd88f import.

Differences vs select_strict.py:
- Hard-coded list of 15 tasks (excludes 4 CUA clones + post-train-ifeval).
- Only two agent/model pairs are imported: claude-code/anthropic/claude-opus-4-8
  and gemini-cli/gemini/gemini-3.5-flash. nop/oracle are skipped.
- nextjs-vite-rewrite: also skip the claude-code pair.
- Uses the experiment-snapshot task_version_id per task (the version the
  trials actually ran against in cd4dd88f), not the task's
  current_version_id. They mostly coincide; trimul-cuda is the lone
  exception (snapshot v86, current v87).
"""
from __future__ import annotations
import os, json, psycopg2, boto3
from collections import defaultdict
from datetime import datetime

EXPERIMENT_ID = 'cd4dd88f'
VALID_EXC = {'AgentTimeoutError'}

# 15 tasks in scope (task_id, task_name, task_hash)
TASKS = [
    ('biofabric-rust-rewrite-897c0fc8',   'biofabric-rust-rewrite',   '897c0fc8'),
    ('embedding-eval-be1dc228',           'embedding-eval',           'be1dc228'),
    ('find-network-alignments-b2ecebb7',  'find-network-alignments',  'b2ecebb7'),
    ('jax-pytorch-rewrite-93c38683',      'jax-pytorch-rewrite',      '93c38683'),
    ('kubernetes-rust-rewrite-bae0f616',  'kubernetes-rust-rewrite',  'bae0f616'),
    ('nextjs-vite-rewrite-b0d028b0',      'nextjs-vite-rewrite',      'b0d028b0'),
    ('parameter-golf-61e11605',           'parameter-golf',           '61e11605'),
    ('ruby-rust-port-383776ff',           'ruby-rust-port',           '383776ff'),
    ('rust-c-compiler-57913cbe',          'rust-c-compiler',          '57913cbe'),
    ('rust-java-lsp-0a9c67d6',            'rust-java-lsp',            '0a9c67d6'),
    ('stripe-clone-66061f4d',             'stripe-clone',             '66061f4d'),
    ('trimul-cuda-fc34aaba',              'trimul-cuda',              'fc34aaba'),
    ('vliw-kernel-optimization-e4d25121', 'vliw-kernel-optimization', 'e4d25121'),
    ('wasm-simd-40e18127',                'wasm-simd',                '40e18127'),
    ('zstd-decoder-72bbfded',             'zstd-decoder',             '72bbfded'),
]

PAIRS_INCLUDE = [
    ('claude-code', 'anthropic/claude-opus-4-8'),
    ('gemini-cli',  'gemini/gemini-3.5-flash'),
]

PAIRS_SKIP_PER_TASK = {
    'nextjs-vite-rewrite': {('claude-code', 'anthropic/claude-opus-4-8')},
}

def norm_model(m):
    if m == 'global.anthropic.claude-opus-4-8':
        return 'anthropic/claude-opus-4-8'
    if m == 'google/gemini-3.5-flash':
        return 'gemini/gemini-3.5-flash'
    return m

def main():
    s3 = boto3.client('s3',
        endpoint_url=os.environ['ODDISH_S3_ENDPOINT_URL'],
        aws_access_key_id=os.environ['ODDISH_S3_ACCESS_KEY'],
        aws_secret_access_key=os.environ['ODDISH_S3_SECRET_KEY'],
        region_name='us-east-1')
    bucket = os.environ['ODDISH_S3_BUCKET']
    conn = psycopg2.connect(os.environ['ODDISH_DATABASE_URL'].replace('postgresql+asyncpg://','postgresql://')+'?sslmode=require')
    cur = conn.cursor()
    out = {}
    grand_total = 0
    for task_id, task_name, task_hash in TASKS:
        # Determine the experiment-snapshot version (mode of task_version_id
        # across trials in cd4dd88f for this task; should be a single value).
        cur.execute("""
            SELECT task_version_id, COUNT(*) FROM trials
            WHERE task_id=%s AND experiment_id=%s AND deleted_at IS NULL
              AND superseded_by_trial_id IS NULL
            GROUP BY task_version_id ORDER BY COUNT(*) DESC LIMIT 1
        """, (task_id, EXPERIMENT_ID))
        snap_v = cur.fetchone()[0]

        cur.execute("""
            SELECT id, agent, model, provider, reward, status,
                   attempts, max_attempts, started_at, finished_at,
                   input_tokens, cache_tokens, output_tokens, cost_usd,
                   has_trajectory, name, trial_s3_key, error_message
            FROM trials
            WHERE task_id=%s AND experiment_id=%s AND task_version_id=%s
              AND deleted_at IS NULL AND superseded_by_trial_id IS NULL
              AND status='SUCCESS' AND reward IS NOT NULL
        """, (task_id, EXPERIMENT_ID, snap_v))
        rows = cur.fetchall()

        skip_pairs = PAIRS_SKIP_PER_TASK.get(task_name, set())
        by_pair = defaultdict(list)
        for r in rows:
            (tid, agent, model, provider, reward, status,
             attempts, max_attempts, started_at, finished_at,
             input_tokens, cache_tokens, output_tokens, cost_usd,
             has_trajectory, name, trial_s3_key, error_message) = r
            nm = norm_model(model)
            if (agent, nm) not in PAIRS_INCLUDE:
                continue
            if (agent, nm) in skip_pairs:
                continue
            it = {
                'id': tid, 'agent': agent, 'model': nm, 'raw_model': model,
                'provider': provider, 'reward': reward,
                'status': str(status).lower(),
                'attempts': attempts, 'max_attempts': max_attempts,
                'started_at': started_at.isoformat() if started_at else None,
                'finished_at': finished_at.isoformat() if finished_at else None,
                'input_tokens': input_tokens, 'cache_tokens': cache_tokens,
                'output_tokens': output_tokens, 'cost_usd': cost_usd,
                'has_trajectory': has_trajectory, 'source_trial_name': name,
                'trial_s3_key': trial_s3_key,
            }
            # List Oddish-side artifacts + classify exception
            prefix = trial_s3_key or f"tasks/{task_id}/trials/{tid}/"
            if not prefix.endswith('/'): prefix += '/'
            total_bytes = 0
            n_files = 0
            top_files = set()
            traj_max = 0
            subs = set()
            traj_per_sub = defaultdict(int)
            for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get('Contents', []):
                    rel = obj['Key'][len(prefix):]
                    sz = obj['Size']
                    total_bytes += sz; n_files += 1
                    if '/' in rel:
                        sub = rel.split('/',1)[0]
                        subs.add(sub)
                        if rel.endswith('agent/trajectory.json'):
                            traj_per_sub[sub] = sz
                    else:
                        top_files.add(rel)
            excs = set()
            pullable = n_files > 0 and 'result.json' in top_files
            if pullable:
                try:
                    res = json.loads(s3.get_object(Bucket=bucket, Key=prefix+'result.json')['Body'].read())
                    for ev in (res.get('stats',{}).get('evals',{}) or {}).values():
                        for cls in (ev.get('exception_stats') or {}).keys():
                            excs.add(cls)
                except Exception:
                    excs.add('__MISSING_RESULT_JSON__')
            traj_max = max(traj_per_sub.values()) if traj_per_sub else 0
            bad = excs - VALID_EXC
            it.update({
                'total_bytes': total_bytes,
                'n_files': n_files,
                'trajectory_bytes_max': traj_max,
                'exception_classes': sorted(excs),
                'pullable': pullable,
                'valid_strict': pullable and not bad,
                'task_version_id_for_trial': snap_v,
            })
            by_pair[(agent, nm)].append(it)

        # Rank & pick within each pair
        selection_pairs = {}
        for pair in PAIRS_INCLUDE:
            if pair in skip_pairs:
                continue
            all_items = by_pair.get(pair, [])
            valid = [c for c in all_items if c['valid_strict']]
            def _ep(c):
                s = c['started_at']
                return datetime.fromisoformat(s).timestamp() if s else 0
            valid.sort(key=lambda c: (
                0 if c['reward'] == 1.0 else 1,
                -float(c.get('trajectory_bytes_max') or 0),
                -float(c.get('total_bytes') or 0),
                -_ep(c),
            ))
            picks = valid[:5]
            for p in picks:
                p['selection_note'] = (
                    'Selected from cd4dd88f (opus 4.8 / gemini 3.5 flash). Up to 5 per pair, '
                    'ranked by reward then trajectory_bytes then total bytes then recency. '
                    'Strict: empty or {AgentTimeoutError, VerifierTimeoutError} exception_stats only.'
                )
                p['trajectory_bytes'] = p['trajectory_bytes_max']
            selection_pairs[f"{pair[0]}|{pair[1]}"] = {
                'pair': pair,
                'n_candidates': len(all_items),
                'n_valid_strict': len(valid),
                'picks': [p['id'] for p in picks],
                'notes': ('SHORTFALL' if len(picks) < 5 else 'OK'),
            }
            grand_total += len(picks)

        out[task_id] = {
            'task_id': task_id, 'task_name': task_name, 'task_hash': task_hash,
            'task_version_id': snap_v,
            'by_pair': {f"{a}|{m}": by_pair.get((a,m), []) for (a,m) in PAIRS_INCLUDE},
            'selection': {'task_version_id': snap_v, 'pairs': selection_pairs},
        }
        print(f"\n== {task_name:30s} v={snap_v} ==")
        for k, v in selection_pairs.items():
            print(f"   {k:60} cand={v['n_candidates']:>2d}  valid={v['n_valid_strict']:>2d}  pick={len(v['picks'])}  {v['notes']}")

    with open('/tmp/oddish-import/selection_cd4dd88f.json','w') as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nGRAND TOTAL: {grand_total} trials to upload")
    print('Saved /tmp/oddish-import/selection_cd4dd88f.json')

if __name__ == '__main__':
    main()
