"""Stream-upload selected trials from Oddish S3 to ralphbench-logs S3."""
import os, json, sys, time, io
import boto3
from botocore.config import Config
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

DEST_BUCKET = "ralphbench-logs"
DEST_REGION = "us-west-2"

def make_src_client():
    return boto3.client('s3',
        endpoint_url=os.environ['ODDISH_S3_ENDPOINT_URL'],
        aws_access_key_id=os.environ['ODDISH_S3_ACCESS_KEY'],
        aws_secret_access_key=os.environ['ODDISH_S3_SECRET_KEY'],
        region_name='us-east-1',
        config=Config(max_pool_connections=32, retries={'max_attempts':5,'mode':'standard'}))

def make_dst_client():
    return boto3.client('s3',
        aws_access_key_id='AKIAQKPIMBRVS4B37MNG',
        aws_secret_access_key='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',
        region_name=DEST_REGION,
        config=Config(max_pool_connections=32, retries={'max_attempts':5,'mode':'standard'}))

SRC_BUCKET = os.environ['ODDISH_S3_BUCKET']

def list_trial_keys(src, prefix):
    """List all keys under source prefix; return list of (key, size)."""
    out = []
    paginator = src.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=SRC_BUCKET, Prefix=prefix):
        for obj in page.get('Contents', []):
            out.append((obj['Key'], obj['Size']))
    return out

def pick_canonical(src, src_prefix, keys):
    """Choose canonical task-* attempt subdir.
    Priority:
      1) Subdir present in top-level result.json reward_stats with a numeric reward (prefer max),
         break ties by absence from exception_stats.
      2) Subdir with result.json having no exception_info and reward (or non-null).
      3) Subdir with result.json (latest by name).
    """
    # All subdir names
    subdirs = set()
    for k, _ in keys:
        rel = k[len(src_prefix):]
        if '/' in rel:
            sub = rel.split('/',1)[0]
            if sub.startswith('task-'):
                subdirs.add(sub)
    subdirs = sorted(subdirs)
    if not subdirs:
        return None
    if len(subdirs) == 1:
        return subdirs[0]

    # Read top-level result.json
    top = None
    for k, _ in keys:
        rel = k[len(src_prefix):]
        if rel == 'result.json':
            try:
                obj = src.get_object(Bucket=SRC_BUCKET, Key=k)
                top = json.loads(obj['Body'].read())
            except Exception:
                top = None
            break

    # Map subdir -> (reward, in_exception_stats)
    rewards = {}
    in_exc = set()
    if isinstance(top, dict):
        stats = top.get('stats') or {}
        evals = stats.get('evals') or {}
        for ev_name, ev in evals.items():
            if not isinstance(ev, dict): continue
            rs = (ev.get('reward_stats') or {}).get('reward') or {}
            for rv, names in rs.items():
                try:
                    r = float(rv)
                except Exception:
                    continue
                for n in names or []:
                    if n in subdirs:
                        rewards[n] = max(rewards.get(n, r), r)
            ex = ev.get('exception_stats') or {}
            for _name, names in ex.items():
                for n in names or []:
                    in_exc.add(n)
    if rewards:
        # max reward, then prefer not in_exc
        ranked = sorted(rewards.items(),
                        key=lambda kv: (-(kv[1]),
                                        1 if kv[0] in in_exc else 0,
                                        kv[0]))
        return ranked[0][0]

    # Fallback: read each subdir's result.json
    keys_idx = {k: sz for k,sz in keys}
    candidates = []
    for sub in subdirs:
        rk = src_prefix + sub + '/result.json'
        if rk in keys_idx:
            try:
                obj = src.get_object(Bucket=SRC_BUCKET, Key=rk)
                res = json.loads(obj['Body'].read())
                exc = res.get('exception_info')
                rwd = res.get('reward')
                candidates.append((sub, exc, rwd))
            except Exception:
                candidates.append((sub, 'unknown', None))
    if candidates:
        # prefer no exception_info + numeric reward
        ok = [c for c in candidates if not c[1] and c[2] is not None]
        if ok:
            return ok[0][0]
        any_num = [c for c in candidates if c[2] is not None]
        if any_num:
            return any_num[0][0]
        # latest by name
        return candidates[-1][0]
    return subdirs[-1]

def copy_key(src, dst, src_key, dst_key, size_hint=None):
    """Download from src, upload to dst. Preserves bytes exactly."""
    obj = src.get_object(Bucket=SRC_BUCKET, Key=src_key)
    body = obj['Body'].read()
    dst.put_object(Bucket=DEST_BUCKET, Key=dst_key, Body=body)
    return len(body)

def transform_top_config(src_bytes, task_name, task_hash, task_version_id):
    data = json.loads(src_bytes)
    data['task_name'] = task_name
    data['task_hash'] = task_hash
    data['task_version_id'] = task_version_id
    return json.dumps(data, indent=2).encode('utf-8')

def upload_one_trial(src, dst, sel_meta, trial_meta, dst_task_name, manifest_state, log=print):
    """Upload one trial. Append to manifest_state['trials']."""
    tid = trial_meta['id']
    src_prefix = trial_meta['trial_s3_key'] or f"tasks/{sel_meta['task_id']}/trials/{tid}/"
    if not src_prefix.endswith('/'):
        src_prefix += '/'
    dst_prefix = f"{dst_task_name}/{tid}/"

    keys = list_trial_keys(src, src_prefix)
    if not keys:
        log(f"  [{tid}] NO KEYS under {src_prefix}; SKIP")
        return False

    canonical = pick_canonical(src, src_prefix, keys)
    log(f"  [{tid}] canonical={canonical}  n_keys={len(keys)}")

    # Filter keys to copy: top-level files + canonical subdir files
    to_copy = []
    for k, sz in keys:
        rel = k[len(src_prefix):]
        if '/' not in rel:
            to_copy.append((k, sz, dst_prefix + rel, rel))
        else:
            sub = rel.split('/',1)[0]
            if sub == canonical:
                to_copy.append((k, sz, dst_prefix + rel, rel))
            # else skip non-canonical attempts

    # Determine if top-level config/result are present; if so they need transform
    top_keys = {rel for (_k,_s,_d,rel) in to_copy if '/' not in rel}
    has_top_cfg = 'config.json' in top_keys
    has_top_res = 'result.json' in top_keys

    # Transform top-level config.json and result.json
    def _process(item):
        sk, sz, dk, rel = item
        if rel in ('config.json','result.json') and '/' not in rel:
            obj = src.get_object(Bucket=SRC_BUCKET, Key=sk)
            body = obj['Body'].read()
            body = transform_top_config(body, sel_meta['task_name'], sel_meta['task_hash'], sel_meta['task_version_id'])
            dst.put_object(Bucket=DEST_BUCKET, Key=dk, Body=body)
            return ('xform', len(body))
        else:
            obj = src.get_object(Bucket=SRC_BUCKET, Key=sk)
            body = obj['Body'].read()
            dst.put_object(Bucket=DEST_BUCKET, Key=dk, Body=body)
            return ('copy', len(body))

    total_bytes = 0
    n_xform = n_copy = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=16) as pool:
        futs = [pool.submit(_process, it) for it in to_copy]
        for fut in as_completed(futs):
            kind, nb = fut.result()
            total_bytes += nb
            if kind == 'xform': n_xform += 1
            else: n_copy += 1
    dt = time.time() - t0
    log(f"  [{tid}] uploaded {n_xform+n_copy} files ({total_bytes/1024/1024:.1f} MB) in {dt:.1f}s")
    if not has_top_cfg:
        log(f"  [{tid}] WARNING: source has no top-level config.json")
    if not has_top_res:
        log(f"  [{tid}] WARNING: source has no top-level result.json")

    # Build manifest entry
    entry = {
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
        'input_tokens': trial_meta['input_tokens'],
        'cache_tokens': trial_meta['cache_tokens'],
        'output_tokens': trial_meta['output_tokens'],
        'cost_usd': trial_meta['cost_usd'],
        'has_trajectory': trial_meta['has_trajectory'],
        'trajectory_bytes': trial_meta.get('trajectory_bytes', trial_meta.get('trajectory_bytes_max', 0)),
        'origin': 'oddish',
        'artifact_layout': 'harbor-nested',
        'artifact_prefix': f"s3://{DEST_BUCKET}/{dst_prefix}",
        'source_trial_name': trial_meta['source_trial_name'],
        'source_oddish_task_id': sel_meta['task_id'],
        'source_oddish_trial_id': tid,
        'source_oddish_experiment_id': 'cd8c33d8',
        'selection_note': trial_meta.get('selection_note', ''),
        'canonical_attempt_dir': canonical,
    }
    manifest_state['trials'][tid] = entry
    return True

def main():
    only_task = sys.argv[1] if len(sys.argv) > 1 else None
    only_trial = sys.argv[2] if len(sys.argv) > 2 else None
    sel_all = json.load(open('/tmp/oddish-import/selection.json'))
    src = make_src_client()
    dst = make_dst_client()
    
    state_path = '/tmp/oddish-import/state.json'
    if os.path.exists(state_path):
        state = json.load(open(state_path))
    else:
        state = {'tasks': {}}
    
    for task_id, t in sel_all.items():
        if only_task and task_id != only_task:
            continue
        dst_task_name = t['task_name']
        sel_meta = {
            'task_id': task_id,
            'task_name': t['task_name'],
            'task_hash': t['task_hash'],
            'task_version_id': t['task_version_id'],
        }
        ts = state['tasks'].setdefault(task_id, {'trials': {}, 'failures': []})
        ts['task_version_id'] = t['task_version_id']
        ts['task_name'] = t['task_name']
        ts['task_hash'] = t['task_hash']
        # Build flat list of picks with full metadata
        picks_in_order = []
        for k, v in t['selection']['pairs'].items():
            for pid in v['picks']:
                for c in t['by_pair'][k]:
                    if c['id'] == pid:
                        picks_in_order.append(c)
                        break
        print(f"=== {task_id} -> {dst_task_name} ({len(picks_in_order)} trials)")
        for i, c in enumerate(picks_in_order, 1):
            tid = c['id']
            if only_trial and tid != only_trial:
                continue
            if tid in ts['trials']:
                print(f"  [{i}/{len(picks_in_order)}] {tid}: ALREADY UPLOADED — skipping")
                continue
            print(f"  [{i}/{len(picks_in_order)}] {tid}: starting")
            try:
                ok = upload_one_trial(src, dst, sel_meta, c, dst_task_name, ts)
                if not ok:
                    ts['failures'].append({'trial_id': tid, 'reason':'no keys'})
            except Exception as e:
                print(f"  [{tid}] FAILED: {e}")
                ts['failures'].append({'trial_id': tid, 'reason': str(e)})
            # Save state after each trial
            with open(state_path,'w') as f:
                json.dump(state, f, indent=2)
    print('DONE')

if __name__ == '__main__':
    main()
