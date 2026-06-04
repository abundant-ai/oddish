"""User-explicit override (Thu 2026-06-04): three NonZeroAgentExitCodeError
trials accepted because their verifier output is fully populated and
indistinguishable from a valid run except for the agent exit code.

  - find-network-alignments-b2ecebb7-188:
      exit 143 (SIGTERM/wallclock); verifier produced full alignment
      metrics — partial_score=0.67, S3=0.2508 (target >=0.32), full
      yeast_alignment data. Agent did 497 KB of trajectory work.
  - parameter-golf-61e11605-437:
      exit 143 (SIGTERM); verifier passed 7/8 checks, partial_score=1.0,
      pass_rate=0.875, full leaderboard data (trained real LM, val_bpb
      1.0806 vs 0.983 threshold). Reward=0 came from the one validation
      gate the model didn't beat, not from infra.
  - trimul-cuda-fc34aaba-566:
      exit 1 (claude crashed late, not a timeout); verifier ran kernel
      compile + cache probe + benchmark, correctness_passed=False but
      cache_probe_passed=True. $9.18 of real agent cost burned.
      1.77 MB trajectory — largest of any v86 trial.

The 4th candidate (find-network-alignments-189) was skipped per user:
its verifier metrics.json says 'pytest exited before metrics could be
computed' — so there are no real statistics to compare against.

Each manifest entry carries override_validity=true + override_reason
recording the exit code and what the verifier actually produced. The
intentionally_tombstoned_trials list is updated so reconciliation tools
stop trying to restore them as plain entries.
"""
from __future__ import annotations
import os, sys, json, time
sys.path.insert(0, os.path.dirname(__file__))
from augment_cd4dd88f import (
    make_src, make_dst, upload_one_trial, build_entry, DEST_BUCKET,
)
import psycopg2, boto3
from datetime import datetime, timezone
from collections import Counter

OVERRIDES = [
    {
        'task_name': 'find-network-alignments',
        'task_id': 'find-network-alignments-b2ecebb7',
        'task_hash': 'b2ecebb7',
        'task_version_id': 'find-network-alignments-b2ecebb7-v5',
        'trial_id': 'find-network-alignments-b2ecebb7-188',
        'override_reason': (
            'User-explicit override (Thu 2026-06-04): NonZeroAgentExitCodeError '
            '(exit 143 = SIGTERM at 18000s wallclock — semantically an '
            'AgentTimeoutError, just caught at a lower layer). Verifier produced '
            'full alignment metrics: partial_score=0.67, S3=0.2508 (target '
            ">=0.32), full primary_alignment and yeast_alignment dicts. "
            'Trajectory 497 KB, agent log 557 KB. Reward=0 emitted cleanly.'
        ),
    },
    {
        'task_name': 'parameter-golf',
        'task_id': 'parameter-golf-61e11605',
        'task_hash': '61e11605',
        'task_version_id': 'parameter-golf-61e11605-v32',
        'trial_id': 'parameter-golf-61e11605-437',
        'override_reason': (
            'User-explicit override (Thu 2026-06-04): NonZeroAgentExitCodeError '
            '(exit 143 = SIGTERM). Verifier passed 7/8 checks (pass_rate=0.875, '
            'partial_score=1.0), full leaderboard data — agent trained a real '
            'LM (val_bpb=1.0806 vs threshold=0.983). Reward=0 came from the '
            'one held-out validation gate, not from infra.'
        ),
    },
    {
        'task_name': 'trimul-cuda',
        'task_id': 'trimul-cuda-fc34aaba',
        'task_hash': 'fc34aaba',
        'task_version_id': 'trimul-cuda-fc34aaba-v86',
        'trial_id': 'trimul-cuda-fc34aaba-566',
        'override_reason': (
            'User-explicit override (Thu 2026-06-04): NonZeroAgentExitCodeError '
            '(exit 1 — claude crashed late, not a timeout). Verifier ran kernel '
            'compile + cache probe + benchmark: cache_probe_passed=True, '
            'correctness_passed=False. $9.18 of real agent cost, 1.77 MB '
            'trajectory (largest of any v86 trial in cd4dd88f for this task), '
            '1.75 MB claude-code log. Reward=0 emitted from a fully-instrumented '
            'verifier run.'
        ),
    },
]

def main():
    src = make_src(); dst = make_dst()
    url = os.environ['ODDISH_DATABASE_URL'].replace('postgresql+asyncpg://','postgresql://')
    cur = psycopg2.connect(url + '?sslmode=require').cursor()

    state_path = '/tmp/oddish-import/state_nonzero_override.json'
    state = json.load(open(state_path)) if os.path.exists(state_path) else {'entries': {}}

    for spec in OVERRIDES:
        tid = spec['trial_id']
        if tid in state['entries']:
            print(f"  {tid}: already uploaded; skipping")
            continue
        cur.execute("""
          SELECT id, agent, model, provider, reward, status, attempts, max_attempts,
                 started_at, finished_at, input_tokens, cache_tokens, output_tokens,
                 cost_usd, has_trajectory, name, trial_s3_key, experiment_id
          FROM trials WHERE id=%s
        """, (tid,))
        r = cur.fetchone()
        (_id, agent, raw_model, provider, reward, status, att, mx, sa, fa, it, ct, ot,
         cu, ht, name, s3key, exp) = r
        # Normalize claude-code/opus-4-8 (the only model in this batch)
        if 'opus-4-8' in (raw_model or ''):
            norm_model = 'anthropic/claude-opus-4-8'
            norm_provider = 'bedrock' if raw_model.startswith(('global.','us.','bedrock/')) else (
                'openrouter' if raw_model.startswith('openrouter/') else (provider or 'bedrock'))
        else:
            norm_model = raw_model; norm_provider = provider
        cand = {
            'id': tid, 'agent': agent, 'model': norm_model, 'raw_model': raw_model,
            'provider': norm_provider, 'reward': reward, 'status': str(status).lower(),
            'attempts': att, 'max_attempts': mx,
            'started_at': sa.isoformat() if sa else None,
            'finished_at': fa.isoformat() if fa else None,
            'input_tokens': it, 'cache_tokens': ct, 'output_tokens': ot,
            'cost_usd': cu, 'has_trajectory': ht, 'source_trial_name': name,
            'trial_s3_key': s3key,
            'exception_classes': ['NonZeroAgentExitCodeError'],
            'selection_note': (
                'Accepted via user-explicit override despite NonZero exit. '
                'Verifier ran end-to-end and produced complete metrics; the trial '
                'is indistinguishable from a valid run except for the agent exit code.'
            ),
        }
        sel_meta = {'task_id': spec['task_id'], 'task_name': spec['task_name'],
                    'task_hash': spec['task_hash'],
                    'task_version_id': spec['task_version_id']}
        t0 = time.time()
        canonical, nb = upload_one_trial(src, dst, sel_meta, cand, spec['task_name'])
        if canonical is None:
            print(f"  {tid}: FAILED — no source keys")
            continue
        entry = build_entry(cand, sel_meta, canonical, spec['task_name'])
        entry['source_oddish_experiment_id'] = exp
        entry['raw_model'] = raw_model
        entry['override_validity'] = True
        entry['override_reason'] = spec['override_reason']
        state['entries'][tid] = {'entry': entry, 'task_name': spec['task_name']}
        with open(state_path, 'w') as f:
            json.dump(state, f, indent=2)
        print(f"  {tid}: uploaded {nb/1024/1024:.1f} MB in {time.time()-t0:.1f}s; "
              f"canonical={canonical}")

    # Augment each affected manifest
    by_task = {}
    for tid, s in state['entries'].items():
        by_task.setdefault(s['task_name'], []).append((tid, s['entry']))

    for tname, ups in by_task.items():
        m = json.loads(dst.get_object(Bucket=DEST_BUCKET, Key=f"{tname}/_manifest.json")['Body'].read())
        for tid, entry in ups:
            m['trials'][tid] = entry
        def _idx(t):
            tail = t.rsplit('-',1)[-1]
            return (int(tail) if tail.isdigit() else 1<<30, t)
        m['trials'] = dict(sorted(m['trials'].items(), key=lambda kv: _idx(kv[0])))
        m['n_trials'] = len(m['trials'])
        # Remove from tombstone list (these are now intentional, not tombstoned)
        if 'intentionally_tombstoned_trials' in m:
            m['intentionally_tombstoned_trials'] = sorted(
                set(m['intentionally_tombstoned_trials']) - {tid for tid,_ in ups}
            )
            if not m['intentionally_tombstoned_trials']:
                m.pop('intentionally_tombstoned_trials', None)
        addl = m.get('additional_imports') or []
        addl.append({
            'imported_at': datetime.now(timezone.utc).isoformat(),
            'source_oddish_experiment_ids': sorted({e['source_oddish_experiment_id'] for _,e in ups}),
            'n_trials_added': len(ups),
            'note': (
                'User-explicit override for NonZeroAgentExitCodeError trials '
                'whose verifier output is fully populated (real metrics.json, '
                'real reward, full trajectory, real agent cost). Same '
                'standard as the prior nextjs-vite-rewrite CC NonZero accepts.'
            ),
        })
        m['additional_imports'] = addl
        m['note'] = (m.get('note') or '') + (
            f" | {datetime.now(timezone.utc).isoformat()} added {len(ups)} "
            f"NonZero trial(s) as user-explicit override (full verifier metrics): "
            f"{', '.join(tid for tid,_ in ups)}."
        )
        body = json.dumps(m, indent=2, default=str).encode('utf-8')
        dst.put_object(Bucket=DEST_BUCKET, Key=f"{tname}/_manifest.json", Body=body)
        with open(f"/tmp/oddish-import/{tname}_manifest.json", 'w') as f:
            f.write(body.decode())
        pc = Counter((e['agent'], e['model']) for e in m['trials'].values())
        npass = sum(1 for ee in m['trials'].values() if ee.get('reward')==1.0)
        print(f"\n{tname} manifest n_trials={m['n_trials']}; new pair counts:")
        for p, n in sorted(pc.items()):
            print(f"  {p[0]:12s} / {p[1]:42s}  n={n}")

if __name__ == '__main__':
    main()
