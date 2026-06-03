"""Strict selection for the 4 CUA tasks (slack/mastodon/s3-clone/excel-clone)
from Oddish experiment cd4dd88f.

For the claude-code pair, the same logical cell (agent=claude-code,
normalized_model=anthropic/claude-opus-4-8) covers two raw routes:

  - global.anthropic.claude-opus-4-8     (provider=bedrock)
  - openrouter/anthropic/claude-opus-4.8 (provider=openrouter)

We rank them together and keep the route as ``provider`` on each entry.

Validity: SUCCESS + numeric reward + snapshot task_version_id +
exception_stats clean or {AgentTimeoutError} only (NonZero excluded).

Rank: reward DESC, trajectory_bytes DESC, total_bytes DESC, recency DESC.
Cap: up to 5 per (agent, normalized_model) pair per task. Tasks where the
quota isn't filled simply ship with whatever is currently available
(trials are still rolling in upstream).
"""
from __future__ import annotations
import os, json, psycopg2, boto3
from collections import defaultdict
from datetime import datetime

EXPERIMENT_ID = 'cd4dd88f'
VALID_EXC = {'AgentTimeoutError'}

TASKS = [
    ('slack-clone-b0a98beb',    'slack-clone',    'b0a98beb'),
    ('mastodon-clone-e9964b7a', 'mastodon-clone', 'e9964b7a'),
    ('s3-clone-23996371',       's3-clone',       '23996371'),
    ('excel-clone-685bff89',    'excel-clone',    '685bff89'),
]

# Raw model strings -> normalized (agent, normalized_model, recorded_provider_default)
# We keep the per-trial provider from the DB, but model is normalized.
RAW_TO_NORM = {
    ('claude-code', 'global.anthropic.claude-opus-4-8'):
        ('claude-code', 'anthropic/claude-opus-4-8'),
    ('claude-code', 'openrouter/anthropic/claude-opus-4.8'):
        ('claude-code', 'anthropic/claude-opus-4-8'),
    ('gemini-cli',  'gemini/gemini-3.5-flash'):
        ('gemini-cli',  'gemini/gemini-3.5-flash'),
}
PAIRS_INCLUDE = {
    ('claude-code', 'anthropic/claude-opus-4-8'),
    ('gemini-cli',  'gemini/gemini-3.5-flash'),
}

def list_keys_and_meta(s3, bucket, prefix):
    total = 0; n_files = 0; top_files = set()
    traj_per_sub = defaultdict(int)
    subs = set()
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []):
            rel = obj['Key'][len(prefix):]; sz = obj['Size']
            total += sz; n_files += 1
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
    return {
        'total_bytes': total, 'n_files': n_files,
        'top_files': sorted(top_files), 'subs': sorted(subs),
        'trajectory_bytes_max': max(traj_per_sub.values()) if traj_per_sub else 0,
        'exception_classes': sorted(excs), 'pullable': pullable,
    }

def main():
    s3 = boto3.client('s3', endpoint_url=os.environ['ODDISH_S3_ENDPOINT_URL'],
        aws_access_key_id=os.environ['ODDISH_S3_ACCESS_KEY'],
        aws_secret_access_key=os.environ['ODDISH_S3_SECRET_KEY'], region_name='us-east-1')
    bucket = os.environ['ODDISH_S3_BUCKET']
    conn = psycopg2.connect(os.environ['ODDISH_DATABASE_URL'].replace(
        'postgresql+asyncpg://','postgresql://') + '?sslmode=require')
    cur = conn.cursor()
    out = {}
    grand_total = 0
    for task_id, task_name, task_hash in TASKS:
        cur.execute("""SELECT task_version_id, COUNT(*) FROM trials
                       WHERE task_id=%s AND experiment_id=%s AND deleted_at IS NULL
                         AND superseded_by_trial_id IS NULL
                       GROUP BY task_version_id ORDER BY COUNT(*) DESC LIMIT 1""",
                    (task_id, EXPERIMENT_ID))
        snap_v = cur.fetchone()[0]

        cur.execute("""
            SELECT id, agent, model, provider, reward, status,
                   attempts, max_attempts, started_at, finished_at,
                   input_tokens, cache_tokens, output_tokens, cost_usd,
                   has_trajectory, name, trial_s3_key
            FROM trials WHERE task_id=%s AND experiment_id=%s AND task_version_id=%s
              AND deleted_at IS NULL AND superseded_by_trial_id IS NULL
              AND status='SUCCESS' AND reward IS NOT NULL
        """, (task_id, EXPERIMENT_ID, snap_v))
        rows = cur.fetchall()

        by_pair = defaultdict(list)
        for r in rows:
            (tid, agent, model, provider, reward, status, attempts, max_attempts,
             sa, fa, it, ct, ot, cu, ht, name, s3key) = r
            norm = RAW_TO_NORM.get((agent, model))
            if not norm or norm not in PAIRS_INCLUDE:
                continue
            it_dict = {
                'id': tid, 'agent': norm[0], 'model': norm[1], 'raw_model': model,
                'provider': provider, 'reward': reward,
                'status': str(status).lower(), 'attempts': attempts, 'max_attempts': max_attempts,
                'started_at': sa.isoformat() if sa else None,
                'finished_at': fa.isoformat() if fa else None,
                'input_tokens': it, 'cache_tokens': ct, 'output_tokens': ot,
                'cost_usd': cu, 'has_trajectory': ht, 'source_trial_name': name,
                'trial_s3_key': s3key, 'task_version_id_for_trial': snap_v,
            }
            prefix = s3key or f"tasks/{task_id}/trials/{tid}/"
            if not prefix.endswith('/'): prefix += '/'
            meta = list_keys_and_meta(s3, bucket, prefix)
            it_dict.update(meta)
            bad = set(meta['exception_classes']) - VALID_EXC
            it_dict['valid_strict'] = meta['pullable'] and not bad
            it_dict['rejected_reason'] = (
                None if it_dict['valid_strict']
                else ('not pullable' if not meta['pullable']
                      else f"forbidden exception classes: {sorted(bad)}")
            )
            by_pair[norm].append(it_dict)

        sel_pairs = {}
        for pair in sorted(PAIRS_INCLUDE):
            items = by_pair.get(pair, [])
            valid = [c for c in items if c['valid_strict']]
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
                    f"Selected from cd4dd88f (CUA-task import) on {snap_v}. "
                    f"Up to 5 per (agent, normalized_model) pair; ranked by "
                    f"reward then trajectory_bytes then total bytes then recency. "
                    f"Both bedrock and openrouter routes are the same normalized "
                    f"cell; the route is recorded in the entry's provider field. "
                    f"Strict: empty or {{AgentTimeoutError}} exception_stats only."
                )
                p['trajectory_bytes'] = p['trajectory_bytes_max']
            sel_pairs[f"{pair[0]}|{pair[1]}"] = {
                'pair': pair,
                'n_candidates_total': len(items),
                'n_valid_strict': len(valid),
                'picks': [p['id'] for p in picks],
                'route_breakdown': {
                    'bedrock': sum(1 for p in picks if p['provider'] in ('bedrock','claude')),
                    'openrouter': sum(1 for p in picks if p['provider'] == 'openrouter'),
                    'gemini': sum(1 for p in picks if p['provider'] == 'gemini'),
                },
                'notes': 'OK' if len(picks) == 5 else f"SHORTFALL ({len(picks)}/5)",
            }
            grand_total += len(picks)

        out[task_id] = {
            'task_id': task_id, 'task_name': task_name, 'task_hash': task_hash,
            'task_version_id': snap_v,
            'by_pair': {f"{a}|{m}": by_pair.get((a,m), []) for (a,m) in PAIRS_INCLUDE},
            'selection': {'task_version_id': snap_v, 'pairs': sel_pairs},
        }
        print(f"\n== {task_name:18s} v={snap_v} ==")
        for k, v in sel_pairs.items():
            print(f"   {k:60} cand={v['n_candidates_total']:>2d}  valid={v['n_valid_strict']:>2d}  "
                  f"pick={len(v['picks'])} ({v['route_breakdown']})  {v['notes']}")
            for pid in v['picks']:
                pair_tuple = tuple(k.split('|'))
                cand = next(c for c in by_pair[pair_tuple] if c['id']==pid)
                print(f"     - {pid:36s} reward={cand['reward']} provider={cand['provider']:10s} "
                      f"traj={cand['trajectory_bytes_max']:>9d} excs={cand['exception_classes'] or '(clean)'}")
            # Also show non-picked invalids so user can see which were filtered
            pair_tuple = tuple(k.split('|'))
            invalids = [c for c in by_pair[pair_tuple] if not c['valid_strict']]
            for c in invalids[:5]:
                print(f"     - {c['id']:36s} EXCLUDED: {c['rejected_reason']}")

    print(f"\nGRAND TOTAL: {grand_total} picks across 4 CUA tasks (max 40 = 8 cells × 5)")
    with open('/tmp/oddish-import/selection_cua_cd4dd88f.json','w') as f:
        json.dump(out, f, indent=2, default=str)
    print('Saved /tmp/oddish-import/selection_cua_cd4dd88f.json')

if __name__ == '__main__':
    main()
