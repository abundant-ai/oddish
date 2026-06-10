"""Import glm-claude-code trials from Oddish experiment e0ef4703 into
s3://zai-swe-marathon/. Mirrors the ralphbench-logs convention: per-task
prefix with _manifest.json + <trial_id>/<top files + canonical task-* attempt>.

Filter:
  - agent='glm-claude-code'
  - status='SUCCESS' AND reward IS NOT NULL
  - task_version_id = latest version that the experiment ran on for the task
  - exception_classes clean or {AgentTimeoutError} only
  - open-internet only (every e0ef4703 task already uses its open-internet
    hash, so no extra filter needed)

Output: matches ralphbench manifest schema (experiment_id, task_versions,
additional_imports, override_validity for any future overrides, etc.).

This script reads the destination credentials from the configparser-style
~/.aws/credentials [zai] profile (NEVER from env vars), so the keys never
leak into commit logs or state files.
"""
from __future__ import annotations
import os, sys, json, time
import boto3, psycopg2
from botocore.config import Config
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter, defaultdict
from datetime import datetime, timezone

DST_BUCKET = 'zai-swe-marathon'
DST_REGION = 'us-west-2'
EXPERIMENT_ID = 'e0ef4703'
AGENT = 'glm-claude-code'
VALID_EXC = {'AgentTimeoutError'}

# Reuse upload primitives from augment_cd4dd88f, but with a different dst client.
sys.path.insert(0, os.path.dirname(__file__))
from augment_cd4dd88f import make_src, list_keys, pick_canonical, transform_top
# We need a custom destination client and a parameterized version of
# upload_one_trial that takes the dst client + bucket as args (augment_cd4dd88f's
# is hard-wired to ralphbench-logs).

def make_zai_dst():
    """Boto3 client that reads from the ~/.aws/credentials [zai] profile.
    Never use os.environ here — that path mixes in ralphbench creds."""
    sess = boto3.session.Session(profile_name='zai', region_name=DST_REGION)
    return sess.client('s3', config=Config(max_pool_connections=32,
                                            retries={'max_attempts':5,'mode':'standard'}))

SRC_BUCKET = os.environ['ODDISH_S3_BUCKET']

def upload_one_trial(src, dst, sel_meta, trial, dst_task_name):
    tid = trial['id']
    src_prefix = trial.get('trial_s3_key') or f"tasks/{sel_meta['task_id']}/trials/{tid}/"
    if not src_prefix.endswith('/'): src_prefix += '/'
    dst_prefix = f"{dst_task_name}/{tid}/"
    keys = list_keys(src, src_prefix)
    if not keys:
        return None, 0
    canonical = pick_canonical(src, src_prefix, keys)
    to_copy = []
    for k, sz in keys:
        rel = k[len(src_prefix):]
        if '/' not in rel:
            to_copy.append((k, sz, dst_prefix + rel, rel))
        elif rel.split('/',1)[0] == canonical:
            to_copy.append((k, sz, dst_prefix + rel, rel))
    def _proc(item):
        sk, sz, dk, rel = item
        body = src.get_object(Bucket=SRC_BUCKET, Key=sk)['Body'].read()
        if rel in ('config.json','result.json') and '/' not in rel:
            body = transform_top(body, sel_meta['task_name'], sel_meta['task_hash'],
                                  sel_meta['task_version_id'])
        dst.put_object(Bucket=DST_BUCKET, Key=dk, Body=body)
        return len(body)
    total = 0
    with ThreadPoolExecutor(max_workers=16) as pool:
        for fut in as_completed([pool.submit(_proc, it) for it in to_copy]):
            total += fut.result()
    return canonical, total

def build_entry(trial, sel_meta, canonical, dst_task_name, partial_score=None):
    e = {
        'agent': trial['agent'],
        'model': trial['model'],
        'provider': trial['provider'],
        'reward': trial['reward'],
        'status': trial['status'],
        'attempts': trial['attempts'],
        'max_attempts': trial['max_attempts'],
        'task_version_id': sel_meta['task_version_id'],
        'started_at': trial['started_at'],
        'finished_at': trial['finished_at'],
        'input_tokens': trial.get('input_tokens'),
        'cache_tokens': trial.get('cache_tokens'),
        'output_tokens': trial.get('output_tokens'),
        'cost_usd': trial.get('cost_usd'),
        'has_trajectory': trial.get('has_trajectory'),
        'trajectory_bytes': trial.get('trajectory_bytes', 0),
        'exception_classes': trial.get('exception_classes', []),
        'origin': 'oddish',
        'artifact_layout': 'harbor-nested',
        'artifact_prefix': f"s3://{DST_BUCKET}/{dst_task_name}/{trial['id']}/",
        'source_trial_name': trial.get('source_trial_name'),
        'source_oddish_task_id': sel_meta['task_id'],
        'source_oddish_trial_id': trial['id'],
        'source_oddish_experiment_id': EXPERIMENT_ID,
        'canonical_attempt_dir': canonical,
        'task_name': sel_meta['task_name'],
        'task_hash': sel_meta['task_hash'],
        'selection_note': trial.get('selection_note', ''),
        'raw_model': trial.get('raw_model', trial['model']),
    }
    if partial_score is not None:
        e['verifier_partial_score'] = partial_score
    return e

def fetch_partial_score(src, src_prefix, canonical):
    """Read the verifier metrics.json for the canonical attempt and return a
    score-like field. Accepts both standard (verifier/metrics.json) and CUA
    two-stage (verifier/correctness/metrics.json) layouts."""
    if not canonical: return None
    for sub in ('verifier/metrics.json', 'verifier/correctness/metrics.json'):
        try:
            m = json.loads(src.get_object(Bucket=SRC_BUCKET,
                Key=src_prefix + canonical + '/' + sub)['Body'].read())
            if not isinstance(m, dict): continue
            for k in ('binary_strict','partial_score','score','pass_rate','accuracy'):
                if k in m and isinstance(m[k],(int,float)):
                    return m[k]
            if 'reward' in m and isinstance(m['reward'],(int,float)):
                return m['reward']
        except Exception:
            continue
    return None

def main():
    src = make_src()
    dst = make_zai_dst()

    url = os.environ['ODDISH_DATABASE_URL'].replace('postgresql+asyncpg://','postgresql://')
    cur = psycopg2.connect(url + '?sslmode=require').cursor()

    # 1. Latest task_version_id per task in this experiment for this agent
    cur.execute("""
      WITH per_task AS (
        SELECT task_id, task_version_id,
               CAST(SUBSTRING(task_version_id FROM 'v([0-9]+)$') AS int) AS vnum,
               COUNT(*) n
        FROM trials WHERE experiment_id=%s AND agent=%s
          AND deleted_at IS NULL AND superseded_by_trial_id IS NULL
        GROUP BY task_id, task_version_id
      )
      SELECT DISTINCT ON (task_id) task_id, task_version_id
      FROM per_task ORDER BY task_id, vnum DESC NULLS LAST
    """, (EXPERIMENT_ID, AGENT))
    latest = dict(cur.fetchall())

    # 2. For each task: SUCCESS+numeric trials on that version, strict-valid only
    cur.execute("""
      SELECT t.id, t.task_id, t.task_version_id, t.agent, t.model, t.provider,
             t.reward, t.status, t.attempts, t.max_attempts,
             t.started_at, t.finished_at,
             t.input_tokens, t.cache_tokens, t.output_tokens, t.cost_usd,
             t.has_trajectory, t.name, t.trial_s3_key, tk.name
      FROM trials t JOIN tasks tk ON tk.id = t.task_id
      WHERE t.experiment_id=%s AND t.agent=%s
        AND t.deleted_at IS NULL AND t.superseded_by_trial_id IS NULL
        AND t.status='SUCCESS' AND t.reward IS NOT NULL
    """, (EXPERIMENT_ID, AGENT))
    rows = cur.fetchall()
    by_task = defaultdict(list)
    for r in rows:
        (rid, tid, vid, agent, model, prov, reward, status, att, mx, sa, fa,
         it, ct, ot, cu, ht, name, s3key, task_name) = r
        if latest.get(tid) != vid:
            continue  # not the latest version
        prefix = (s3key or f"tasks/{tid}/trials/{rid}/")
        if not prefix.endswith('/'): prefix += '/'
        # exception_stats from top-level result.json
        try:
            res = json.loads(src.get_object(Bucket=SRC_BUCKET, Key=prefix+'result.json')['Body'].read())
            excs = set()
            for ev in (res.get('stats',{}).get('evals',{}) or {}).values():
                excs.update((ev.get('exception_stats') or {}).keys())
        except Exception:
            excs = {'__MISSING__'}
        if (excs - VALID_EXC):
            continue  # strict-filter excludes
        # Compute trajectory_bytes for ranking
        traj_max = 0
        for k, sz in list_keys(src, prefix):
            if k.endswith('agent/trajectory.json'):
                traj_max = max(traj_max, sz)
        by_task[(tid, task_name, vid)].append({
            'id': rid, 'agent': agent, 'model': model, 'raw_model': model,
            'provider': prov, 'reward': reward, 'status': str(status).lower(),
            'attempts': att, 'max_attempts': mx,
            'started_at': sa.isoformat() if sa else None,
            'finished_at': fa.isoformat() if fa else None,
            'input_tokens': it, 'cache_tokens': ct, 'output_tokens': ot,
            'cost_usd': cu, 'has_trajectory': ht, 'source_trial_name': name,
            'trial_s3_key': prefix,
            'trajectory_bytes': traj_max,
            'exception_classes': sorted(excs),
            'selection_note': (
                f"Imported from Oddish experiment {EXPERIMENT_ID} (glm-x-preview "
                f"smoke test) for agent={agent}, model={model} on task version "
                f"{vid}. Strict validity: SUCCESS + numeric reward + "
                f"exception_stats empty or AgentTimeoutError-only. Open-internet "
                f"task variant (canonical task hash, not the closed-internet variant)."
            ),
        })

    # 3. Per task: rank & cap at 5. Ranking: reward 1.0 first, then trajectory_bytes,
    #    then recency.
    grand_total = 0
    state = {'tasks': {}}
    state_path = '/tmp/oddish-import/state_zai_marathon.json'
    if os.path.exists(state_path):
        state = json.load(open(state_path))
    def _ep(c):
        s = c['started_at']
        return datetime.fromisoformat(s).timestamp() if s else 0
    print(f"{'task':35s} {'latest':40s} n  pass")
    for (tid, task_name, vid), cands in sorted(by_task.items()):
        cands.sort(key=lambda c: (
            0 if c['reward'] == 1.0 else 1,
            -float(c.get('trajectory_bytes') or 0),
            -_ep(c),
        ))
        picks = cands[:5]
        # Hash from task_id e.g. "biofabric-rust-rewrite-897c0fc8" -> "897c0fc8"
        task_hash = tid.rsplit('-',1)[-1]
        sel_meta = {'task_id': tid, 'task_name': task_name,
                    'task_hash': task_hash, 'task_version_id': vid}
        ts = state['tasks'].setdefault(tid, {'task_name': task_name, 'task_hash': task_hash,
                                              'task_version_id': vid,
                                              'new_entries': {}, 'failures': []})
        for p in picks:
            if p['id'] in ts['new_entries']:
                continue
            try:
                canonical, nb = upload_one_trial(src, dst, sel_meta, p, task_name)
                if canonical is None:
                    ts['failures'].append({'trial_id': p['id'], 'reason': 'no keys'})
                    continue
                score = fetch_partial_score(src, p['trial_s3_key'], canonical)
                entry = build_entry(p, sel_meta, canonical, task_name, partial_score=score)
                ts['new_entries'][p['id']] = entry
                with open(state_path, 'w') as f:
                    json.dump(state, f, indent=2)
            except Exception as e:
                ts['failures'].append({'trial_id': p['id'], 'reason': str(e)})
                with open(state_path, 'w') as f:
                    json.dump(state, f, indent=2)
        n_pass = sum(1 for p in picks if p['reward'] == 1.0)
        print(f"  {task_name:35s} {vid:40s} {len(picks)}  pass={n_pass}")
        grand_total += len(picks)
    print(f"\nGrand total picks: {grand_total}")

    # 4. Write per-task _manifest.json
    for tid, ts in state['tasks'].items():
        task_name = ts['task_name']
        trials = ts['new_entries']
        if not trials: continue
        def _idx(t):
            tail = t.rsplit('-',1)[-1]
            return (int(tail) if tail.isdigit() else 1<<30, t)
        trials = dict(sorted(trials.items(), key=lambda kv: _idx(kv[0])))
        pair_counts = Counter((e['agent'], e['model']) for e in trials.values())
        n_pass = sum(1 for e in trials.values() if e.get('reward') == 1.0)
        manifest = {
            'experiment_id': EXPERIMENT_ID,
            'experiment_ids': [EXPERIMENT_ID],
            'n_trials': len(trials),
            'note': (
                f"Created at {datetime.now(timezone.utc).isoformat()} from Oddish "
                f"experiment {EXPERIMENT_ID} ('glm-x-preview smoke test'). Imports "
                f"up to 5 strict-valid glm-claude-code / zai/glm-x-preview[1m] "
                f"trials for the open-internet variant of this task (canonical "
                f"task hash). Strict validity: SUCCESS + numeric reward + "
                f"exception_stats empty or AgentTimeoutError-only."
            ),
            'task': tid, 'task_name': task_name,
            'task_hash': ts['task_hash'],
            'task_version_id': ts['task_version_id'],
            'task_versions': [ts['task_version_id']],
            'task_hashes': [ts['task_hash']],
            'is_multi_version': False,
            'pair_counts': {f"{a}|{m}": n for (a,m), n in pair_counts.items()},
            'n_pass': n_pass,
            'duplicate_fingerprint_count': 0,
            'trials': trials,
            'additional_imports': [{
                'imported_at': datetime.now(timezone.utc).isoformat(),
                'source_oddish_experiment_id': EXPERIMENT_ID,
                'task_version_id_at_snapshot': ts['task_version_id'],
                'n_trials_added': len(trials),
                'pairs_added': [['glm-claude-code', 'zai/glm-x-preview[1m]']],
                'note': 'Initial population of zai-swe-marathon bucket.',
            }],
        }
        body = json.dumps(manifest, indent=2, default=str).encode('utf-8')
        dst.put_object(Bucket=DST_BUCKET, Key=f"{task_name}/_manifest.json",
                       Body=body, ContentType='application/json')
        # local copy (no creds in it, just data)
        with open(f"/tmp/oddish-import/{task_name}_zai_manifest.json",'w') as f:
            f.write(body.decode())
        print(f"  manifest: s3://{DST_BUCKET}/{task_name}/_manifest.json "
              f"(n_trials={len(trials)}, n_pass={n_pass})")

if __name__ == '__main__':
    main()
