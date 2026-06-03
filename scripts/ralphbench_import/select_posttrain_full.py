"""Fill in remaining post-train-ifeval cells in s3://ralphbench-logs/post-train-ifeval/.

Cells to pick (5 valid per pair where available):
  Model-bearing cells on v34 (same content hash as v32/v33):
    - claude-code / anthropic/claude-opus-4-8     (cd4dd88f, v34)  [normalized from global.*]
    - gemini-cli  / gemini/gemini-3.5-flash       (cd4dd88f, v34)  [normalized from google/*]
    - gemini-cli  / gemini/gemini-3.1-pro-preview (6f068eca, v34)  [normalized from google/*]
    - terminus-2  / anthropic/claude-opus-4-7     (0042aebf, v34)  [normalized from global.*]
    - terminus-2  / openai/gpt-5.5                (0042aebf, v34)
    - terminus-2  / gemini/gemini-3.1-pro-preview (0042aebf, v34)
    - terminus-2  / openrouter/deepseek/deepseek-v4-pro (0042aebf, v34)
    - terminus-2  / openrouter/moonshotai/kimi-k2.6     (0042aebf, v34)
    - terminus-2  / openrouter/minimax/minimax-m2.7     (0042aebf, v34)
    - terminus-2  / openrouter/z-ai/glm-5.1             (0042aebf, v34)  [only 3 SUCCESS; 2 RUNNING]
    - kimi-cli    / openrouter/moonshotai/kimi-k2.6     (0042aebf, v34)
  nop/oracle baselines on v31 (older version OK per user):
    - nop    / default  (6f068eca, v31)
    - oracle / default  (6f068eca, v31)

Strict validity: SUCCESS + numeric reward + exception_stats clean or
{AgentTimeoutError} only. Tie-break: reward=1.0 first, then
verifier/metrics.json partial_score (for post-train-ifeval the field is
'binary_strict' — fall back to 'partial_score' if present), then
trajectory_bytes, then total_bytes, then recency.

Note: the existing codex/openai/gpt-5.5 (v33) cell is left alone.
"""
from __future__ import annotations
import os, json, psycopg2, boto3
from collections import defaultdict
from datetime import datetime

VALID_EXC = {'AgentTimeoutError'}
TASK_ID    = 'post-train-ifeval-c9f2cfbc'
TASK_NAME  = 'post-train-ifeval'
TASK_HASH  = 'c9f2cfbc'

# (agent, raw_model_filter) → (agent, normalized_model)
# raw_model_filter is the value stored in trials.model in Oddish DB.
RAW_TO_NORM = {
    ('claude-code', 'global.anthropic.claude-opus-4-8'):     ('claude-code', 'anthropic/claude-opus-4-8'),
    ('gemini-cli',  'google/gemini-3.5-flash'):              ('gemini-cli',  'gemini/gemini-3.5-flash'),
    ('gemini-cli',  'gemini/gemini-3.5-flash'):              ('gemini-cli',  'gemini/gemini-3.5-flash'),
    ('gemini-cli',  'google/gemini-3.1-pro-preview'):        ('gemini-cli',  'gemini/gemini-3.1-pro-preview'),
    ('terminus-2',  'global.anthropic.claude-opus-4-7'):     ('terminus-2',  'anthropic/claude-opus-4-7'),
    ('terminus-2',  'openai/gpt-5.5'):                       ('terminus-2',  'openai/gpt-5.5'),
    ('terminus-2',  'gemini/gemini-3.1-pro-preview'):        ('terminus-2',  'gemini/gemini-3.1-pro-preview'),
    ('terminus-2',  'openrouter/deepseek/deepseek-v4-pro'):  ('terminus-2',  'openrouter/deepseek/deepseek-v4-pro'),
    ('terminus-2',  'openrouter/moonshotai/kimi-k2.6'):      ('terminus-2',  'openrouter/moonshotai/kimi-k2.6'),
    ('terminus-2',  'openrouter/minimax/minimax-m2.7'):      ('terminus-2',  'openrouter/minimax/minimax-m2.7'),
    ('terminus-2',  'openrouter/z-ai/glm-5.1'):              ('terminus-2',  'openrouter/z-ai/glm-5.1'),
    ('kimi-cli',    'openrouter/moonshotai/kimi-k2.6'):      ('kimi-cli',    'openrouter/moonshotai/kimi-k2.6'),
    ('nop',         'default'):                              ('nop',         'default'),
    ('oracle',      'default'):                              ('oracle',      'default'),
}

# (cell_id, version_constraint, experiment_constraint) — defines each cell
CELLS = [
    ('claude-code|anthropic/claude-opus-4-8',          'v34', ['6f068eca']),
    ('gemini-cli|gemini/gemini-3.5-flash',             'v34', ['6f068eca']),
    ('gemini-cli|gemini/gemini-3.1-pro-preview',       'v34', ['6f068eca']),
    ('terminus-2|anthropic/claude-opus-4-7',           'v34', ['0042aebf']),
    ('terminus-2|openai/gpt-5.5',                      'v34', ['0042aebf']),
    ('terminus-2|gemini/gemini-3.1-pro-preview',       'v34', ['0042aebf']),
    ('terminus-2|openrouter/deepseek/deepseek-v4-pro', 'v34', ['0042aebf']),
    ('terminus-2|openrouter/moonshotai/kimi-k2.6',     'v34', ['0042aebf']),
    ('terminus-2|openrouter/minimax/minimax-m2.7',     'v34', ['0042aebf']),
    ('terminus-2|openrouter/z-ai/glm-5.1',             'v34', ['0042aebf']),
    ('kimi-cli|openrouter/moonshotai/kimi-k2.6',       'v34', ['0042aebf']),
    ('nop|default',                                    'v31', ['6f068eca']),
    ('oracle|default',                                 'v31', ['6f068eca']),
]

def list_meta_and_score(s3, bucket, prefix):
    """List artifacts + extract top-level exception_stats and inner
    verifier/metrics.json 'binary_strict' / 'partial_score' for ranking."""
    total = 0; n_files = 0; top_files = set()
    traj_per_sub = defaultdict(int)
    canonical_subdir = None
    metrics_by_sub = {}
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []):
            rel = obj['Key'][len(prefix):]
            sz = obj['Size']
            total += sz; n_files += 1
            if '/' in rel:
                sub = rel.split('/',1)[0]
                if rel.endswith('agent/trajectory.json'):
                    traj_per_sub[sub] = sz
            else:
                top_files.add(rel)
    excs = set()
    pullable = n_files > 0 and 'result.json' in top_files
    rs = {}
    if pullable:
        try:
            res = json.loads(s3.get_object(Bucket=bucket, Key=prefix+'result.json')['Body'].read())
            for ev in (res.get('stats',{}).get('evals',{}) or {}).values():
                for cls in (ev.get('exception_stats') or {}).keys():
                    excs.add(cls)
                rs.update((ev.get('reward_stats') or {}).get('reward') or {})
        except Exception:
            excs.add('__MISSING_RESULT_JSON__')
    # Find canonical
    for rv, names in rs.items():
        for n in names or []:
            if n.startswith('task-'):
                canonical_subdir = n; break
        if canonical_subdir: break
    if canonical_subdir is None:
        # First task-* subdir we saw
        subs = [k for k in traj_per_sub.keys() if k.startswith('task-')]
        if subs:
            canonical_subdir = subs[0]
    # Read inner verifier/metrics.json for the canonical attempt
    score_field = None
    if canonical_subdir:
        try:
            cm = json.loads(s3.get_object(
                Bucket=bucket,
                Key=prefix + canonical_subdir + '/verifier/metrics.json')['Body'].read())
            if isinstance(cm, dict):
                # Prefer binary_strict (the IFEval primary score), then partial_score
                for k in ('binary_strict', 'partial_score', 'score'):
                    if k in cm and isinstance(cm[k], (int, float)):
                        score_field = float(cm[k])
                        break
        except Exception:
            pass
    return {
        'total_bytes': total, 'n_files': n_files,
        'trajectory_bytes_max': max(traj_per_sub.values()) if traj_per_sub else 0,
        'exception_classes': sorted(excs),
        'pullable': pullable,
        'canonical_subdir': canonical_subdir,
        'partial_score': score_field,
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
    for cell_id, ver, exps in CELLS:
        agent, norm_model = cell_id.split('|', 1)
        # Find raw model strings that map to this normalized cell
        raw_models = [raw for (a,raw),(na,nm) in RAW_TO_NORM.items()
                      if (na,nm) == (agent, norm_model) and a == agent]
        task_version_id = f"{TASK_ID}-{ver}"
        cur.execute(f"""
            SELECT id, agent, model, provider, reward, status,
                   attempts, max_attempts, started_at, finished_at,
                   input_tokens, cache_tokens, output_tokens, cost_usd,
                   has_trajectory, name, trial_s3_key, experiment_id
            FROM trials WHERE task_id=%s AND task_version_id=%s
              AND deleted_at IS NULL AND superseded_by_trial_id IS NULL
              AND status='SUCCESS' AND reward IS NOT NULL
              AND experiment_id = ANY(%s)
              AND agent=%s AND model = ANY(%s)
        """, (TASK_ID, task_version_id, exps, agent, raw_models))
        rows = cur.fetchall()

        items = []
        for r in rows:
            (tid, ag, model, provider, reward, status,
             attempts, max_attempts, sa, fa, it, ct, ot, cu, ht, name, s3key, exp_id) = r
            it_dict = {
                'id': tid, 'agent': agent, 'model': norm_model, 'raw_model': model,
                'provider': provider, 'reward': reward,
                'status': str(status).lower(), 'attempts': attempts, 'max_attempts': max_attempts,
                'started_at': sa.isoformat() if sa else None,
                'finished_at': fa.isoformat() if fa else None,
                'input_tokens': it, 'cache_tokens': ct, 'output_tokens': ot,
                'cost_usd': cu, 'has_trajectory': ht, 'source_trial_name': name,
                'trial_s3_key': s3key, 'source_oddish_experiment_id': exp_id,
                'task_version_id': task_version_id,
            }
            prefix = s3key or f"tasks/{TASK_ID}/trials/{tid}/"
            if not prefix.endswith('/'): prefix += '/'
            meta = list_meta_and_score(s3, bucket, prefix)
            it_dict.update(meta)
            bad = set(meta['exception_classes']) - VALID_EXC
            it_dict['valid_strict'] = meta['pullable'] and not bad
            items.append(it_dict)

        valid = [c for c in items if c['valid_strict']]
        def _ep(c):
            s = c['started_at']
            return datetime.fromisoformat(s).timestamp() if s else 0
        # Tie-break:
        #   1. reward=1.0 first
        #   2. partial_score (binary_strict) DESC
        #   3. trajectory_bytes DESC
        #   4. total_bytes DESC
        #   5. recency DESC
        valid.sort(key=lambda c: (
            0 if c['reward'] == 1.0 else 1,
            -float(c.get('partial_score') if c.get('partial_score') is not None else -1),
            -float(c.get('trajectory_bytes_max') or 0),
            -float(c.get('total_bytes') or 0),
            -_ep(c),
        ))
        picks = valid[:5]
        for p in picks:
            p['selection_note'] = (
                f"Selected for post-train-ifeval / {cell_id} on {task_version_id} from "
                f"experiment {p['source_oddish_experiment_id']}. Up to 5 per cell; ranked by "
                f"reward then verifier-metrics 'binary_strict'/'partial_score' then "
                f"trajectory_bytes then total_bytes then recency. Strict validity: clean or "
                f"AgentTimeoutError exception_stats only."
            )
            p['trajectory_bytes'] = p['trajectory_bytes_max']

        out[cell_id] = {
            'cell_id': cell_id, 'agent': agent, 'model': norm_model,
            'version': ver, 'task_version_id': task_version_id,
            'experiments': exps,
            'n_total': len(items),
            'n_valid_strict': len(valid),
            'picks': [p['id'] for p in picks],
            'all_candidates': items,
        }
        grand_total += len(picks)
        print(f"\n== {cell_id:60s} v={ver} exp={exps} ==")
        print(f"   total={len(items):>2d}  valid={len(valid):>2d}  pick={len(picks)}/5"
              + ('  ' if len(picks)==5 else f"  (SHORT {len(picks)}/5)"))
        for p in picks:
            ps = f"score={p['partial_score']:.4f}" if p.get('partial_score') is not None else "score=-"
            print(f"     - {p['id']:38s} reward={p['reward']} {ps}  "
                  f"prov={p['provider']:10s} traj={p['trajectory_bytes_max']:>9d} "
                  f"excs={p['exception_classes'] or '(clean)'} exp={p['source_oddish_experiment_id']}")
        for c in [x for x in items if not x['valid_strict']]:
            print(f"     - {c['id']:38s} EXCLUDED  excs={c['exception_classes'] or 'NA'}")

    print(f"\nGRAND TOTAL: {grand_total} picks across {len(CELLS)} cells "
          f"(max = {5*len(CELLS)})")
    with open('/tmp/oddish-import/selection_posttrain_full.json','w') as f:
        json.dump(out, f, indent=2, default=str)
    print('Saved /tmp/oddish-import/selection_posttrain_full.json')

if __name__ == '__main__':
    main()
