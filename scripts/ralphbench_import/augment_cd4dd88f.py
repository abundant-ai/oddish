"""Upload cd4dd88f trial dirs to ralphbench-logs and merge them into each
task's existing _manifest.json.

For each task in selection_cd4dd88f.json:
  1. Stream each selected trial from Oddish S3 -> ralphbench-logs
     (top-level files + canonical task-* attempt only, with
     task_name/task_hash/task_version_id injected into top-level config/result).
  2. Load existing _manifest.json.
  3. UNION the new entries on top of the existing trial dict (existing trial
     entries are preserved; new keys come from cd4dd88f).
  4. Update top-level metadata:
       - n_trials = len(trials)
       - task_versions: add the cd4dd88f snapshot version if missing
       - is_multi_version = True if more than one
       - experiment_ids: list of all experiments seen (legacy `experiment_id`
         field kept for backward compat)
       - additional_imports[]: log of the cd4dd88f import (count + version)
       - note: append a short summary line
  5. Re-upload _manifest.json.
"""
from __future__ import annotations
import os, json, sys, time
import boto3
from botocore.config import Config
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from datetime import datetime, timezone

DEST_BUCKET = "ralphbench-logs"
DEST_REGION = "us-west-2"
SRC_BUCKET  = os.environ['ODDISH_S3_BUCKET']
EXPERIMENT_ID = 'cd4dd88f'

def make_src():
    return boto3.client('s3',
        endpoint_url=os.environ['ODDISH_S3_ENDPOINT_URL'],
        aws_access_key_id=os.environ['ODDISH_S3_ACCESS_KEY'],
        aws_secret_access_key=os.environ['ODDISH_S3_SECRET_KEY'],
        region_name='us-east-1',
        config=Config(max_pool_connections=32, retries={'max_attempts':5,'mode':'standard'}))

def make_dst():
    return boto3.client('s3',
        aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'],
        region_name=DEST_REGION,
        config=Config(max_pool_connections=32, retries={'max_attempts':5,'mode':'standard'}))

def list_keys(src, prefix):
    out = []
    for page in src.get_paginator('list_objects_v2').paginate(Bucket=SRC_BUCKET, Prefix=prefix):
        for obj in page.get('Contents', []):
            out.append((obj['Key'], obj['Size']))
    return out

def pick_canonical(src, src_prefix, keys):
    subdirs = sorted({k[len(src_prefix):].split('/',1)[0]
                      for k,_ in keys if '/' in k[len(src_prefix):]
                      and k[len(src_prefix):].startswith('task-')})
    if not subdirs:
        return None
    if len(subdirs) == 1:
        return subdirs[0]
    rewards = {}
    in_exc = set()
    for k,_ in keys:
        rel = k[len(src_prefix):]
        if rel == 'result.json':
            res = json.loads(src.get_object(Bucket=SRC_BUCKET, Key=k)['Body'].read())
            for ev in (res.get('stats',{}).get('evals',{}) or {}).values():
                for rv, names in (ev.get('reward_stats') or {}).get('reward', {}).items():
                    try: r = float(rv)
                    except Exception: continue
                    for n in names or []:
                        if n in subdirs: rewards[n] = max(rewards.get(n, r), r)
                for cls, names in (ev.get('exception_stats') or {}).items():
                    for n in names or []: in_exc.add(n)
            break
    if rewards:
        return sorted(rewards.items(),
            key=lambda kv: (-(kv[1]), 1 if kv[0] in in_exc else 0, kv[0]))[0][0]
    return subdirs[-1]

def transform_top(src_bytes, task_name, task_hash, task_version_id):
    data = json.loads(src_bytes)
    data['task_name'] = task_name
    data['task_hash'] = task_hash
    data['task_version_id'] = task_version_id
    return json.dumps(data, indent=2).encode('utf-8')

def upload_one_trial(src, dst, sel_meta, trial_meta, dst_task_name):
    tid = trial_meta['id']
    src_prefix = trial_meta.get('trial_s3_key') or f"tasks/{sel_meta['task_id']}/trials/{tid}/"
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
        else:
            if rel.split('/',1)[0] == canonical:
                to_copy.append((k, sz, dst_prefix + rel, rel))
    def _proc(item):
        sk, sz, dk, rel = item
        body = src.get_object(Bucket=SRC_BUCKET, Key=sk)['Body'].read()
        if rel in ('config.json','result.json') and '/' not in rel:
            body = transform_top(body, sel_meta['task_name'], sel_meta['task_hash'], sel_meta['task_version_id'])
        dst.put_object(Bucket=DEST_BUCKET, Key=dk, Body=body)
        return len(body)
    total = 0
    with ThreadPoolExecutor(max_workers=16) as pool:
        for fut in as_completed([pool.submit(_proc, it) for it in to_copy]):
            total += fut.result()
    return canonical, total

def build_entry(trial_meta, sel_meta, canonical, dst_task_name):
    return {
        'agent': trial_meta['agent'],
        'model': trial_meta['model'],
        'provider': trial_meta['provider'],
        'reward': trial_meta['reward'],
        'status': trial_meta['status'],
        'attempts': trial_meta['attempts'],
        'max_attempts': trial_meta['max_attempts'],
        'task_version_id': sel_meta['task_version_id'],
        'started_at': trial_meta['started_at'],
        'finished_at': trial_meta['finished_at'],
        'input_tokens': trial_meta.get('input_tokens'),
        'cache_tokens': trial_meta.get('cache_tokens'),
        'output_tokens': trial_meta.get('output_tokens'),
        'cost_usd': trial_meta.get('cost_usd'),
        'has_trajectory': trial_meta.get('has_trajectory'),
        'trajectory_bytes': trial_meta.get('trajectory_bytes', trial_meta.get('trajectory_bytes_max', 0)),
        'exception_classes': trial_meta.get('exception_classes', []),
        'origin': 'oddish',
        'artifact_layout': 'harbor-nested',
        'artifact_prefix': f"s3://{DEST_BUCKET}/{dst_task_name}/{trial_meta['id']}/",
        'source_trial_name': trial_meta.get('source_trial_name'),
        'source_oddish_task_id': sel_meta['task_id'],
        'source_oddish_trial_id': trial_meta['id'],
        'source_oddish_experiment_id': EXPERIMENT_ID,
        'selection_note': trial_meta.get('selection_note', ''),
        'canonical_attempt_dir': canonical,
        'task_name': sel_meta['task_name'],
        'task_hash': sel_meta['task_hash'],
    }

def main():
    only_task = sys.argv[1] if len(sys.argv) > 1 else None
    sel = json.load(open('/tmp/oddish-import/selection_cd4dd88f.json'))
    src = make_src(); dst = make_dst()

    state_path = '/tmp/oddish-import/state_cd4dd88f.json'
    if os.path.exists(state_path):
        state = json.load(open(state_path))
    else:
        state = {'tasks': {}}

    for task_id, t in sel.items():
        if only_task and task_id != only_task:
            continue
        task_name = t['task_name']
        sel_meta = {'task_id': task_id, 'task_name': task_name,
                    'task_hash': t['task_hash'], 'task_version_id': t['task_version_id']}
        picks_in_order = []
        for k, v in t['selection']['pairs'].items():
            for pid in v['picks']:
                for c in t['by_pair'][k]:
                    if c['id'] == pid:
                        picks_in_order.append(c)
                        break
        print(f"\n=== {task_id} -> {task_name} (uploading {len(picks_in_order)} trials)")
        ts = state['tasks'].setdefault(task_id, {'task_name': task_name, 'task_hash': t['task_hash'],
                                                 'task_version_id': t['task_version_id'],
                                                 'new_entries': {}, 'failures': []})
        for i, c in enumerate(picks_in_order, 1):
            tid = c['id']
            if tid in ts['new_entries']:
                print(f"  [{i}/{len(picks_in_order)}] {tid}: ALREADY uploaded — skipping")
                continue
            t0 = time.time()
            try:
                canonical, nb = upload_one_trial(src, dst, sel_meta, c, task_name)
                if canonical is None:
                    print(f"  [{i}/{len(picks_in_order)}] {tid}: FAILED — no keys at source")
                    ts['failures'].append({'trial_id': tid, 'reason': 'no keys'})
                    continue
                print(f"  [{i}/{len(picks_in_order)}] {tid}: uploaded {nb/1024/1024:.1f} MB in {time.time()-t0:.1f}s; canonical={canonical}")
                ts['new_entries'][tid] = build_entry(c, sel_meta, canonical, task_name)
            except Exception as e:
                print(f"  [{i}/{len(picks_in_order)}] {tid}: FAILED — {e}")
                ts['failures'].append({'trial_id': tid, 'reason': str(e)})
            with open(state_path, 'w') as f:
                json.dump(state, f, indent=2)

        # Augment existing manifest
        try:
            cur_manifest = json.loads(dst.get_object(Bucket=DEST_BUCKET, Key=f"{task_name}/_manifest.json")['Body'].read())
        except Exception:
            cur_manifest = None
        if cur_manifest is None:
            cur_manifest = {
                'experiment_id': EXPERIMENT_ID, 'n_trials': 0, 'note': '',
                'task': task_id, 'task_name': task_name, 'task_hash': t['task_hash'],
                'task_version_id': t['task_version_id'], 'trials': {},
            }
        # Union trials
        existing_trials = cur_manifest.get('trials', {}) or {}
        merged_trials = dict(existing_trials)
        added = 0
        for tid, entry in ts['new_entries'].items():
            if tid in merged_trials:
                # Already present (different experiment perhaps). Overwrite with new entry.
                pass
            merged_trials[tid] = entry
            added += 1
        # Sort by trial index (consistent with prior practice)
        def _idx(tid):
            tail = tid.rsplit('-',1)[-1]
            return (int(tail) if tail.isdigit() else 1<<30, tid)
        merged_trials = dict(sorted(merged_trials.items(), key=lambda kv: _idx(kv[0])))
        # Update top-level metadata
        existing_exp_ids = cur_manifest.get('experiment_ids') or [cur_manifest.get('experiment_id')]
        existing_exp_ids = [e for e in existing_exp_ids if e]
        if EXPERIMENT_ID not in existing_exp_ids:
            existing_exp_ids.append(EXPERIMENT_ID)
        existing_versions = list(cur_manifest.get('task_versions') or [])
        if cur_manifest.get('task_version_id') and cur_manifest['task_version_id'] not in existing_versions:
            existing_versions.append(cur_manifest['task_version_id'])
        if t['task_version_id'] not in existing_versions:
            existing_versions.append(t['task_version_id'])
        existing_hashes = list(cur_manifest.get('task_hashes') or [])
        if t['task_hash'] not in existing_hashes:
            existing_hashes.append(t['task_hash'])
        n_added = len([tid for tid in ts['new_entries'] if tid not in existing_trials])
        additional = cur_manifest.get('additional_imports') or []
        additional.append({
            'imported_at': datetime.now(timezone.utc).isoformat(),
            'source_oddish_experiment_id': EXPERIMENT_ID,
            'task_version_id_at_snapshot': t['task_version_id'],
            'n_trials_added': n_added,
            'pairs_added': sorted({(e['agent'], e['model']) for e in ts['new_entries'].values()},
                                  key=lambda p:(p[0],p[1])),
            'note': ('Augmenting (Option A): add up to 5 trials per pair for the new '
                     'agent/model pairs from this experiment. nop/oracle excluded; for '
                     'nextjs-vite-rewrite, claude-code is also excluded.'),
        })
        cur_manifest['experiment_ids'] = existing_exp_ids
        cur_manifest['task_versions'] = existing_versions
        cur_manifest['task_hashes'] = existing_hashes
        cur_manifest['is_multi_version'] = len(existing_versions) > 1
        cur_manifest['trials'] = merged_trials
        cur_manifest['n_trials'] = len(merged_trials)
        cur_manifest['additional_imports'] = additional
        # Append to note
        existing_note = cur_manifest.get('note') or ''
        append = (
            f" | {datetime.now(timezone.utc).isoformat()} appended {n_added} trials from "
            f"Oddish experiment {EXPERIMENT_ID} on task_version {t['task_version_id']} "
            f"(pairs: claude-code/anthropic/claude-opus-4-8"
            f"{' (skipped for nextjs-vite-rewrite)' if task_name=='nextjs-vite-rewrite' else ''}, "
            f"gemini-cli/gemini/gemini-3.5-flash); strict validity filter (status=SUCCESS, "
            f"numeric reward, exception_stats clean or AgentTimeoutError/VerifierTimeoutError)."
        )
        cur_manifest['note'] = (existing_note + append).strip()

        body = json.dumps(cur_manifest, indent=2, default=str).encode('utf-8')
        dst.put_object(Bucket=DEST_BUCKET, Key=f"{task_name}/_manifest.json", Body=body,
                       ContentType='application/json')
        # Local copy
        with open(f"/tmp/oddish-import/{task_name}_manifest.json", 'w') as f:
            f.write(body.decode())
        # Summarize pairs
        pair_counts = Counter((e['agent'], e['model']) for e in merged_trials.values())
        print(f"  Updated _manifest.json: n_trials={cur_manifest['n_trials']} (+{n_added}); "
              f"experiment_ids={existing_exp_ids}; is_multi_version={cur_manifest['is_multi_version']}")
        for pair, n in sorted(pair_counts.items()):
            print(f"    {pair[0]:12s} / {pair[1]:40s}  n={n}")

if __name__ == '__main__':
    main()
