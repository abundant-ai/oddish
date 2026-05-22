"""Reconcile s3://ralphbench-logs/<task>/ with selection_strict.json.

For each task:
  1. Load existing manifest (current ralphbench state).
  2. Diff against strict selection (latest task_version, AgentTimeoutError
     /VerifierTimeoutError only).
  3. ADD missing trials by streaming them from Oddish (oddish-source S3) to
     ralphbench-logs.
  4. REMOVE trials no longer selected (their entire prefix in ralphbench-logs).
  5. Rewrite _manifest.json.

Uses the same canonical-attempt logic and config/result injection as
upload.py.
"""
from __future__ import annotations
import os, json, sys, time
import boto3
from botocore.config import Config
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter, defaultdict
from datetime import datetime, timezone

DEST_BUCKET = "ralphbench-logs"
DEST_REGION = "us-west-2"
SRC_BUCKET  = os.environ['ODDISH_S3_BUCKET']

def make_src_client():
    return boto3.client('s3',
        endpoint_url=os.environ['ODDISH_S3_ENDPOINT_URL'],
        aws_access_key_id=os.environ['ODDISH_S3_ACCESS_KEY'],
        aws_secret_access_key=os.environ['ODDISH_S3_SECRET_KEY'],
        region_name='us-east-1',
        config=Config(max_pool_connections=32, retries={'max_attempts':5,'mode':'standard'}))

def make_dst_client():
    return boto3.client('s3',
        aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'],
        region_name=DEST_REGION,
        config=Config(max_pool_connections=32, retries={'max_attempts':5,'mode':'standard'}))

# ----- Copied/adapted from upload.py for canonical attempt selection -----

def list_trial_keys(src, prefix):
    out = []
    for page in src.get_paginator('list_objects_v2').paginate(Bucket=SRC_BUCKET, Prefix=prefix):
        for obj in page.get('Contents', []):
            out.append((obj['Key'], obj['Size']))
    return out

def pick_canonical(src, src_prefix, keys):
    subdirs = sorted({k[len(src_prefix):].split('/',1)[0]
                      for k,_ in keys if '/' in k[len(src_prefix):] and k[len(src_prefix):].startswith('task-')})
    if not subdirs:
        return None
    if len(subdirs) == 1:
        return subdirs[0]
    # Top-level result.json -> map subdir -> (reward, in_exc)
    rewards = {}
    in_exc = set()
    for k, _ in keys:
        rel = k[len(src_prefix):]
        if rel == 'result.json':
            obj = src.get_object(Bucket=SRC_BUCKET, Key=k)
            top = json.loads(obj['Body'].read())
            for ev in (top.get('stats',{}).get('evals',{}) or {}).values():
                rs = (ev.get('reward_stats') or {}).get('reward') or {}
                for rv, names in rs.items():
                    try: r = float(rv)
                    except Exception: continue
                    for n in names or []:
                        if n in subdirs:
                            rewards[n] = max(rewards.get(n, r), r)
                for cls, names in (ev.get('exception_stats') or {}).items():
                    for n in names or []:
                        in_exc.add(n)
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
    keys = list_trial_keys(src, src_prefix)
    if not keys:
        return None, 'no keys at source'
    canonical = pick_canonical(src, src_prefix, keys)
    to_copy = []
    for k, sz in keys:
        rel = k[len(src_prefix):]
        if '/' not in rel:
            to_copy.append((k, sz, dst_prefix + rel, rel))
        else:
            sub = rel.split('/',1)[0]
            if sub == canonical:
                to_copy.append((k, sz, dst_prefix + rel, rel))
    def _proc(item):
        sk, sz, dk, rel = item
        if rel in ('config.json', 'result.json') and '/' not in rel:
            body = src.get_object(Bucket=SRC_BUCKET, Key=sk)['Body'].read()
            body = transform_top(body, sel_meta['task_name'], sel_meta['task_hash'], sel_meta['task_version_id'])
            dst.put_object(Bucket=DEST_BUCKET, Key=dk, Body=body)
            return len(body)
        else:
            body = src.get_object(Bucket=SRC_BUCKET, Key=sk)['Body'].read()
            dst.put_object(Bucket=DEST_BUCKET, Key=dk, Body=body)
            return len(body)
    nb = 0
    with ThreadPoolExecutor(max_workers=16) as pool:
        for fut in as_completed([pool.submit(_proc, it) for it in to_copy]):
            nb += fut.result()
    return canonical, nb

def delete_prefix(dst, prefix):
    paginator = dst.get_paginator('list_objects_v2')
    pending = []
    n = 0
    for page in paginator.paginate(Bucket=DEST_BUCKET, Prefix=prefix):
        for obj in page.get('Contents', []):
            pending.append({'Key': obj['Key']})
            if len(pending) >= 1000:
                dst.delete_objects(Bucket=DEST_BUCKET, Delete={'Objects': pending, 'Quiet': True})
                n += len(pending)
                pending = []
    if pending:
        dst.delete_objects(Bucket=DEST_BUCKET, Delete={'Objects': pending, 'Quiet': True})
        n += len(pending)
    return n

# ----- Manifest assembly -----

def build_manifest_entry(trial_meta, sel_meta, canonical, dst_task_name):
    e = {
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
        'source_oddish_experiment_id': 'cd8c33d8',
        'selection_note': trial_meta.get('selection_note', ''),
        'canonical_attempt_dir': canonical,
    }
    return e

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

def main():
    only_task = sys.argv[1] if len(sys.argv) > 1 else None
    sel = json.load(open('/tmp/oddish-import/selection_strict.json'))
    src = make_src_client()
    dst = make_dst_client()

    for task_id, t in sel.items():
        if only_task and task_id != only_task:
            continue
        task_name = t['task_name']
        sel_meta = {'task_id': task_id, 'task_name': task_name,
                    'task_hash': t['task_hash'], 'task_version_id': t['task_version_id']}
        # Map trial_id -> trial_meta (only for picked trials)
        picked_meta = {}
        for k, v in t['selection']['pairs'].items():
            for pid in v['picks']:
                for c in t['by_pair'][k]:
                    if c['id'] == pid:
                        picked_meta[pid] = c
                        break
        new_picks = set(picked_meta.keys())

        # Load existing manifest
        try:
            cur_manifest = json.loads(dst.get_object(Bucket=DEST_BUCKET, Key=f"{task_name}/_manifest.json")['Body'].read())
            old_picks = set(cur_manifest['trials'].keys())
            cur_entries = cur_manifest['trials']
        except Exception:
            old_picks = set(); cur_entries = {}

        add = sorted(new_picks - old_picks)
        remove = sorted(old_picks - new_picks)
        keep = sorted(old_picks & new_picks)
        print(f"\n=== {task_id} -> {task_name} ===")
        print(f"  KEEP {len(keep)}, ADD {len(add)}, REMOVE {len(remove)}")

        # --- ADD ---
        new_entries = {}
        # carry forward KEEP entries (already in ralphbench) — but refresh
        # them with new fields (exception_classes, selection_note, task_version_id)
        for tid in keep:
            base = cur_entries.get(tid, {})
            # We have new metadata for keep trials too (they're in picked_meta)
            new_entries[tid] = build_manifest_entry(picked_meta[tid], sel_meta,
                                                   base.get('canonical_attempt_dir'), task_name)

        for i, tid in enumerate(add, 1):
            t0 = time.time()
            try:
                canonical, nb = upload_one_trial(src, dst, sel_meta, picked_meta[tid], task_name)
                if canonical is None:
                    print(f"  [{i}/{len(add)}] {tid}: FAILED — {nb}")
                    continue
                print(f"  [{i}/{len(add)}] {tid}: uploaded {(nb or 0)/1024/1024:.1f} MB in {time.time()-t0:.1f}s; canonical={canonical}")
                new_entries[tid] = build_manifest_entry(picked_meta[tid], sel_meta, canonical, task_name)
            except Exception as e:
                print(f"  [{i}/{len(add)}] {tid}: FAILED — {e}")

        # --- REMOVE ---
        for i, tid in enumerate(remove, 1):
            prefix = f"{task_name}/{tid}/"
            n = delete_prefix(dst, prefix)
            print(f"  [{i}/{len(remove)}] REMOVE {tid}: deleted {n} keys")

        # --- Validate every kept/added trial ---
        # Refresh canonical_attempt_dir for KEEP trials from S3 if missing
        for tid, e in new_entries.items():
            if e.get('canonical_attempt_dir'):
                continue
            paginator = dst.get_paginator('list_objects_v2')
            subs = set()
            for page in paginator.paginate(Bucket=DEST_BUCKET, Prefix=f"{task_name}/{tid}/", Delimiter='/'):
                for p in page.get('CommonPrefixes', []):
                    sub = p['Prefix'].split('/')[-2]
                    if sub.startswith('task-'):
                        subs.add(sub)
            if len(subs) == 1:
                e['canonical_attempt_dir'] = sorted(subs)[0]

        # --- Build & upload new manifest ---
        pair_counts = Counter((e['agent'], e['model']) for e in new_entries.values())
        n_pass = sum(1 for e in new_entries.values() if e.get('reward') == 1.0)
        seen_fp = {}; dup = 0
        for tid, e in new_entries.items():
            fp = (e['agent'], e['model'], e['reward'], e['started_at'], e['finished_at'], e.get('source_trial_name'))
            if fp in seen_fp: dup += 1
            else: seen_fp[fp] = tid
        notes = []
        if dup: notes.append(f"duplicate fingerprint count: {dup}")
        for pair in EXPECTED_PAIRS:
            c = pair_counts.get(pair, 0)
            if c < 5:
                notes.append(f"shortfall {pair[0]}|{pair[1]}: {c}/5 — limited by v={t['task_version_id']} valid trials (excludes NonZeroAgentExitCodeError and other infra failures)")
        note = (
            f"Re-generated at {datetime.now(timezone.utc).isoformat()} from Oddish experiment "
            f"cd8c33d8 on task version {t['task_version_id']}. Strict selection: up to 5 valid "
            f"trials per agent/model pair, where valid = SUCCESS + numeric reward AND "
            f"top-level result.json exception_stats is empty or limited to "
            f"{{AgentTimeoutError, VerifierTimeoutError}}. Ranked by reward then "
            f"trajectory_bytes then total artifact bytes then recency."
        )
        if notes: note += " Notes: " + "; ".join(notes) + "."

        # Sort by trial index
        sorted_trials = dict(sorted(new_entries.items(),
            key=lambda kv: (int(kv[0].rsplit('-',1)[-1]) if kv[0].rsplit('-',1)[-1].isdigit() else 0, kv[0])))
        manifest = {
            'experiment_id': 'cd8c33d8',
            'n_trials': len(sorted_trials),
            'note': note,
            'task': task_id,
            'task_name': task_name,
            'task_hash': t['task_hash'],
            'task_version_id': t['task_version_id'],
            'pair_counts': {f"{a}|{m}": pair_counts.get((a,m),0) for (a,m) in EXPECTED_PAIRS},
            'n_pass': n_pass,
            'duplicate_fingerprint_count': dup,
            'trials': sorted_trials,
        }
        body = json.dumps(manifest, indent=2, default=str).encode('utf-8')
        dst.put_object(Bucket=DEST_BUCKET, Key=f"{task_name}/_manifest.json", Body=body,
                       ContentType='application/json')
        print(f"  Uploaded _manifest.json ({len(body)} bytes; n_trials={len(sorted_trials)}, n_pass={n_pass}, dup={dup})")
        for pair in EXPECTED_PAIRS:
            c = pair_counts.get(pair, 0)
            mark = 'OK' if c == 5 else f"GAP {c}/5"
            print(f"    {pair[0]:12s} / {pair[1]:40s}  {c}  {mark}")
        # Local copy for traceability
        with open(f'/tmp/oddish-import/{task_name}_manifest.json', 'w') as f:
            f.write(body.decode())

if __name__ == '__main__':
    main()
