"""Strict selection for the cd8c33d8 ralphbench import.

Tightens two filters that the original select scripts missed:

1) Filter by ``task_version_id = current_version_id`` so only trials run
   against the LATEST task version are eligible (the dashboard uses the
   same definition of "valid trial for this task").

2) Filter out trials whose top-level ``result.json`` shows infra-class
   exceptions. Per the user's rules, the only exception classes that
   still count as a "valid numeric-reward trial" are
   ``AgentTimeoutError`` and ``VerifierTimeoutError``. Everything else
   (notably ``NonZeroAgentExitCodeError`` from SIGTERM/SIGKILL/exit-1
   agent crashes) is an infra/orchestrator failure and is excluded.

Ranking inside a pair is unchanged: prefer reward=1.0, then larger
trajectory, then larger total artifact bytes, then more recent.
"""
from __future__ import annotations
import os, json, psycopg2, boto3
from collections import defaultdict
from datetime import datetime

EXPERIMENT_ID = 'cd8c33d8'
VALID_EXC = {'AgentTimeoutError', 'VerifierTimeoutError'}

TASKS = [
    ('s3-clone-23996371',      's3-clone',      '23996371'),
    ('slack-clone-b0a98beb',   'slack-clone',   'b0a98beb'),
    ('mastodon-clone-e9964b7a','mastodon-clone','e9964b7a'),
]

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

def list_keys_and_meta(s3, bucket, prefix):
    """List all keys; pull top-level result.json to extract exception classes
    and identify the canonical attempt's trajectory size."""
    total_bytes = 0
    n_files = 0
    top_files = set()
    traj_per_sub = defaultdict(int)
    subs = set()
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []):
            rel = obj['Key'][len(prefix):]
            sz = obj['Size']
            total_bytes += sz
            n_files += 1
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
            obj = s3.get_object(Bucket=bucket, Key=prefix + 'result.json')
            res = json.loads(obj['Body'].read())
            for ev in (res.get('stats',{}).get('evals',{}) or {}).values():
                for cls in (ev.get('exception_stats') or {}).keys():
                    excs.add(cls)
        except Exception:
            excs.add('__MISSING_RESULT_JSON__')
    return {
        'total_bytes': total_bytes,
        'n_files': n_files,
        'top_files': sorted(top_files),
        'subs': sorted(subs),
        'trajectory_bytes_max': max(traj_per_sub.values()) if traj_per_sub else 0,
        'exception_classes': sorted(excs),
        'pullable': pullable,
    }

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
    for task_id, task_name, task_hash in TASKS:
        cur.execute("SELECT current_version_id FROM tasks WHERE id=%s", (task_id,))
        task_version_id = cur.fetchone()[0]
        cur.execute("""
            SELECT id, agent, model, provider, reward, status,
                   attempts, max_attempts, started_at, finished_at,
                   input_tokens, cache_tokens, output_tokens, cost_usd,
                   has_trajectory, name, trial_s3_key, error_message
            FROM trials
            WHERE task_id=%s AND experiment_id=%s AND task_version_id=%s
              AND deleted_at IS NULL AND superseded_by_trial_id IS NULL
              AND status='SUCCESS' AND reward IS NOT NULL
        """, (task_id, EXPERIMENT_ID, task_version_id))
        rows = cur.fetchall()
        print(f"\n=== {task_id}  v={task_version_id}  candidates (SUCCESS+numeric): {len(rows)} ===")
        by_pair = defaultdict(list)
        for r in rows:
            (tid, agent, model, provider, reward, status,
             attempts, max_attempts, started_at, finished_at,
             input_tokens, cache_tokens, output_tokens, cost_usd,
             has_trajectory, name, trial_s3_key, error_message) = r
            nm = norm_model(model)
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
                'trial_s3_key': trial_s3_key, 'error_message': error_message,
            }
            prefix = trial_s3_key or f"tasks/{task_id}/trials/{tid}/"
            if not prefix.endswith('/'): prefix += '/'
            meta = list_keys_and_meta(s3, bucket, prefix)
            it.update(meta)
            # Strict validity:
            # - Must be pullable (top-level result.json exists)
            # - Exception classes (if any) must be a subset of VALID_EXC
            bad_excs = set(meta['exception_classes']) - VALID_EXC
            it['valid_strict'] = meta['pullable'] and not bad_excs
            it['rejected_reason'] = (
                None if it['valid_strict']
                else ('not pullable' if not meta['pullable']
                      else f"infra exception classes: {sorted(bad_excs)}")
            )
            by_pair[(agent, nm)].append(it)

        # Rank & pick within each expected pair
        selection = {'task_version_id': task_version_id, 'pairs': {}}
        for pair in EXPECTED_PAIRS:
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
                    'Selected as one of up to 5 valid trials for this task/agent/model, '
                    'ranked by reward then trajectory_bytes then total artifact bytes then '
                    'recency. Strict: only AgentTimeoutError/VerifierTimeoutError exception '
                    'classes are kept; NonZero/other infra failures are excluded.'
                )
                p['trajectory_bytes'] = p['trajectory_bytes_max']
            selection['pairs'][f"{pair[0]}|{pair[1]}"] = {
                'pair': pair,
                'n_candidates_success_numeric': len(all_items),
                'n_valid_strict': len(valid),
                'picks': [p['id'] for p in picks],
                'notes': ('SHORTFALL' if len(picks) < 5 else 'OK'),
            }

        # Print summary
        npicks = sum(len(v['picks']) for v in selection['pairs'].values())
        print(f"  total strict picks: {npicks}/65")
        for k, v in selection['pairs'].items():
            mark = 'OK' if len(v['picks'])==5 else f"SHORTFALL {len(v['picks'])}/5"
            print(f"    {k:60} cand={v['n_candidates_success_numeric']:>2d}  valid_strict={v['n_valid_strict']:>2d}  picks={len(v['picks'])}  {mark}")

        # Stash by_pair (rejection reasons useful for the report)
        out[task_id] = {
            'task_id': task_id, 'task_name': task_name, 'task_hash': task_hash,
            'task_version_id': task_version_id,
            'by_pair': {f"{a}|{m}": by_pair.get((a,m), []) for a,m in EXPECTED_PAIRS},
            'selection': selection,
        }
    with open('/tmp/oddish-import/selection_strict.json','w') as f:
        json.dump(out, f, indent=2, default=str)
    print('\nSaved /tmp/oddish-import/selection_strict.json')

if __name__ == '__main__':
    main()
