"""Select candidate trials for s3-clone v13 (latest), one task only.

Filters by task_version_id so older-version trials aren't considered.
"""
import os, json, psycopg2, boto3
from collections import defaultdict

DB_URL = os.environ['ODDISH_DATABASE_URL'].replace('postgresql+asyncpg://', 'postgresql://')
EXPERIMENT_ID = 'cd8c33d8'

TASK_ID = 's3-clone-23996371'
TASK_NAME = 's3-clone'
TASK_HASH = '23996371'

EXPECTED_PAIRS = [
    ('claude-code', 'anthropic/claude-opus-4-7'),
    ('codex',       'openai/gpt-5.5'),
    ('gemini-cli',  'gemini/gemini-3.1-pro-preview'),
    ('kimi-cli',    'openrouter/moonshotai/kimi-k2.6'),
    ('terminus-2',  'anthropic/claude-opus-4-7'),
    ('terminus-2',  'openai/gpt-5.5'),
    ('terminus-2',  'gemini/gemini-3.1-pro-preview'),
    ('terminus-2',  'openrouter/moonshotai/kimi-k2.6'),
    ('terminus-2',  'openrouter/deepseek/deepseek-v4-pro'),
    ('terminus-2',  'openrouter/z-ai/glm-5.1'),
    ('terminus-2',  'openrouter/minimax/minimax-m2.7'),
    ('nop',         'default'),
    ('oracle',      'default'),
]

def norm_model(m):
    if m is None: return None
    if m in ('global.anthropic.claude-opus-4-7', 'claude-opus-4-7'):
        return 'anthropic/claude-opus-4-7'
    if m == 'gpt-5.5':
        return 'openai/gpt-5.5'
    if m == 'google/gemini-3.1-pro-preview':
        return 'gemini/gemini-3.1-pro-preview'
    return m

def main():
    s3 = boto3.client('s3',
        endpoint_url=os.environ['ODDISH_S3_ENDPOINT_URL'],
        aws_access_key_id=os.environ['ODDISH_S3_ACCESS_KEY'],
        aws_secret_access_key=os.environ['ODDISH_S3_SECRET_KEY'],
        region_name='us-east-1')
    bucket = os.environ['ODDISH_S3_BUCKET']

    conn = psycopg2.connect(DB_URL + '?sslmode=require')
    cur = conn.cursor()

    cur.execute("SELECT current_version_id FROM tasks WHERE id=%s", (TASK_ID,))
    task_version_id = cur.fetchone()[0]
    print(f"task_version_id={task_version_id}")

    # SUCCESS + numeric reward on the latest task version only
    cur.execute("""
        SELECT id, agent, model, provider, reward, status,
               attempts, max_attempts, started_at, finished_at,
               input_tokens, cache_tokens, output_tokens, cost_usd,
               has_trajectory, name, trial_s3_key, error_message
        FROM trials
        WHERE task_id=%s AND experiment_id=%s
          AND task_version_id=%s
          AND deleted_at IS NULL AND superseded_by_trial_id IS NULL
          AND status='SUCCESS' AND reward IS NOT NULL
    """, (TASK_ID, EXPERIMENT_ID, task_version_id))
    rows = cur.fetchall()

    by_pair = defaultdict(list)
    for r in rows:
        (tid, agent, model, provider, reward, status,
         attempts, max_attempts, started_at, finished_at,
         input_tokens, cache_tokens, output_tokens, cost_usd,
         has_trajectory, name, trial_s3_key, error_message) = r
        nm = norm_model(model)
        by_pair[(agent, nm)].append({
            'id': tid, 'agent': agent, 'model': nm, 'raw_model': model,
            'provider': provider, 'reward': reward,
            'status': str(status).lower() if status else None,
            'attempts': attempts, 'max_attempts': max_attempts,
            'started_at': started_at.isoformat() if started_at else None,
            'finished_at': finished_at.isoformat() if finished_at else None,
            'input_tokens': input_tokens, 'cache_tokens': cache_tokens,
            'output_tokens': output_tokens, 'cost_usd': cost_usd,
            'has_trajectory': has_trajectory, 'source_trial_name': name,
            'trial_s3_key': trial_s3_key,
            'error_message': error_message,
        })

    # For each candidate trial: list S3 to compute total_bytes & trajectory_bytes
    for pair, items in by_pair.items():
        for it in items:
            tid = it['id']
            prefix = it['trial_s3_key'] or f"tasks/{TASK_ID}/trials/{tid}/"
            if not prefix.endswith('/'):
                prefix += '/'
            traj_bytes = defaultdict(int)
            total_bytes = 0
            n_files = 0
            subdirs_seen = set()
            top_files = []
            for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get('Contents', []):
                    rel = obj['Key'][len(prefix):]
                    sz = obj['Size']
                    total_bytes += sz
                    n_files += 1
                    parts = rel.split('/', 1)
                    if len(parts) == 1:
                        top_files.append(rel)
                    else:
                        sub = parts[0]
                        subdirs_seen.add(sub)
                        if rel.endswith('agent/trajectory.json'):
                            traj_bytes[sub] = sz
            it['total_bytes'] = total_bytes
            it['n_files'] = n_files
            it['trajectory_bytes_max'] = max(traj_bytes.values()) if traj_bytes else 0
            it['top_files'] = top_files
            it['subdirs'] = sorted(subdirs_seen)
            it['pullable'] = n_files > 0 and 'result.json' in top_files

    # Apply selection rules per pair
    import datetime
    def _ep(c):
        s = c['started_at']
        if not s: return 0
        return datetime.datetime.fromisoformat(s).timestamp()

    selection = {'task_version_id': task_version_id, 'pairs': {}}
    for pair in EXPECTED_PAIRS:
        candidates = [c for c in by_pair.get(pair, []) if c['pullable']]
        candidates.sort(key=lambda c: (
            0 if c['reward'] == 1.0 else 1,
            -float(c['trajectory_bytes_max'] or 0),
            -float(c['total_bytes'] or 0),
            -_ep(c),
        ))
        picks = candidates[:5]
        for p in picks:
            p['selection_note'] = (
                'Selected as one of up to 5 valid trials for this task/agent/model, '
                'ranked by reward then trajectory_bytes then total artifact bytes then recency.'
            )
            p['trajectory_bytes'] = p['trajectory_bytes_max']
        selection['pairs'][f"{pair[0]}|{pair[1]}"] = {
            'pair': pair,
            'n_valid': len(candidates),
            'picks': [p['id'] for p in picks],
            'notes': ('SHORTFALL' if len(picks) < 5 else 'OK'),
        }

    print(f"\n==> {TASK_ID}  version={task_version_id}")
    npicks_total = 0
    for k, v in selection['pairs'].items():
        n = len(v['picks'])
        npicks_total += n
        print(f"  {k:60} valid={v['n_valid']:3d}  picks={n}  {'OK' if n==5 else 'SHORTFALL'}")
        for pid in v['picks']:
            cand = next(c for c in by_pair[tuple(k.split('|'))] if c['id']==pid)
            print(f"    - {pid:32} reward={cand['reward']} traj={cand['trajectory_bytes_max']:>9d}  total={cand['total_bytes']:>11d}  attempts={cand['attempts']}")
    print(f"  total picks: {npicks_total}/65")

    out = {
        TASK_ID: {
            'task_id': TASK_ID,
            'task_name': TASK_NAME,
            'task_hash': TASK_HASH,
            'task_version_id': task_version_id,
            'by_pair': {f"{a}|{m}": [c for c in by_pair.get((a,m), []) if c['pullable']] for a,m in EXPECTED_PAIRS},
            'selection': selection,
        }
    }
    with open('/tmp/oddish-import/selection_s3clone.json','w') as f:
        json.dump(out, f, indent=2, default=str)
    print('\nSaved /tmp/oddish-import/selection_s3clone.json')

if __name__ == '__main__':
    main()
