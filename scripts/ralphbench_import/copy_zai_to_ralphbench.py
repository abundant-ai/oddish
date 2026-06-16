"""Copy all 100 glm-claude-code trials from s3://zai-swe-marathon/ into
s3://ralphbench-logs/ now that the GLM 5.2 model is public.

Per user instruction:
  - Rename model in the manifest entry from `zai/glm-x-preview[1m]` to
    `zai/glm-5.2[1m]` (raw_model preserved for forensic traceability).
  - Keep formatting identical to existing ralphbench entries.
  - Merge into existing per-task manifests (do not overwrite existing
    trials from terminus-2/glm-5.1, claude-code/opus, etc.).

Per-trial copy plan:
  - Server-side `copy_object` across buckets where possible (faster).
  - For top-level config.json + result.json: download → patch model field
    → upload (so the artifact JSON matches the manifest's new model name).
  - Other files (canonical attempt artifacts, audit JSONs) copy untouched.
  - Manifest entry updated: model→ zai/glm-5.2[1m]; raw_model preserved;
    artifact_prefix bumped to s3://ralphbench-logs/...; original
    artifact_prefix saved as zai_artifact_prefix for cross-reference.
"""
from __future__ import annotations
import os, json, sys, time
import boto3
from botocore.exceptions import ClientError
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from datetime import datetime, timezone
from botocore.config import Config

SRC_BUCKET = 'zai-swe-marathon'
DST_BUCKET = 'ralphbench-logs'
REGION = 'us-west-2'

OLD_MODEL = 'zai/glm-x-preview[1m]'
NEW_MODEL = 'zai/glm-5.2[1m]'

# Tasks to skip (not real task prefixes)
SKIP_PREFIXES = {'reward-hacking'}


def make_src():
    sess = boto3.session.Session(profile_name='zai', region_name=REGION)
    return sess.client('s3', config=Config(max_pool_connections=32,
                                            retries={'max_attempts':5,'mode':'standard'}))

def make_dst():
    return boto3.client('s3', region_name=REGION,
        config=Config(max_pool_connections=32,
                      retries={'max_attempts':5,'mode':'standard'}))

JSON_TOP_FILES = {'config.json', 'result.json'}


def patch_top_json(body: bytes) -> bytes:
    """Update model_name / model fields in the top-level config/result JSON
    to the new public name."""
    try:
        data = json.loads(body)
    except Exception:
        return body
    # config.json has `agents: [{model_name: ...}]`
    if isinstance(data.get('agents'), list):
        for a in data['agents']:
            if a.get('model_name') == OLD_MODEL:
                a['model_name'] = NEW_MODEL
            # env vars sometimes carry the model name too — leave runtime
            # vars alone; they reference the actual API endpoint config
            # used at runtime which was glm-x-preview.
    # result.json may have `task_version_id`, but typically not model. Some
    # top-level results carry `model` in stats. Sweep through known
    # locations conservatively.
    if data.get('model') == OLD_MODEL:
        data['model'] = NEW_MODEL
    return json.dumps(data, indent=2, default=str).encode('utf-8')


def copy_one_file(src, dst, src_key, dst_key, patch=False):
    """Copy a single key. If patch=True, download → mutate JSON → upload.
    Otherwise stream src.get → dst.put (cross-account safe)."""
    if patch:
        body = src.get_object(Bucket=SRC_BUCKET, Key=src_key)['Body'].read()
        body = patch_top_json(body)
        dst.put_object(Bucket=DST_BUCKET, Key=dst_key, Body=body,
                       ContentType='application/json')
        return len(body)
    body = src.get_object(Bucket=SRC_BUCKET, Key=src_key)['Body'].read()
    dst.put_object(Bucket=DST_BUCKET, Key=dst_key, Body=body)
    return len(body)


def copy_trial_dir(src, dst, task_name, trial_id):
    """Copy all keys under <task>/<trial>/ from zai to ralphbench."""
    src_prefix = f"{task_name}/{trial_id}/"
    dst_prefix = src_prefix  # same relative path
    keys = []
    for page in src.get_paginator('list_objects_v2').paginate(
        Bucket=SRC_BUCKET, Prefix=src_prefix):
        for o in page.get('Contents', []):
            keys.append((o['Key'], o['Size']))
    if not keys:
        return 0, 0
    total = 0; n = 0
    def _proc(k_sz):
        k, sz = k_sz
        rel = k[len(src_prefix):]
        # Patch top-level config.json + result.json (NOT the canonical
        # attempt copies; those carry the original runtime config).
        patch = (rel in JSON_TOP_FILES)
        dst_key = dst_prefix + rel
        return copy_one_file(src, dst, k, dst_key, patch=patch)
    with ThreadPoolExecutor(max_workers=16) as pool:
        futs = [pool.submit(_proc, ks) for ks in keys]
        for fut in as_completed(futs):
            total += fut.result()
            n += 1
    return n, total


def patch_entry(entry, task_name, trial_id):
    """Rename model, preserve raw_model, bump artifact_prefix."""
    e = dict(entry)
    e['model'] = NEW_MODEL
    # raw_model stays as the literal Oddish DB string for traceability
    if 'raw_model' not in e:
        e['raw_model'] = OLD_MODEL
    # Bump artifact_prefix to ralphbench
    e['zai_artifact_prefix'] = e.get('artifact_prefix') or \
        f"s3://{SRC_BUCKET}/{task_name}/{trial_id}/"
    e['artifact_prefix'] = f"s3://{DST_BUCKET}/{task_name}/{trial_id}/"
    return e


def main():
    src = make_src(); dst = make_dst()

    # Discover tasks in zai
    tasks = []
    for page in src.get_paginator('list_objects_v2').paginate(
        Bucket=SRC_BUCKET, Delimiter='/'):
        for p in page.get('CommonPrefixes',[]):
            tn = p['Prefix'].rstrip('/')
            if tn in SKIP_PREFIXES: continue
            tasks.append(tn)

    state_path = '/tmp/oddish-import/state_zai_to_ralph.json'
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    state = {'tasks': {}}
    if os.path.exists(state_path):
        state = json.load(open(state_path))

    grand_n_trials = 0
    grand_bytes = 0
    for task_name in tasks:
        print(f"\n=== {task_name} ===")
        try:
            zai_m = json.loads(src.get_object(Bucket=SRC_BUCKET,
                Key=f'{task_name}/_manifest.json')['Body'].read())
        except Exception as e:
            print(f"  no zai manifest, skipping ({e})")
            continue

        # Existing ralphbench manifest
        try:
            ralph_m = json.loads(dst.get_object(Bucket=DST_BUCKET,
                Key=f'{task_name}/_manifest.json')['Body'].read())
        except dst.exceptions.NoSuchKey:
            ralph_m = None
        except Exception as e:
            if 'NoSuchKey' in str(e) or '404' in str(e):
                ralph_m = None
            else:
                raise

        ts = state['tasks'].setdefault(task_name, {'copied': [], 'failures': []})

        copied_entries = {}
        for trial_id, entry in zai_m['trials'].items():
            if trial_id in ts['copied']:
                print(f"  {trial_id}: already copied, reusing entry from state")
                copied_entries[trial_id] = patch_entry(entry, task_name, trial_id)
                continue
            t0 = time.time()
            try:
                n_files, total_bytes = copy_trial_dir(src, dst, task_name, trial_id)
                if n_files == 0:
                    print(f"  {trial_id}: no source keys, skipping")
                    ts['failures'].append({'trial_id': trial_id, 'reason': 'empty'})
                    continue
                copied_entries[trial_id] = patch_entry(entry, task_name, trial_id)
                ts['copied'].append(trial_id)
                grand_bytes += total_bytes
                grand_n_trials += 1
                print(f"  {trial_id}: {n_files} files / {total_bytes/1024/1024:.1f} MB in {time.time()-t0:.1f}s")
                with open(state_path,'w') as f:
                    json.dump(state, f, indent=2, default=str)
            except Exception as e:
                print(f"  {trial_id}: FAILED — {e}")
                ts['failures'].append({'trial_id': trial_id, 'reason': str(e)})
                with open(state_path,'w') as f:
                    json.dump(state, f, indent=2, default=str)

        if not copied_entries:
            print(f"  no entries copied for {task_name}")
            continue

        # Merge into ralphbench manifest
        if ralph_m is None:
            # Create fresh
            ralph_m = {
                'experiment_id': zai_m.get('experiment_id'),
                'experiment_ids': zai_m.get('experiment_ids') or [zai_m.get('experiment_id')],
                'note': '',
                'task': zai_m.get('task') or task_name,
                'task_name': task_name,
                'task_hash': zai_m.get('task_hash'),
                'task_version_id': zai_m.get('task_version_id'),
                'task_versions': list(zai_m.get('task_versions') or [zai_m.get('task_version_id')]),
                'task_hashes': list(zai_m.get('task_hashes') or [zai_m.get('task_hash')]),
                'trials': {},
                'additional_imports': [],
            }

        existing_trials = ralph_m.get('trials', {}) or {}
        merged = dict(existing_trials)
        merged.update(copied_entries)
        def _idx(t):
            tail = t.rsplit('-',1)[-1]
            return (int(tail) if tail.isdigit() else 1<<30, t)
        merged = dict(sorted(merged.items(), key=lambda kv: _idx(kv[0])))
        ralph_m['trials'] = merged
        ralph_m['n_trials'] = len(merged)
        pc = Counter((e['agent'], e['model']) for e in merged.values())
        ralph_m['pair_counts'] = {f"{a}|{m}": n for (a,m), n in pc.items()}
        ralph_m['n_pass'] = sum(1 for e in merged.values() if e.get('reward') == 1.0)

        # Update task versions / hashes / experiment_ids — UNION with zai
        existing_ev = list(ralph_m.get('experiment_ids') or [])
        for eid in (zai_m.get('experiment_ids') or [zai_m.get('experiment_id')]):
            if eid and eid not in existing_ev:
                existing_ev.append(eid)
        ralph_m['experiment_ids'] = existing_ev
        existing_versions = list(ralph_m.get('task_versions') or [])
        for v in (zai_m.get('task_versions') or [zai_m.get('task_version_id')]):
            if v and v not in existing_versions:
                existing_versions.append(v)
        ralph_m['task_versions'] = existing_versions
        existing_hashes = list(ralph_m.get('task_hashes') or [])
        for h in (zai_m.get('task_hashes') or [zai_m.get('task_hash')]):
            if h and h not in existing_hashes:
                existing_hashes.append(h)
        ralph_m['task_hashes'] = existing_hashes
        ralph_m['is_multi_version'] = len(existing_versions) > 1

        n_added = len([t for t in copied_entries if t not in existing_trials])
        ralph_m.setdefault('additional_imports', []).append({
            'imported_at': datetime.now(timezone.utc).isoformat(),
            'source_oddish_experiment_id': zai_m.get('experiment_id') or
                                            (zai_m.get('experiment_ids') or [None])[0],
            'task_version_id_at_snapshot': zai_m.get('task_version_id'),
            'n_trials_added': n_added,
            'pairs_added': sorted({(e['agent'], NEW_MODEL) for e in copied_entries.values()},
                                  key=lambda p:(p[0],p[1])),
            'note': (f'Bulk import of glm-claude-code trials from '
                     f's3://{SRC_BUCKET}/{task_name}/. Model renamed from '
                     f'{OLD_MODEL} to {NEW_MODEL} per public release of GLM '
                     f'5.2; raw_model preserved on each entry for forensic '
                     f'traceability. All other fields (reward, partial_score, '
                     f'exception_classes, override_validity flags, '
                     f'modification_audit_path, etc.) carried through unchanged.'),
        })
        existing_note = ralph_m.get('note') or ''
        append = (f" | {datetime.now(timezone.utc).isoformat()} imported "
                  f"{n_added} glm-claude-code/{NEW_MODEL} trials from "
                  f"s3://{SRC_BUCKET}/.")
        ralph_m['note'] = (existing_note + append).strip()

        body = json.dumps(ralph_m, indent=2, default=str).encode('utf-8')
        dst.put_object(Bucket=DST_BUCKET, Key=f'{task_name}/_manifest.json',
                       Body=body, ContentType='application/json')
        with open(f'/tmp/oddish-import/{task_name}_ralph_with_glm.json','w') as f:
            f.write(body.decode())
        print(f"  manifest updated: n_trials={ralph_m['n_trials']} (+{n_added}); "
              f"versions={ralph_m['task_versions']}; pairs={sorted(pc.keys())}")

    print(f"\n=== Bulk-copy summary ===")
    print(f"  trials copied: {grand_n_trials}")
    print(f"  total bytes:   {grand_bytes/1024/1024:.1f} MB ({grand_bytes/1024/1024/1024:.2f} GB)")

if __name__ == '__main__':
    main()
