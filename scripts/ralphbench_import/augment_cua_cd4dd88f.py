"""Upload cd4dd88f CUA trial dirs to ralphbench-logs and merge them into
each task's existing _manifest.json.

Same pattern as augment_cd4dd88f.py — preserves existing entries, appends
new ones, updates top-level metadata (experiment_ids, task_versions,
is_multi_version, additional_imports[]).

For the claude-code/anthropic/claude-opus-4-8 pair the raw model string
varies by route (bedrock vs openrouter). The selection collapses them
into one normalized cell; each entry's ``provider`` field records the
actual route. The top-level config.json/result.json have the canonical
model id ('anthropic/claude-opus-4-8') injected even though the inner
Harbor config still carries the raw form.
"""
from __future__ import annotations
import os, json, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from augment_cd4dd88f import (  # noqa: E402
    make_src, make_dst, upload_one_trial, build_entry, DEST_BUCKET,
)

EXPERIMENT_ID = 'cd4dd88f'

def main():
    only_task = sys.argv[1] if len(sys.argv) > 1 else None
    sel = json.load(open('/tmp/oddish-import/selection_cua_cd4dd88f.json'))
    src = make_src(); dst = make_dst()

    state_path = '/tmp/oddish-import/state_cua_cd4dd88f.json'
    state = json.load(open(state_path)) if os.path.exists(state_path) else {'tasks': {}}

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
        ts = state['tasks'].setdefault(task_id, {
            'task_name': task_name, 'task_hash': t['task_hash'],
            'task_version_id': t['task_version_id'],
            'new_entries': {}, 'failures': [],
        })
        for i, c in enumerate(picks_in_order, 1):
            tid = c['id']
            if tid in ts['new_entries']:
                print(f"  [{i}/{len(picks_in_order)}] {tid}: already uploaded, skipping")
                continue
            t0 = time.time()
            try:
                canonical, nb = upload_one_trial(src, dst, sel_meta, c, task_name)
                if canonical is None:
                    print(f"  [{i}/{len(picks_in_order)}] {tid}: FAILED — no keys")
                    ts['failures'].append({'trial_id': tid, 'reason': 'no keys'})
                    continue
                print(f"  [{i}/{len(picks_in_order)}] {tid}: {nb/1024/1024:.1f} MB in "
                      f"{time.time()-t0:.1f}s  canonical={canonical}  provider={c['provider']}")
                ts['new_entries'][tid] = build_entry(c, sel_meta, canonical, task_name)
                # Note the raw route inside the entry for traceability.
                ts['new_entries'][tid]['raw_model'] = c.get('raw_model', c['model'])
            except Exception as e:
                print(f"  [{i}/{len(picks_in_order)}] {tid}: FAILED — {e}")
                ts['failures'].append({'trial_id': tid, 'reason': str(e)})
            with open(state_path, 'w') as f:
                json.dump(state, f, indent=2)

        # ---- Augment manifest in place ----
        cur_manifest = json.loads(
            dst.get_object(Bucket=DEST_BUCKET, Key=f"{task_name}/_manifest.json")['Body'].read()
        )
        existing = cur_manifest.get('trials', {}) or {}
        merged = dict(existing)
        n_new = 0
        for tid, entry in ts['new_entries'].items():
            if tid not in existing:
                n_new += 1
            merged[tid] = entry

        def _idx(t):
            tail = t.rsplit('-',1)[-1]
            return (int(tail) if tail.isdigit() else 1<<30, t)
        merged = dict(sorted(merged.items(), key=lambda kv: _idx(kv[0])))

        cur_manifest['trials'] = merged
        cur_manifest['n_trials'] = len(merged)
        eids = list(cur_manifest.get('experiment_ids') or [cur_manifest.get('experiment_id')])
        eids = [e for e in eids if e]
        if EXPERIMENT_ID not in eids: eids.append(EXPERIMENT_ID)
        cur_manifest['experiment_ids'] = eids
        tv = list(cur_manifest.get('task_versions') or [])
        if cur_manifest.get('task_version_id') and cur_manifest['task_version_id'] not in tv:
            tv.append(cur_manifest['task_version_id'])
        if t['task_version_id'] not in tv: tv.append(t['task_version_id'])
        cur_manifest['task_versions'] = tv
        cur_manifest['is_multi_version'] = len(tv) > 1
        addl = cur_manifest.get('additional_imports') or []
        addl.append({
            'imported_at': datetime.now(timezone.utc).isoformat(),
            'source_oddish_experiment_id': EXPERIMENT_ID,
            'task_version_id_at_snapshot': t['task_version_id'],
            'n_trials_added': n_new,
            'pairs_added': sorted({(e['agent'], e['model']) for e in ts['new_entries'].values()},
                                  key=lambda p:(p[0],p[1])),
            'note': (
                'CUA-task cd4dd88f import: claude-code/anthropic/claude-opus-4-8 + '
                'gemini-cli/gemini/gemini-3.5-flash. Strict validity (AgentTimeoutError '
                'or clean only). For the claude pair, both bedrock and openrouter route '
                'trials are folded into one normalized cell (model="anthropic/claude-opus-4-8"); '
                'the actual route is recorded per-entry as provider in {bedrock, claude, openrouter}. '
                'Trials are still rolling in upstream so this cell may be short of 5 '
                'until more land.'
            ),
        })
        cur_manifest['additional_imports'] = addl
        # Note tail
        cur_manifest['note'] = (cur_manifest.get('note') or '') + (
            f" | {datetime.now(timezone.utc).isoformat()} appended {n_new} trials from "
            f"Oddish experiment {EXPERIMENT_ID} ({t['task_version_id']}) for "
            f"claude-code/anthropic/claude-opus-4-8 (bedrock + openrouter routes folded "
            f"into one normalized cell) and gemini-cli/gemini/gemini-3.5-flash. Strict "
            f"AgentTimeoutError-only validity filter."
        )
        body = json.dumps(cur_manifest, indent=2, default=str).encode('utf-8')
        dst.put_object(Bucket=DEST_BUCKET, Key=f"{task_name}/_manifest.json",
                       Body=body, ContentType='application/json')
        with open(f"/tmp/oddish-import/{task_name}_manifest.json", 'w') as f:
            f.write(body.decode())
        pair_counts = Counter((e['agent'], e['model']) for e in merged.values())
        # Provider breakdown for the new opus-4-8 pair
        opus_routes = Counter(e.get('provider','?') for e in merged.values()
                              if e.get('agent')=='claude-code' and e.get('model')=='anthropic/claude-opus-4-8'
                              and e.get('source_oddish_experiment_id')==EXPERIMENT_ID)
        print(f"  Updated _manifest.json: n_trials={cur_manifest['n_trials']} (+{n_new});  "
              f"experiment_ids={eids}; is_multi_version={cur_manifest['is_multi_version']}")
        for p, n in sorted(pair_counts.items()):
            print(f"    {p[0]:12s} / {p[1]:42s}  n={n}")
        if opus_routes:
            print(f"  cd4dd88f opus-4-8 route breakdown: {dict(opus_routes)}")

if __name__ == '__main__':
    main()
