"""User-explicit override: copy the top 5 claude-code/anthropic/claude-opus-4-8
trials for nextjs-vite-rewrite from cd4dd88f into ralphbench-logs, even
though every candidate hit NonZeroAgentExitCodeError (would normally be
excluded by the strict filter). Each new manifest entry carries
``override_validity: true`` plus an ``override_reason`` string so future
audits can recognize and pass them.

This reuses the upload primitives from augment_cd4dd88f.py.
"""
from __future__ import annotations
import os, json, sys, time
import boto3
from botocore.config import Config
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from augment_cd4dd88f import (  # noqa: E402
    make_src, make_dst, list_keys, pick_canonical, transform_top,
    upload_one_trial, build_entry, DEST_BUCKET, SRC_BUCKET,
)

EXPERIMENT_ID = 'cd4dd88f'
TASK_ID       = 'nextjs-vite-rewrite-b0d028b0'
TASK_NAME     = 'nextjs-vite-rewrite'
TASK_HASH     = 'b0d028b0'

PICKS_FILE = '/tmp/oddish-import/nextjs_cc_top5.json'
OVERRIDE_REASON = (
    'User-explicit override (Tue 2026-06-02): user accepted '
    'NonZeroAgentExitCodeError trials for this one cell because all 10 '
    'cd4dd88f SUCCESS+numeric_reward candidates for '
    'nextjs-vite-rewrite / claude-code / anthropic/claude-opus-4-8 hit '
    'that exception class. Verifier ran end-to-end on every pick; '
    'reward=0 emitted with full trajectory + logs + metrics.'
)

def main():
    data = json.load(open(PICKS_FILE))
    picks = data['top_5_picks']
    sel_meta = {
        'task_id': TASK_ID, 'task_name': TASK_NAME, 'task_hash': TASK_HASH,
        'task_version_id': data['task_version_id'],
    }
    src = make_src(); dst = make_dst()

    # Track per-trial state separately from the main cd4dd88f run.
    state_path = '/tmp/oddish-import/state_nextjs_cc_override.json'
    state = json.load(open(state_path)) if os.path.exists(state_path) else {'new_entries': {}}

    print(f"== Overriding nextjs claude-code/opus-4-8 — {len(picks)} trials ==")
    for i, c in enumerate(picks, 1):
        tid = c['trial_id']
        if tid in state['new_entries']:
            print(f"  [{i}/{len(picks)}] {tid}: already uploaded, skipping")
            continue
        # build_entry expects 'id' not 'trial_id' from upstream selection format
        trial_meta = dict(c)
        trial_meta['id'] = tid
        trial_meta['agent'] = 'claude-code'
        trial_meta['model'] = 'anthropic/claude-opus-4-8'
        trial_meta['provider'] = 'anthropic'
        trial_meta['status'] = 'success'
        trial_meta['has_trajectory'] = True
        trial_meta['trajectory_bytes_max'] = c['trajectory_bytes']
        trial_meta['exception_classes'] = c['exception_classes']
        trial_meta['selection_note'] = (
            'Top 5 of 10 cd4dd88f SUCCESS+numeric_reward trials for this cell '
            '(ranked by trajectory_bytes), allowed despite '
            'NonZeroAgentExitCodeError per user override.'
        )
        t0 = time.time()
        try:
            canonical, nb = upload_one_trial(src, dst, sel_meta, trial_meta, TASK_NAME)
            if canonical is None:
                print(f"  [{i}/{len(picks)}] {tid}: FAILED — no keys at source")
                continue
            entry = build_entry(trial_meta, sel_meta, canonical, TASK_NAME)
            entry['override_validity'] = True
            entry['override_reason'] = OVERRIDE_REASON
            state['new_entries'][tid] = entry
            with open(state_path, 'w') as f:
                json.dump(state, f, indent=2)
            print(f"  [{i}/{len(picks)}] {tid}: uploaded {nb/1024/1024:.1f} MB in {time.time()-t0:.1f}s; canonical={canonical}")
        except Exception as e:
            print(f"  [{i}/{len(picks)}] {tid}: FAILED — {e}")

    # ---- Manifest augmentation (preserve existing entries) ----
    cur_manifest = json.loads(
        dst.get_object(Bucket=DEST_BUCKET, Key=f"{TASK_NAME}/_manifest.json")['Body'].read()
    )
    existing = cur_manifest.get('trials', {}) or {}
    merged = dict(existing)
    n_new = 0
    for tid, entry in state['new_entries'].items():
        if tid not in existing:
            n_new += 1
        merged[tid] = entry

    def _idx(t):
        tail = t.rsplit('-',1)[-1]
        return (int(tail) if tail.isdigit() else 1<<30, t)
    merged = dict(sorted(merged.items(), key=lambda kv: _idx(kv[0])))

    cur_manifest['trials'] = merged
    cur_manifest['n_trials'] = len(merged)
    # Audit log entry for this override import.
    additional = cur_manifest.get('additional_imports') or []
    additional.append({
        'imported_at': datetime.now(timezone.utc).isoformat(),
        'source_oddish_experiment_id': EXPERIMENT_ID,
        'task_version_id_at_snapshot': data['task_version_id'],
        'n_trials_added': n_new,
        'pairs_added': [['claude-code', 'anthropic/claude-opus-4-8']],
        'note': (
            'OVERRIDE: ' + OVERRIDE_REASON
        ),
    })
    cur_manifest['additional_imports'] = additional
    # Top-level note append
    cur_manifest['note'] = (cur_manifest.get('note') or '') + (
        f" | {datetime.now(timezone.utc).isoformat()} appended {n_new} trials from "
        f"Oddish experiment {EXPERIMENT_ID} for claude-code/anthropic/claude-opus-4-8 "
        f"on {data['task_version_id']} as an explicit user override of the strict "
        f"validity filter (NonZeroAgentExitCodeError accepted — verifier ran on every "
        f"included trial and emitted reward=0)."
    )
    # Refresh experiment_ids / task_versions / hashes (cd4dd88f already there but harmless)
    eids = list(cur_manifest.get('experiment_ids') or [cur_manifest.get('experiment_id')])
    eids = [e for e in eids if e]
    if EXPERIMENT_ID not in eids: eids.append(EXPERIMENT_ID)
    cur_manifest['experiment_ids'] = eids
    tv = list(cur_manifest.get('task_versions') or [])
    if data['task_version_id'] not in tv: tv.append(data['task_version_id'])
    cur_manifest['task_versions'] = tv
    cur_manifest['is_multi_version'] = len(tv) > 1
    body = json.dumps(cur_manifest, indent=2, default=str).encode('utf-8')
    dst.put_object(Bucket=DEST_BUCKET, Key=f"{TASK_NAME}/_manifest.json",
                   Body=body, ContentType='application/json')
    with open(f"/tmp/oddish-import/{TASK_NAME}_manifest.json",'w') as f:
        f.write(body.decode())
    pair_counts = Counter((e['agent'], e['model']) for e in merged.values())
    print(f"\nManifest n_trials={cur_manifest['n_trials']} (+{n_new}); experiment_ids={eids}")
    for p, n in sorted(pair_counts.items()):
        print(f"  {p[0]:12s} / {p[1]:40s}  n={n}")

if __name__ == '__main__':
    main()
