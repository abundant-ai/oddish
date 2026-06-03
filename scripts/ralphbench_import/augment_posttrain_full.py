"""Upload the 63 new post-train-ifeval picks + augment the manifest."""
from __future__ import annotations
import os, sys, json, time
sys.path.insert(0, os.path.dirname(__file__))
from augment_cd4dd88f import (
    make_src, make_dst, upload_one_trial, build_entry, DEST_BUCKET,
)
from collections import Counter
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

TASK_ID     = 'post-train-ifeval-c9f2cfbc'
TASK_NAME   = 'post-train-ifeval'
TASK_HASH   = 'c9f2cfbc'

def main():
    sel = json.load(open('/tmp/oddish-import/selection_posttrain_full.json'))
    src = make_src(); dst = make_dst()

    state_path = '/tmp/oddish-import/state_posttrain_full.json'
    state = json.load(open(state_path)) if os.path.exists(state_path) else {'entries': {}, 'failures': []}

    all_picks = []
    for cell_id, cell in sel.items():
        sel_meta = {
            'task_id': TASK_ID, 'task_name': TASK_NAME, 'task_hash': TASK_HASH,
            'task_version_id': cell['task_version_id'],
        }
        for pid in cell['picks']:
            cand = next(c for c in cell['all_candidates'] if c['id'] == pid)
            all_picks.append((cell_id, sel_meta, cand))
    print(f"Total picks to upload: {len(all_picks)}")

    for i, (cell_id, sel_meta, cand) in enumerate(all_picks, 1):
        tid = cand['id']
        if tid in state['entries']:
            print(f"  [{i}/{len(all_picks)}] {tid}: already uploaded, skipping")
            continue
        # Override raw model in build_entry: our normalized model is in cand['model']
        # already, but we want raw_model preserved.
        t0 = time.time()
        try:
            canonical, nb = upload_one_trial(src, dst, sel_meta, cand, TASK_NAME)
            if canonical is None:
                print(f"  [{i}/{len(all_picks)}] {tid}: FAILED — no source keys")
                state['failures'].append({'trial_id': tid, 'reason': 'no keys'})
                continue
            entry = build_entry(cand, sel_meta, canonical, TASK_NAME)
            # Override source_oddish_experiment_id with the candidate's actual exp
            entry['source_oddish_experiment_id'] = cand['source_oddish_experiment_id']
            entry['raw_model'] = cand.get('raw_model', cand['model'])
            # Record the partial_score we picked on
            if cand.get('partial_score') is not None:
                entry['verifier_partial_score'] = cand['partial_score']
            entry['cell_id'] = cell_id
            state['entries'][tid] = entry
            with open(state_path, 'w') as f:
                json.dump(state, f, indent=2)
            print(f"  [{i}/{len(all_picks)}] {tid:38s}  {nb/1024/1024:>6.1f} MB in {time.time()-t0:>5.1f}s  "
                  f"({cell_id})  canonical={canonical}")
        except Exception as e:
            print(f"  [{i}/{len(all_picks)}] {tid}: FAILED — {e}")
            state['failures'].append({'trial_id': tid, 'reason': str(e)})

    # ---- Augment the manifest ----
    cur_manifest = json.loads(dst.get_object(Bucket=DEST_BUCKET, Key=f"{TASK_NAME}/_manifest.json")['Body'].read())
    existing = cur_manifest.get('trials', {}) or {}
    merged = dict(existing)
    n_new = 0
    for tid, entry in state['entries'].items():
        if tid not in existing:
            n_new += 1
        merged[tid] = entry

    def _idx(t):
        tail = t.rsplit('-',1)[-1]
        return (int(tail) if tail.isdigit() else 1<<30, t)
    merged = dict(sorted(merged.items(), key=lambda kv: _idx(kv[0])))

    cur_manifest['trials'] = merged
    cur_manifest['n_trials'] = len(merged)
    # Collect experiment_ids and task_versions
    eids = list(cur_manifest.get('experiment_ids') or [cur_manifest.get('experiment_id')])
    eids = [e for e in eids if e]
    for e in state['entries'].values():
        if e.get('source_oddish_experiment_id') not in eids:
            eids.append(e['source_oddish_experiment_id'])
    cur_manifest['experiment_ids'] = eids
    tv = list(cur_manifest.get('task_versions') or [cur_manifest.get('task_version_id')])
    tv = [t for t in tv if t]
    for e in state['entries'].values():
        if e.get('task_version_id') not in tv:
            tv.append(e['task_version_id'])
    cur_manifest['task_versions'] = tv
    cur_manifest['is_multi_version'] = len(tv) > 1
    addl = cur_manifest.get('additional_imports') or []
    addl.append({
        'imported_at': datetime.now(timezone.utc).isoformat(),
        'source_oddish_experiment_ids': sorted({e['source_oddish_experiment_id'] for e in state['entries'].values()}),
        'task_versions_imported': sorted({e['task_version_id'] for e in state['entries'].values()}),
        'n_trials_added': n_new,
        'cells_added': sorted({e['cell_id'] for e in state['entries'].values()}),
        'note': (
            'Backfill of remaining post-train-ifeval cells. v34 (same content '
            'hash as v32/v33) for all model-bearing cells; v31 for nop/oracle '
            'baselines (older content, but only version with 5 baseline trials '
            'on the post-IFEval task). Ranked by reward then verifier '
            'binary_strict/partial_score then trajectory_bytes then total_bytes '
            'then recency. Strict validity (AgentTimeoutError or clean only).'
        ),
    })
    cur_manifest['additional_imports'] = addl
    cur_manifest['note'] = (cur_manifest.get('note') or '') + (
        f" | {datetime.now(timezone.utc).isoformat()} backfilled {n_new} trials "
        f"across {len(set(e['cell_id'] for e in state['entries'].values()))} cells "
        f"(experiments {sorted({e['source_oddish_experiment_id'] for e in state['entries'].values()})}, "
        f"versions {sorted({e['task_version_id'] for e in state['entries'].values()})})."
    )

    body = json.dumps(cur_manifest, indent=2, default=str).encode('utf-8')
    dst.put_object(Bucket=DEST_BUCKET, Key=f"{TASK_NAME}/_manifest.json", Body=body,
                   ContentType='application/json')
    with open(f"/tmp/oddish-import/{TASK_NAME}_manifest.json", 'w') as f:
        f.write(body.decode())

    print(f"\nManifest updated: n_trials={cur_manifest['n_trials']} (+{n_new}); "
          f"experiment_ids={eids}; task_versions={tv}; is_multi_version={cur_manifest['is_multi_version']}")
    pc = Counter((e['agent'], e['model']) for e in merged.values())
    for p, n in sorted(pc.items()):
        npass = sum(1 for ee in merged.values()
                    if ee['agent']==p[0] and ee['model']==p[1] and ee.get('reward')==1.0)
        print(f"  {p[0]:12s} / {p[1]:42s}  n={n}  pass={npass}")

if __name__ == '__main__':
    main()
