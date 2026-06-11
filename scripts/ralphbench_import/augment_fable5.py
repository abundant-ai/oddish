"""Upload claude-code / claude-fable-5 trials from Oddish experiment
3942355b into ralphbench-logs and augment each task's _manifest.json.

Reads /tmp/oddish-import/fable5_selection.json (produced by the
partial-score-aware survey + canonical-attempt-aware pre-upload audit).

Model normalization (matches existing bucket convention for opus-4-7/4-8):
  global.anthropic.claude-fable-5  →  anthropic/claude-fable-5

For each selected trial:
  1. Stream artifacts from Oddish S3 → ralphbench-logs (top-level files
     + canonical task-* attempt only). pick_canonical is the smart
     picker that reads result.json reward_stats to find the successful
     attempt for multi-attempt trials.
  2. Inject task_name/task_hash/task_version_id into top-level
     config.json + result.json (bucket convention).
  3. UNION the new entries into the existing _manifest.json without
     touching existing entries. Update top-level metadata:
       - n_trials, pair_counts, n_pass
       - task_versions / task_hashes / is_multi_version
       - experiment_ids (cumulative)
       - additional_imports[] (audit log row)
     and append a one-line note.
"""
from __future__ import annotations
import os, sys, json, time
import boto3
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, '/workspace/scripts/ralphbench_import')
from augment_cd4dd88f import (
    make_src, make_dst, list_keys, pick_canonical, transform_top,
)

DEST_BUCKET = 'ralphbench-logs'
DEST_REGION = 'us-west-2'
SRC_BUCKET  = os.environ['ODDISH_S3_BUCKET']
EXPERIMENT_ID = '3942355b'
RAW_MODEL = 'global.anthropic.claude-fable-5'
NORM_MODEL = 'anthropic/claude-fable-5'


def upload_one_trial(src, dst, sel_meta, trial, dst_task_name):
    tid = trial['id']
    src_prefix = trial['prefix']
    if not src_prefix.endswith('/'): src_prefix += '/'
    dst_prefix = f"{dst_task_name}/{tid}/"
    keys = list_keys(src, src_prefix)
    if not keys:
        return None, 0
    canonical = trial.get('canonical') or pick_canonical(src, src_prefix, keys)
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
        dst.put_object(Bucket=DEST_BUCKET, Key=dk, Body=body)
        return len(body)

    total = 0
    with ThreadPoolExecutor(max_workers=16) as pool:
        for fut in as_completed([pool.submit(_proc, it) for it in to_copy]):
            total += fut.result()
    return canonical, total


def build_entry(trial, sel_meta, canonical, dst_task_name):
    e = {
        'agent': 'claude-code',
        'model': NORM_MODEL,
        'provider': trial.get('provider') or 'bedrock',
        'reward': trial['reward'],
        'status': trial['status'],
        'attempts': trial.get('attempts'),
        'max_attempts': trial.get('max_attempts'),
        'task_version_id': sel_meta['task_version_id'],
        'started_at': trial.get('started_at'),
        'finished_at': trial.get('finished_at'),
        'input_tokens': trial.get('input_tokens'),
        'cache_tokens': trial.get('cache_tokens'),
        'output_tokens': trial.get('output_tokens'),
        'cost_usd': trial.get('cost_usd'),
        'has_trajectory': trial.get('has_trajectory'),
        'trajectory_bytes': trial.get('trajectory_bytes') or 0,
        'exception_classes': trial.get('exception_classes', []),
        'origin': 'oddish',
        'artifact_layout': 'harbor-nested',
        'artifact_prefix': f"s3://{DEST_BUCKET}/{dst_task_name}/{trial['id']}/",
        'source_trial_name': trial.get('source_trial_name'),
        'source_oddish_task_id': sel_meta['task_id'],
        'source_oddish_trial_id': trial['id'],
        'source_oddish_experiment_id': EXPERIMENT_ID,
        'canonical_attempt_dir': canonical,
        'task_name': sel_meta['task_name'],
        'task_hash': sel_meta['task_hash'],
        'raw_model': RAW_MODEL,
        'selection_note': (
            f'Imported from Oddish experiment {EXPERIMENT_ID} (preliminary '
            f'results for claude-code + claude-fable-5 via Bedrock). Strict '
            f'validity: SUCCESS + numeric reward + exception_classes ⊆ '
            f'{{AgentTimeoutError}} + latest task version + complete '
            f'verifier artifacts. Ranked by reward=1 first, then '
            f'verifier partial_score desc, then trajectory length desc.'
        ),
    }
    if trial.get('partial_score') is not None:
        e['verifier_partial_score'] = trial['partial_score']
    return e


def main():
    sel = json.load(open('/tmp/oddish-import/fable5_selection.json'))
    src = make_src(); dst = make_dst()

    state_path = '/tmp/oddish-import/state_fable5.json'
    state = {'tasks': {}}
    if os.path.exists(state_path):
        state = json.load(open(state_path))

    for task_name, picks in sorted(sel.items()):
        if not picks:
            continue
        # All picks share task_id/task_version_id
        tid = picks[0]['task_id']
        vid = picks[0]['vid']
        task_hash = tid.rsplit('-', 1)[-1]
        sel_meta = {'task_id': tid, 'task_name': task_name,
                    'task_hash': task_hash, 'task_version_id': vid}

        print(f"\n=== {task_name}  ({len(picks)} picks, version {vid}) ===")
        ts = state['tasks'].setdefault(tid, {
            'task_name': task_name, 'task_hash': task_hash,
            'task_version_id': vid, 'new_entries': {}, 'failures': []})

        for i, c in enumerate(picks, 1):
            ctid = c['id']
            if ctid in ts['new_entries']:
                print(f"  [{i}/{len(picks)}] {ctid}: already uploaded, skipping")
                continue
            t0 = time.time()
            try:
                canonical, nb = upload_one_trial(src, dst, sel_meta, c, task_name)
                if canonical is None:
                    print(f"  [{i}/{len(picks)}] {ctid}: FAILED — no keys at source")
                    ts['failures'].append({'trial_id': ctid, 'reason': 'no keys'})
                    continue
                print(f"  [{i}/{len(picks)}] {ctid}: uploaded {nb/1024/1024:.1f} MB in "
                      f"{time.time()-t0:.1f}s; canonical={canonical}")
                ts['new_entries'][ctid] = build_entry(c, sel_meta, canonical, task_name)
            except Exception as e:
                print(f"  [{i}/{len(picks)}] {ctid}: FAILED — {e}")
                ts['failures'].append({'trial_id': ctid, 'reason': str(e)})
            with open(state_path, 'w') as f:
                json.dump(state, f, indent=2)

        # Augment existing manifest
        try:
            cur_manifest = json.loads(dst.get_object(Bucket=DEST_BUCKET,
                Key=f"{task_name}/_manifest.json")['Body'].read())
        except Exception:
            cur_manifest = None
        if cur_manifest is None:
            cur_manifest = {'experiment_id': EXPERIMENT_ID, 'n_trials': 0,
                            'note': '', 'task': tid, 'task_name': task_name,
                            'task_hash': task_hash, 'task_version_id': vid,
                            'trials': {}}
        existing_trials = cur_manifest.get('trials', {}) or {}
        merged = dict(existing_trials)
        for ctid, entry in ts['new_entries'].items():
            merged[ctid] = entry
        # Sort by trial index
        def _idx(t):
            tail = t.rsplit('-',1)[-1]
            return (int(tail) if tail.isdigit() else 1<<30, t)
        merged = dict(sorted(merged.items(), key=lambda kv: _idx(kv[0])))

        # Top-level metadata
        existing_exp_ids = cur_manifest.get('experiment_ids') or [cur_manifest.get('experiment_id')]
        existing_exp_ids = [e for e in existing_exp_ids if e]
        if EXPERIMENT_ID not in existing_exp_ids:
            existing_exp_ids.append(EXPERIMENT_ID)
        existing_versions = list(cur_manifest.get('task_versions') or [])
        if cur_manifest.get('task_version_id') and cur_manifest['task_version_id'] not in existing_versions:
            existing_versions.append(cur_manifest['task_version_id'])
        if vid not in existing_versions:
            existing_versions.append(vid)
        existing_hashes = list(cur_manifest.get('task_hashes') or [])
        if task_hash not in existing_hashes:
            existing_hashes.append(task_hash)
        n_added = len([t for t in ts['new_entries'] if t not in existing_trials])
        additional = cur_manifest.get('additional_imports') or []
        additional.append({
            'imported_at': datetime.now(timezone.utc).isoformat(),
            'source_oddish_experiment_id': EXPERIMENT_ID,
            'task_version_id_at_snapshot': vid,
            'n_trials_added': n_added,
            'pairs_added': [['claude-code', NORM_MODEL]],
            'note': (f'Preliminary results for claude-code + claude-fable-5 '
                     f'(Bedrock global.anthropic.claude-fable-5). Strict '
                     f'validity: SUCCESS + numeric reward + exception_classes '
                     f'⊆ {{AgentTimeoutError}} + complete verifier artifacts. '
                     f'Ranked by reward=1 first, then verifier partial_score, '
                     f'then trajectory length. Excluded tasks per user: k8s, '
                     f'nextjs, s3-clone, ruby-rust-port, post-train-ifeval.'),
        })

        cur_manifest['experiment_ids'] = existing_exp_ids
        cur_manifest['task_versions'] = existing_versions
        cur_manifest['task_hashes'] = existing_hashes
        cur_manifest['is_multi_version'] = len(existing_versions) > 1
        cur_manifest['trials'] = merged
        cur_manifest['n_trials'] = len(merged)
        cur_manifest['additional_imports'] = additional
        # pair_counts + n_pass refresh
        pc = Counter((e['agent'], e['model']) for e in merged.values())
        cur_manifest['pair_counts'] = {f"{a}|{m}": n for (a,m), n in pc.items()}
        cur_manifest['n_pass'] = sum(1 for e in merged.values() if e.get('reward') == 1.0)
        # Append note
        existing_note = cur_manifest.get('note') or ''
        n_pass = sum(1 for entry in ts['new_entries'].values() if entry.get('reward') == 1.0)
        append = (f" | {datetime.now(timezone.utc).isoformat()} appended "
                  f"{n_added} preliminary claude-code/{NORM_MODEL} trials from "
                  f"Oddish experiment {EXPERIMENT_ID} on task_version {vid} "
                  f"({n_pass}/{len(ts['new_entries'])} passes).")
        cur_manifest['note'] = (existing_note + append).strip()

        body = json.dumps(cur_manifest, indent=2, default=str).encode('utf-8')
        dst.put_object(Bucket=DEST_BUCKET, Key=f"{task_name}/_manifest.json",
                       Body=body, ContentType='application/json')
        with open(f"/tmp/oddish-import/{task_name}_fable5_manifest.json",'w') as f:
            f.write(body.decode())
        print(f"  manifest updated: n_trials={cur_manifest['n_trials']} "
              f"(+{n_added}); experiment_ids={existing_exp_ids}; "
              f"is_multi_version={cur_manifest['is_multi_version']}")

if __name__ == '__main__':
    main()
