"""Select candidate trials per pair and compute sizes."""
import os, json, psycopg2, boto3
from collections import defaultdict

DB_URL = os.environ['ODDISH_DATABASE_URL'].replace('postgresql+asyncpg://', 'postgresql://')
EXPERIMENT_ID = 'cd8c33d8'
TASKS = {
    'slack-clone-b0a98beb':   {'task_name':'slack-clone',   'task_hash':'b0a98beb'},
    'mastodon-clone-e9964b7a':{'task_name':'mastodon-clone','task_hash':'e9964b7a'},
}

# 13 expected pairs (agent, model)
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

# Normalize raw model names to bucket convention.
def norm_model(m):
    if m is None:
        return None
    raw = m
    if m == 'global.anthropic.claude-opus-4-7' or m == 'claude-opus-4-7':
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

    out_selection = {}
    for task_id, meta in TASKS.items():
        task_name = meta['task_name']
        task_hash = meta['task_hash']
        # Get current_version_id
        cur.execute("SELECT current_version_id FROM tasks WHERE id=%s", (task_id,))
        task_version_id = cur.fetchone()[0]
        print(f"==> {task_id}  version={task_version_id}")

        # Pull all candidate trials (SUCCESS + numeric reward) in cd8c33d8
        cur.execute("""
            SELECT id, agent, model, provider, reward, status,
                   attempts, max_attempts, started_at, finished_at,
                   input_tokens, cache_tokens, output_tokens, cost_usd,
                   has_trajectory, name, trial_s3_key
            FROM trials
            WHERE task_id=%s AND experiment_id=%s
              AND deleted_at IS NULL AND superseded_by_trial_id IS NULL
              AND status='SUCCESS' AND reward IS NOT NULL
        """, (task_id, EXPERIMENT_ID))
        rows = cur.fetchall()
        # Bucket by (agent, normalized_model)
        by_pair = defaultdict(list)
        for r in rows:
            (tid, agent, model, provider, reward, status,
             attempts, max_attempts, started_at, finished_at,
             input_tokens, cache_tokens, output_tokens, cost_usd,
             has_trajectory, name, trial_s3_key) = r
            nm = norm_model(model)
            by_pair[(agent, nm)].append({
                'id': tid, 'agent': agent, 'model': nm, 'raw_model': model,
                'provider': provider, 'reward': reward, 'status': status.lower() if hasattr(status,'lower') else str(status).lower(),
                'attempts': attempts, 'max_attempts': max_attempts,
                'started_at': started_at.isoformat() if started_at else None,
                'finished_at': finished_at.isoformat() if finished_at else None,
                'input_tokens': input_tokens, 'cache_tokens': cache_tokens,
                'output_tokens': output_tokens, 'cost_usd': cost_usd,
                'has_trajectory': has_trajectory, 'source_trial_name': name,
                'trial_s3_key': trial_s3_key,
            })

        # For each candidate, list S3 to compute trajectory_bytes and total_bytes
        # And to verify pullable (>=1 file).
        for pair, items in by_pair.items():
            for it in items:
                tid = it['id']
                prefix = it['trial_s3_key'] or f"tasks/{task_id}/trials/{tid}/"
                if not prefix.endswith('/'):
                    prefix += '/'
                paginator = s3.get_paginator('list_objects_v2')
                total_bytes = 0
                traj_bytes_per_sub = defaultdict(int)
                subdir_keys = defaultdict(list)
                subdirs_seen = set()
                top_files = []
                n_files = 0
                for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                    for obj in page.get('Contents', []):
                        rel = obj['Key'][len(prefix):]
                        sz = obj['Size']
                        total_bytes += sz
                        n_files += 1
                        parts = rel.split('/', 1)
                        if len(parts) == 1:
                            top_files.append((rel, sz))
                        else:
                            sub = parts[0]
                            subdirs_seen.add(sub)
                            if rel.endswith('agent/trajectory.json'):
                                traj_bytes_per_sub[sub] = sz
                it['total_bytes'] = total_bytes
                it['n_files'] = n_files
                it['top_files'] = [t[0] for t in top_files]
                it['subdirs'] = sorted(subdirs_seen)
                it['traj_bytes_per_sub'] = dict(traj_bytes_per_sub)
                # Trajectory bytes: max across attempts (canonical likely largest)
                it['trajectory_bytes_max'] = max(traj_bytes_per_sub.values()) if traj_bytes_per_sub else 0
                it['pullable'] = n_files > 0 and any(f == 'result.json' for f in [t for t in it['top_files']])

        # Now select per expected pair
        task_selection = {'task_version_id': task_version_id, 'pairs': {}}
        for pair in EXPECTED_PAIRS:
            candidates = [c for c in by_pair.get(pair, []) if c['pullable']]
            # Tie-break:
            # 1. reward=1.0 over others
            # 2. trajectory_bytes_max DESC
            # 3. total_bytes DESC
            # 4. started_at DESC (recent)
            candidates.sort(key=lambda c: (
                -(1.0 if c['reward'] == 1.0 else 0.0),
                -float(c['trajectory_bytes_max'] or 0),
                -float(c['total_bytes'] or 0),
                # stable deterministic: most recent first
                c['started_at'] or '',
            ), reverse=False)
            # The reverse on started_at -- want DESC. Easier: reverse twice.
            # Actually our key has minuses for the desc ones; for started_at we want DESC
            # so negate by reversing or by sorting again. Let's resort properly:
            candidates.sort(key=lambda c: (
                0 if c['reward'] == 1.0 else 1,                  # 1.0 first
                -float(c['trajectory_bytes_max'] or 0),          # larger first
                -float(c['total_bytes'] or 0),                   # larger first
                # for recency DESC, sort by negative timestamp ordinal
                # use started_at string negated by inverting the string isn't possible; pad to 30 then we sort ascending by inverse — simpler: store negative epoch
            ))
            # Re-sort with negative epoch for recency
            import datetime
            def _ep(c):
                s = c['started_at']
                if not s: return 0
                return datetime.datetime.fromisoformat(s).timestamp()
            candidates.sort(key=lambda c: (
                0 if c['reward'] == 1.0 else 1,
                -float(c['trajectory_bytes_max'] or 0),
                -float(c['total_bytes'] or 0),
                -_ep(c),
            ))
            picks = candidates[:5]
            task_selection['pairs'][f"{pair[0]}|{pair[1]}"] = {
                'pair': pair,
                'n_valid': len(candidates),
                'picks': [p['id'] for p in picks],
                'notes': ('SHORTFALL' if len(picks) < 5 else 'OK'),
            }
            # Inject selection_note into each picked candidate dict
            for p in picks:
                p['selection_note'] = (
                    'Selected as one of up to 5 valid trials for this task/agent/model, '
                    'ranked by reward then trajectory_bytes then total artifact bytes then recency.'
                )
                p['trajectory_bytes'] = p['trajectory_bytes_max']

        # Save selection
        out_selection[task_id] = {
            'task_id': task_id,
            'task_name': task_name,
            'task_hash': task_hash,
            'task_version_id': task_version_id,
            'by_pair': {f"{a}|{m}": [c for c in by_pair.get((a,m), []) if c['pullable']] for a,m in EXPECTED_PAIRS},
            'selection': task_selection,
        }
        # Print summary
        print(f"  task_version_id={task_version_id}")
        for k, v in task_selection['pairs'].items():
            n = len(v['picks'])
            print(f"    {k:60} valid={v['n_valid']:3d}  picks={n}  {'OK' if n==5 else 'SHORTFALL'}")
            for pid in v['picks']:
                # find the candidate
                cand = next(c for c in by_pair[tuple(k.split('|'))] if c['id']==pid)
                print(f"      - {pid:32} reward={cand['reward']} traj={cand['trajectory_bytes_max']:>9d}  total={cand['total_bytes']:>11d}  attempts={cand['attempts']}")

    with open('/tmp/oddish-import/selection.json','w') as f:
        json.dump(out_selection, f, indent=2, default=str)
    print('\nSaved /tmp/oddish-import/selection.json')

if __name__ == '__main__':
    main()
