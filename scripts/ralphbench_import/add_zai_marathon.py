"""Follow-up importer for s3://zai-swe-marathon/. Adds any *new* strict-valid
glm-claude-code trials from Oddish experiment e0ef4703 that aren't already
in the bucket, capping each cell at 5 total (existing + new).

Skips per user instruction:
  - post-train-ifeval
  - ruby-rust-port
  - nextjs-vite-rewrite

Strict validity gate (unchanged from original import):
  - status='SUCCESS' AND reward IS NOT NULL
  - task_version_id = latest version that the experiment ran on
  - exception_stats empty or only {AgentTimeoutError}
  - runtime network policy is open-internet (no allowlist OR >= 10 CIDRs)

For each task touched:
  - If a manifest already exists in the bucket: augment in place, append
    a new entry to additional_imports[], update n_trials / n_pass /
    pair_counts.
  - If brand new: create a fresh manifest with the same schema as the
    initial import.
"""
from __future__ import annotations
import os, sys, json
import boto3, psycopg2
from botocore.config import Config
from collections import Counter, defaultdict
from datetime import datetime, timezone

DST_BUCKET = 'zai-swe-marathon'
DST_REGION = 'us-west-2'
EXPERIMENT_ID = 'e0ef4703'
AGENT = 'glm-claude-code'
VALID_EXC = {'AgentTimeoutError'}
MAX_PER_CELL = 5
# Tasks explicitly excluded from the zai bucket by user instruction:
#   - nextjs-vite-rewrite: original 3-way exclusion list; every glm-claude-code
#     candidate in e0ef4703 has NonZeroAgentExitCodeError without a verifier
#     pass, so it would not pass the open-rule anyway.
#   - post-train-ifeval: removed from the bucket per later instruction.
#   - kubernetes-rust-rewrite: removed from the bucket per later instruction.
SKIP_TASKS = {'nextjs-vite-rewrite', 'post-train-ifeval', 'kubernetes-rust-rewrite'}

sys.path.insert(0, os.path.dirname(__file__))
from import_zai_marathon import (
    make_zai_dst, upload_one_trial, build_entry,
    classify_network_policy, fetch_partial_score,
)
from augment_cd4dd88f import make_src, list_keys, pick_canonical

SRC_BUCKET = os.environ['ODDISH_S3_BUCKET']


def fetch_manifest(dst, task_name):
    try:
        body = dst.get_object(Bucket=DST_BUCKET, Key=f"{task_name}/_manifest.json")['Body'].read()
        return json.loads(body)
    except dst.exceptions.NoSuchKey:
        return None
    except Exception as e:
        msg = str(e)
        if 'NoSuchKey' in msg or '404' in msg:
            return None
        raise


def rank(cands):
    """Same ranking as original: reward=1.0 first, then trajectory_bytes, then recency."""
    def _ep(c):
        s = c.get('started_at')
        if not s: return 0
        return datetime.fromisoformat(s).timestamp()
    cands.sort(key=lambda c: (
        0 if c['reward'] == 1.0 else 1,
        -float(c.get('trajectory_bytes') or 0),
        -_ep(c),
    ))
    return cands


def main():
    src = make_src()
    dst = make_zai_dst()

    url = os.environ['ODDISH_DATABASE_URL'].replace('postgresql+asyncpg://','postgresql://')
    cur = psycopg2.connect(url + '?sslmode=require').cursor()

    # Latest version per task in this experiment for this agent
    cur.execute("""
      WITH per_task AS (
        SELECT task_id, task_version_id,
               CAST(SUBSTRING(task_version_id FROM 'v([0-9]+)$') AS int) AS vnum
        FROM trials WHERE experiment_id=%s AND agent=%s
          AND deleted_at IS NULL AND superseded_by_trial_id IS NULL
      )
      SELECT DISTINCT ON (task_id) task_id, task_version_id
      FROM per_task ORDER BY task_id, vnum DESC NULLS LAST
    """, (EXPERIMENT_ID, AGENT))
    latest = dict(cur.fetchall())

    # Candidate pool
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
    by_task = defaultdict(list)
    for r in cur.fetchall():
        (rid, tid, vid, agent, model, prov, reward, status, att, mx, sa, fa,
         it, ct, ot, cu, ht, name, s3key, task_name) = r
        if task_name in SKIP_TASKS: continue
        if latest.get(tid) != vid: continue
        prefix = (s3key or f"tasks/{tid}/trials/{rid}/")
        if not prefix.endswith('/'): prefix += '/'
        try:
            res = json.loads(src.get_object(Bucket=SRC_BUCKET, Key=prefix+'result.json')['Body'].read())
            excs = set()
            for ev in (res.get('stats',{}).get('evals',{}) or {}).values():
                excs.update((ev.get('exception_stats') or {}).keys())
        except Exception:
            excs = {'__MISSING__'}
        if (excs - VALID_EXC): continue
        # trajectory bytes for ranking
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
                f"Imported from Oddish experiment {EXPERIMENT_ID} for agent={agent}, "
                f"model={model} on task version {vid}. Strict validity: SUCCESS + "
                f"numeric reward + exception_stats empty or AgentTimeoutError-only. "
                f"Open-internet variant gate at upload time."
            ),
        })

    # Per task: figure out what to add (skipping IDs already in bucket, capping
    # at MAX_PER_CELL total).
    state_path = '/tmp/oddish-import/state_zai_followup.json'
    state = {'tasks': {}}
    if os.path.exists(state_path):
        state = json.load(open(state_path))

    summary = []
    for (tid, task_name, vid), cands in sorted(by_task.items()):
        existing_manifest = fetch_manifest(dst, task_name)
        existing_ids = set(existing_manifest['trials'].keys()) if existing_manifest else set()
        capacity = max(0, MAX_PER_CELL - len(existing_ids))

        rank(cands)
        new_cands = [c for c in cands if c['id'] not in existing_ids]
        if capacity == 0 or not new_cands:
            summary.append((task_name, len(existing_ids), 0, 0, 'capacity=0' if capacity==0 else 'no new'))
            continue

        task_hash = tid.rsplit('-',1)[-1]
        sel_meta = {'task_id': tid, 'task_name': task_name,
                    'task_hash': task_hash, 'task_version_id': vid}
        ts = state['tasks'].setdefault(tid, {
            'task_name': task_name, 'task_hash': task_hash,
            'task_version_id': vid, 'new_entries': {}, 'closed_skips': [],
            'failures': []})

        added = 0
        closed_skipped = 0
        for p in new_cands:
            if added >= capacity: break
            if p['id'] in ts['new_entries']:
                added += 1
                continue
            # Open-internet gate
            src_keys = list_keys(src, p['trial_s3_key'])
            canon_src = pick_canonical(src, p['trial_s3_key'], src_keys)
            policy, allowlist_n, is_open = classify_network_policy(
                src, p['trial_s3_key'], canon_src)
            if not is_open:
                ts['closed_skips'].append({'trial_id': p['id'], 'policy': policy})
                closed_skipped += 1
                with open(state_path,'w') as f: json.dump(state, f, indent=2, default=str)
                continue
            # Upload
            try:
                canonical, _ = upload_one_trial(src, dst, sel_meta, p, task_name)
                if canonical is None:
                    ts['failures'].append({'trial_id': p['id'], 'reason': 'no keys'})
                    continue
                score = fetch_partial_score(src, p['trial_s3_key'], canonical)
                entry = build_entry(p, sel_meta, canonical, task_name,
                                    partial_score=score, network_policy=policy,
                                    network_allowlist_size=allowlist_n)
                ts['new_entries'][p['id']] = entry
                added += 1
                with open(state_path,'w') as f: json.dump(state, f, indent=2, default=str)
            except Exception as e:
                ts['failures'].append({'trial_id': p['id'], 'reason': str(e)})
                with open(state_path,'w') as f: json.dump(state, f, indent=2, default=str)

        # Write/update manifest
        new_entries = ts['new_entries']
        if not new_entries:
            summary.append((task_name, len(existing_ids), 0, closed_skipped,
                            'all candidates closed-internet' if closed_skipped else 'no uploads'))
            continue

        if existing_manifest:
            merged = dict(existing_manifest['trials'])
            merged.update(new_entries)
            def _idx(t):
                tail = t.rsplit('-',1)[-1]
                return (int(tail) if tail.isdigit() else 1<<30, t)
            merged = dict(sorted(merged.items(), key=lambda kv: _idx(kv[0])))
            manifest = dict(existing_manifest)
            manifest['trials'] = merged
            manifest['n_trials'] = len(merged)
            pair_counts = Counter((e['agent'], e['model']) for e in merged.values())
            manifest['pair_counts'] = {f"{a}|{m}": n for (a,m), n in pair_counts.items()}
            manifest['n_pass'] = sum(1 for e in merged.values() if e.get('reward') == 1.0)
            tv = set(manifest.get('task_versions', [manifest.get('task_version_id')]))
            tv.add(vid)
            manifest['task_versions'] = sorted(tv)
            manifest['is_multi_version'] = len(tv) > 1
            ai = manifest.setdefault('additional_imports', [])
            ai.append({
                'imported_at': datetime.now(timezone.utc).isoformat(),
                'source_oddish_experiment_id': EXPERIMENT_ID,
                'task_version_id_at_snapshot': vid,
                'n_trials_added': len(new_entries),
                'pairs_added': [['glm-claude-code', list(new_entries.values())[0]['model']]],
                'note': (f'Follow-up import: filled cell from {len(existing_ids)} to '
                         f'{len(merged)} valid trials.'),
            })
        else:
            def _idx(t):
                tail = t.rsplit('-',1)[-1]
                return (int(tail) if tail.isdigit() else 1<<30, t)
            sorted_entries = dict(sorted(new_entries.items(), key=lambda kv: _idx(kv[0])))
            pair_counts = Counter((e['agent'], e['model']) for e in sorted_entries.values())
            manifest = {
                'experiment_id': EXPERIMENT_ID,
                'experiment_ids': [EXPERIMENT_ID],
                'n_trials': len(sorted_entries),
                'note': (
                    f"Created at {datetime.now(timezone.utc).isoformat()} from Oddish "
                    f"experiment {EXPERIMENT_ID}. Up to 5 strict-valid glm-claude-code "
                    f"trials for the open-internet variant of this task. Strict "
                    f"validity: SUCCESS + numeric reward + exception_stats empty or "
                    f"AgentTimeoutError-only; runtime open-internet gate at upload."
                ),
                'task': tid, 'task_name': task_name,
                'task_hash': task_hash, 'task_version_id': vid,
                'task_versions': [vid], 'task_hashes': [task_hash],
                'is_multi_version': False,
                'pair_counts': {f"{a}|{m}": n for (a,m), n in pair_counts.items()},
                'n_pass': sum(1 for e in sorted_entries.values() if e.get('reward') == 1.0),
                'duplicate_fingerprint_count': 0,
                'trials': sorted_entries,
                'additional_imports': [{
                    'imported_at': datetime.now(timezone.utc).isoformat(),
                    'source_oddish_experiment_id': EXPERIMENT_ID,
                    'task_version_id_at_snapshot': vid,
                    'n_trials_added': len(sorted_entries),
                    'pairs_added': [['glm-claude-code',
                                      list(sorted_entries.values())[0]['model']]],
                    'note': 'Follow-up import: new task prefix added to bucket.',
                }],
            }
        body = json.dumps(manifest, indent=2, default=str).encode('utf-8')
        dst.put_object(Bucket=DST_BUCKET, Key=f"{task_name}/_manifest.json",
                       Body=body, ContentType='application/json')
        with open(f"/tmp/oddish-import/{task_name}_zai_followup_manifest.json",'w') as f:
            f.write(body.decode())
        summary.append((task_name, len(existing_ids), len(new_entries),
                        closed_skipped, 'OK'))

    print(f"\n=== Summary ===")
    print(f"{'task':28s} {'had':>4s} {'added':>5s} {'closed-skip':>11s}  result")
    total_added = total_closed = 0
    for tn, had, added, cs, result in summary:
        total_added += added; total_closed += cs
        print(f"  {tn:28s} {had:>4d} {added:>5d} {cs:>11d}  {result}")
    print(f"\nTotal new trials uploaded: {total_added}")
    print(f"Total closed-internet skips: {total_closed}")

if __name__ == '__main__':
    main()
