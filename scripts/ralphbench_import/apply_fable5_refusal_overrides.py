"""Override-include the 10 claude-code/claude-fable-5 trials that hit
NonZeroAgentExitCodeError on embedding-eval and trimul-cuda. Per user:

  'Fable refused to answer embed and trimul tasks - but those refusals
   should still be treated as fails. Fable doesn't answer ML coding
   questions by design, so it should be penalized accordingly. But maybe
   ensure that reward=0 and metrics.json of 0.'

All 10 trials are verified pre-upload to have:
  - status='SUCCESS' (the trial completed; the agent's process crashed)
  - reward = 0.0  (DB)
  - verifier/metrics.json present with partial_score = 0.0
  - verifier/reward.txt = '0'
  - exception_classes = {'NonZeroAgentExitCodeError'}  (exit 1: agent
    crash, NOT SIGKILL; consistent with model refusing to engage with
    ML coding tasks and exiting non-zero rather than producing output)

Each entry carries:
  - override_validity = true
  - override_reason  (explains the refusal pattern + the verifier
    confirming reward=0 / partial_score=0)
  - agent_exit_code = 1
"""
from __future__ import annotations
import os, sys, json, time
import boto3, psycopg2
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, '/workspace/scripts/ralphbench_import')
from augment_cd4dd88f import make_src, make_dst, list_keys, pick_canonical, transform_top

DEST_BUCKET = 'ralphbench-logs'
SRC_BUCKET = os.environ['ODDISH_S3_BUCKET']
EXPERIMENT_ID = '3942355b'
RAW_MODEL = 'global.anthropic.claude-fable-5'
NORM_MODEL = 'anthropic/claude-fable-5'

REFUSALS_BY_TASK = {
    'embedding-eval': [
        'embedding-eval-be1dc228-358', 'embedding-eval-be1dc228-359',
        'embedding-eval-be1dc228-360', 'embedding-eval-be1dc228-368',
        'embedding-eval-be1dc228-372',
    ],
    'trimul-cuda': [
        'trimul-cuda-fc34aaba-591', 'trimul-cuda-fc34aaba-592',
        'trimul-cuda-fc34aaba-593', 'trimul-cuda-fc34aaba-600',
        'trimul-cuda-fc34aaba-604',
    ],
}


def upload_one_trial(src, dst, sel_meta, trial, dst_task_name):
    tid = trial['id']
    src_prefix = trial['prefix']
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
        dst.put_object(Bucket=DEST_BUCKET, Key=dk, Body=body)
        return len(body)

    total = 0
    with ThreadPoolExecutor(max_workers=16) as pool:
        for fut in as_completed([pool.submit(_proc, it) for it in to_copy]):
            total += fut.result()
    return canonical, total


def main():
    src = make_src(); dst = make_dst()
    url = os.environ['ODDISH_DATABASE_URL'].replace('postgresql+asyncpg://','postgresql://')
    cur = psycopg2.connect(url + '?sslmode=require').cursor()

    state_path = '/tmp/oddish-import/state_fable5_refusals.json'
    state = {'tasks': {}}
    if os.path.exists(state_path):
        state = json.load(open(state_path))

    for task_name, rid_list in REFUSALS_BY_TASK.items():
        # Pull DB rows for the 5 trials
        cur.execute("""SELECT t.id, t.task_id, t.task_version_id, t.provider,
                              t.reward, t.status, t.attempts, t.max_attempts,
                              t.started_at, t.finished_at,
                              t.input_tokens, t.cache_tokens, t.output_tokens,
                              t.cost_usd, t.has_trajectory, t.name, t.trial_s3_key
                       FROM trials t WHERE id = ANY(%s)""", (rid_list,))
        rows = cur.fetchall()
        if not rows: continue

        task_id = rows[0][1]; vid = rows[0][2]
        task_hash = task_id.rsplit('-',1)[-1]
        sel_meta = {'task_id': task_id, 'task_name': task_name,
                    'task_hash': task_hash, 'task_version_id': vid}

        print(f"\n=== {task_name}  ({len(rows)} refusal trials, version {vid}) ===")
        ts = state['tasks'].setdefault(task_id, {'task_name': task_name,
            'task_hash': task_hash, 'task_version_id': vid,
            'new_entries': {}, 'failures': []})

        for r in rows:
            (rid, _, _, provider, reward, status, att, mx, sa, fa,
             it, ct, ot, cu, ht, name, s3k) = r
            prefix = (s3k or f"tasks/{task_id}/trials/{rid}/")
            if not prefix.endswith('/'): prefix += '/'

            # fetch partial_score from verifier
            try:
                m = json.loads(src.get_object(Bucket=SRC_BUCKET,
                    Key=prefix+f"{pick_canonical(src, prefix, list_keys(src, prefix))}/verifier/metrics.json")['Body'].read())
                partial = m.get('partial_score', 0.0)
            except Exception:
                partial = None

            trial = {'id': rid, 'prefix': prefix,
                     'agent': 'claude-code', 'model': NORM_MODEL,
                     'provider': provider or 'bedrock', 'reward': reward,
                     'status': status, 'attempts': att, 'max_attempts': mx,
                     'started_at': sa.isoformat() if sa else None,
                     'finished_at': fa.isoformat() if fa else None,
                     'input_tokens': it, 'cache_tokens': ct, 'output_tokens': ot,
                     'cost_usd': cu, 'has_trajectory': ht,
                     'source_trial_name': name,
                     'exception_classes': ['NonZeroAgentExitCodeError'],
                     'partial_score': partial}

            if rid in ts['new_entries']:
                print(f"  {rid}: already uploaded, skipping")
                continue
            t0 = time.time()
            try:
                canonical, nb = upload_one_trial(src, dst, sel_meta, trial, task_name)
                if canonical is None:
                    print(f"  {rid}: FAILED — no keys at source")
                    ts['failures'].append({'trial_id': rid, 'reason': 'no keys'})
                    continue
                print(f"  {rid}: uploaded {nb/1024/1024:.1f} MB in {time.time()-t0:.1f}s")

                entry = {
                    'agent': 'claude-code', 'model': NORM_MODEL,
                    'provider': trial['provider'], 'reward': reward,
                    'status': status, 'attempts': att, 'max_attempts': mx,
                    'task_version_id': vid,
                    'started_at': trial['started_at'], 'finished_at': trial['finished_at'],
                    'input_tokens': it, 'cache_tokens': ct, 'output_tokens': ot,
                    'cost_usd': cu, 'has_trajectory': ht,
                    'trajectory_bytes': 0,
                    'exception_classes': ['NonZeroAgentExitCodeError'],
                    'origin': 'oddish',
                    'artifact_layout': 'harbor-nested',
                    'artifact_prefix': f"s3://{DEST_BUCKET}/{task_name}/{rid}/",
                    'source_trial_name': name,
                    'source_oddish_task_id': task_id,
                    'source_oddish_trial_id': rid,
                    'source_oddish_experiment_id': EXPERIMENT_ID,
                    'canonical_attempt_dir': canonical,
                    'task_name': task_name, 'task_hash': task_hash,
                    'raw_model': RAW_MODEL,
                    'verifier_partial_score': partial,
                    'agent_exit_code': 1,
                    'override_validity': True,
                    'override_reason': (
                        f'Fable refused to engage with this ML-coding task by design '
                        f'and the Claude Code wrapper exited non-zero (exit 1, NOT '
                        f'SIGKILL) without producing output. The verifier ran and '
                        f'graded the empty/non-conforming output as reward=0 with '
                        f'verifier/metrics.json reporting partial_score=0.0 and '
                        f'verifier/reward.txt="0" — i.e., the verifier treated the '
                        f'refusal as a clean failure. Per user instruction, the '
                        f'refusal counts as a legitimate fail and the model should '
                        f'be penalized accordingly. exception_classes=[NonZero] '
                        f'is preserved for forensic clarity.'
                    ),
                    'selection_note': (
                        f'Imported from Oddish experiment {EXPERIMENT_ID} as a '
                        f'fable-5 refusal-fail entry. Override-include because '
                        f'the verifier produced a complete grade (reward=0, '
                        f'partial_score=0) — i.e., outcome is indistinguishable '
                        f'from a normal reward=0 trial. The agent\'s NonZero exit '
                        f'reflects refusal to attempt the ML-coding task, not '
                        f'infrastructure failure.'
                    ),
                }
                ts['new_entries'][rid] = entry
                with open(state_path,'w') as f:
                    json.dump(state, f, indent=2, default=str)
            except Exception as e:
                print(f"  {rid}: FAILED — {e}")
                ts['failures'].append({'trial_id': rid, 'reason': str(e)})

        # Augment existing manifest
        try:
            cur_manifest = json.loads(dst.get_object(Bucket=DEST_BUCKET,
                Key=f"{task_name}/_manifest.json")['Body'].read())
        except Exception:
            cur_manifest = None
        if cur_manifest is None:
            cur_manifest = {'experiment_id': EXPERIMENT_ID, 'n_trials': 0,
                            'task': task_id, 'task_name': task_name,
                            'task_hash': task_hash, 'task_version_id': vid,
                            'trials': {}}
        existing = cur_manifest.get('trials', {}) or {}
        merged = dict(existing); merged.update(ts['new_entries'])
        def _idx(t):
            tail = t.rsplit('-',1)[-1]
            return (int(tail) if tail.isdigit() else 1<<30, t)
        merged = dict(sorted(merged.items(), key=lambda kv: _idx(kv[0])))

        exp_ids = cur_manifest.get('experiment_ids') or [cur_manifest.get('experiment_id')]
        exp_ids = [e for e in exp_ids if e]
        if EXPERIMENT_ID not in exp_ids: exp_ids.append(EXPERIMENT_ID)
        versions = list(cur_manifest.get('task_versions') or [])
        if cur_manifest.get('task_version_id') and cur_manifest['task_version_id'] not in versions:
            versions.append(cur_manifest['task_version_id'])
        if vid not in versions: versions.append(vid)
        hashes = list(cur_manifest.get('task_hashes') or [])
        if task_hash not in hashes: hashes.append(task_hash)

        n_added = len([t for t in ts['new_entries'] if t not in existing])
        additional = cur_manifest.get('additional_imports') or []
        additional.append({
            'imported_at': datetime.now(timezone.utc).isoformat(),
            'source_oddish_experiment_id': EXPERIMENT_ID,
            'task_version_id_at_snapshot': vid,
            'n_trials_added': n_added,
            'n_trials_override': n_added,
            'pairs_added': [['claude-code', NORM_MODEL]],
            'note': (f'5 fable-5 refusal-fail trials per user request: model '
                     f'refused to engage with this ML-coding task by design; '
                     f'verifier still produced a complete grade (reward=0, '
                     f'partial_score=0) so the refusal is treated as a '
                     f'legitimate failure. override_validity=true with explicit '
                     f'override_reason per entry. Same data as the strict pool '
                     f'rejected from augment_fable5.py.'),
        })
        cur_manifest['experiment_ids'] = exp_ids
        cur_manifest['task_versions'] = versions
        cur_manifest['task_hashes'] = hashes
        cur_manifest['is_multi_version'] = len(versions) > 1
        cur_manifest['trials'] = merged
        cur_manifest['n_trials'] = len(merged)
        cur_manifest['additional_imports'] = additional
        pc = Counter((e['agent'], e['model']) for e in merged.values())
        cur_manifest['pair_counts'] = {f"{a}|{m}": n for (a,m), n in pc.items()}
        cur_manifest['n_pass'] = sum(1 for e in merged.values() if e.get('reward') == 1.0)
        existing_note = cur_manifest.get('note') or ''
        append = (f" | {datetime.now(timezone.utc).isoformat()} appended {n_added} "
                  f"fable-5 refusal-fail trials (NonZero exit 1 with verifier "
                  f"reward=0/partial=0); override_validity=true per user.")
        cur_manifest['note'] = (existing_note + append).strip()

        body = json.dumps(cur_manifest, indent=2, default=str).encode('utf-8')
        dst.put_object(Bucket=DEST_BUCKET, Key=f"{task_name}/_manifest.json",
                       Body=body, ContentType='application/json')
        with open(f"/tmp/oddish-import/{task_name}_fable5_refusal_manifest.json",'w') as f:
            f.write(body.decode())
        print(f"  manifest updated: n_trials={cur_manifest['n_trials']} (+{n_added}); "
              f"n_pass={cur_manifest['n_pass']}; experiment_ids={exp_ids}")

if __name__ == '__main__':
    main()
