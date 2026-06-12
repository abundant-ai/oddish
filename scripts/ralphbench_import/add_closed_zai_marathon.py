"""Closed-internet importer for s3://zai-swe-marathon/. Targets the 5 tasks
that were originally skipped by the open-internet gate:
  - embedding-eval
  - kubernetes-rust-rewrite
  - parameter-golf
  - trimul-cuda
  - zstd-decoder

Each of these tasks runs *only* with a tight Modal CIDR allowlist (1-3
prefixes) in e0ef4703 — there is no open-internet sibling variant. The
closed-task validity rule (after user correction of an earlier typo) is:

  status='SUCCESS' AND reward IS NOT NULL AND
  exception_classes ⊆ {'AgentTimeoutError'}

That is: any reward in [0.0, 1.0] is OK, exceptions must be empty OR only
AgentTimeoutError. NonZeroAgentExitCodeError / VerifierTimeoutError / etc.
are still rejected. This is the same rule the open-internet bucket uses;
the only differentiator between the two halves of the bucket is whether
the trial ran with a tight CIDR allowlist at runtime.

Bucket convention unchanged: cap at 5 trials per (agent, model) per task.
Each entry carries the runtime `network_policy` + `network_allowlist_size`
so downstream consumers can filter open vs closed separately.

NOTE: per user, the claude-code version-pin issue is being explicitly
ignored for this batch; agent here is glm-claude-code (correct wrapper)
regardless.
"""
from __future__ import annotations
import os, sys, json
import boto3, psycopg2
from botocore.config import Config
from collections import Counter, defaultdict
from datetime import datetime, timezone

DST_BUCKET = 'zai-swe-marathon'
DST_REGION = 'us-west-2'
EXPERIMENT_ID = 'e0ef4703'
AGENT = 'glm-claude-code'
MAX_PER_CELL = 5
# k8s was re-enabled by user (one strict-valid completion on v13).
CLOSED_TASKS = {'embedding-eval', 'kubernetes-rust-rewrite',
                'parameter-golf', 'trimul-cuda', 'zstd-decoder'}

sys.path.insert(0, os.path.dirname(__file__))
from import_zai_marathon import (
    make_zai_dst, upload_one_trial, build_entry,
    classify_network_policy, fetch_partial_score,
)
from augment_cd4dd88f import make_src, list_keys, pick_canonical

SRC_BUCKET = os.environ['ODDISH_S3_BUCKET']


VALID_EXC = {'AgentTimeoutError'}

def is_strict_closed_valid(reward, excs):
    """Closed-task validity rule (post-correction): same shape as the
    open-bucket strict rule — exception_classes ⊆ {AgentTimeoutError},
    any numeric reward. Reward=0 with no exception is now allowed."""
    return not (set(excs) - VALID_EXC)


def main():
    src = make_src()
    dst = make_zai_dst()

    url = os.environ['ODDISH_DATABASE_URL'].replace('postgresql+asyncpg://','postgresql://')
    cur = psycopg2.connect(url + '?sslmode=require').cursor()

    # Latest version per task
    cur.execute("""
      WITH per_task AS (
        SELECT task_id, task_version_id,
               CAST(SUBSTRING(task_version_id FROM 'v([0-9]+)$') AS int) AS vnum
        FROM trials WHERE experiment_id=%s AND agent=%s
          AND deleted_at IS NULL AND superseded_by_trial_id IS NULL
      )
      SELECT DISTINCT ON (task_id) task_id, task_version_id
      FROM per_task ORDER BY task_id, vnum DESC NULLS LAST
    """, (EXPERIMENT_ID, AGENT))
    latest = dict(cur.fetchall())

    # All SUCCESS+numeric trials for the 5 closed tasks
    cur.execute("""
      SELECT t.id, t.task_id, t.task_version_id, t.agent, t.model, t.provider,
             t.reward, t.status, t.attempts, t.max_attempts,
             t.started_at, t.finished_at,
             t.input_tokens, t.cache_tokens, t.output_tokens, t.cost_usd,
             t.has_trajectory, t.name, t.trial_s3_key, tk.name
      FROM trials t JOIN tasks tk ON tk.id=t.task_id
      WHERE t.experiment_id=%s AND t.agent=%s
        AND t.deleted_at IS NULL AND t.superseded_by_trial_id IS NULL
        AND t.status='SUCCESS' AND t.reward IS NOT NULL
        AND tk.name IN %s
    """, (EXPERIMENT_ID, AGENT, tuple(CLOSED_TASKS)))
    rows = cur.fetchall()

    by_task = defaultdict(list)
    for r in rows:
        (rid, tid, vid, agent, model, prov, reward, status, att, mx, sa, fa,
         it, ct, ot, cu, ht, name, s3key, task_name) = r
        if latest.get(tid) != vid: continue
        prefix = (s3key or f"tasks/{tid}/trials/{rid}/")
        if not prefix.endswith('/'): prefix += '/'
        try:
            res = json.loads(src.get_object(Bucket=SRC_BUCKET, Key=prefix+'result.json')['Body'].read())
            excs = set()
            for ev in (res.get('stats',{}).get('evals',{}) or {}).values():
                excs.update((ev.get('exception_stats') or {}).keys())
        except Exception:
            excs = {'__MISSING__'}
        if not is_strict_closed_valid(reward, excs):
            continue
        traj_max = 0
        for k, sz in list_keys(src, prefix):
            if k.endswith('agent/trajectory.json'):
                traj_max = max(traj_max, sz)
        by_task[(tid, task_name, vid)].append({
            'id': rid, 'agent': agent, 'model': model, 'raw_model': model,
            'provider': prov, 'reward': reward, 'status': str(status).lower(),
            'attempts': att, 'max_attempts': mx,
            'started_at': sa.isoformat() if sa else None,
            'finished_at': fa.isoformat() if fa else None,
            'input_tokens': it, 'cache_tokens': ct, 'output_tokens': ot,
            'cost_usd': cu, 'has_trajectory': ht, 'source_trial_name': name,
            'trial_s3_key': prefix,
            'trajectory_bytes': traj_max,
            'exception_classes': sorted(excs),
            'selection_note': (
                f"Imported from Oddish experiment {EXPERIMENT_ID} for agent={agent}, "
                f"model={model} on task version {vid}. CLOSED-INTERNET task variant "
                f"(Modal CIDR allowlist 1-3 prefixes). Stricter closed-task validity: "
                f"reward=1.0 OR (reward=0.0 AND exception_classes=={{AgentTimeoutError}}). "
                f"All other reward=0 cases rejected. Includes network_policy + "
                f"network_allowlist_size for downstream filtering."
            ),
        })

    # Existing bucket manifests (for the closed tasks where we already have
    # entries from the prior closed-task import round).
    existing_manifests = {}
    for tn in CLOSED_TASKS:
        try:
            body = dst.get_object(Bucket=DST_BUCKET,
                Key=f"{tn}/_manifest.json")['Body'].read()
            existing_manifests[tn] = json.loads(body)
        except Exception:
            existing_manifests[tn] = None

    state_path = '/tmp/oddish-import/state_zai_closed_v2.json'
    state = {'tasks': {}}
    if os.path.exists(state_path):
        state = json.load(open(state_path))

    summary = []
    for (tid, task_name, vid), cands in sorted(by_task.items()):
        # Already-imported trial IDs (from previous closed-task round)
        em = existing_manifests.get(task_name)
        existing_ids = set(em['trials'].keys()) if em else set()
        capacity = max(0, MAX_PER_CELL - len(existing_ids))
        if capacity == 0:
            summary.append((task_name, 0, f'capacity 0 (cell already {len(existing_ids)}/5)'))
            continue
        # Rank: reward=1.0 first, then trajectory_bytes, then recency
        def _ep(c):
            return datetime.fromisoformat(c['started_at']).timestamp() if c['started_at'] else 0
        cands.sort(key=lambda c: (
            0 if c['reward'] == 1.0 else 1,
            -float(c.get('trajectory_bytes') or 0),
            -_ep(c),
        ))
        # Exclude trials already in bucket
        cands = [c for c in cands if c['id'] not in existing_ids]
        picks = cands[:capacity]
        task_hash = tid.rsplit('-',1)[-1]
        sel_meta = {'task_id': tid, 'task_name': task_name,
                    'task_hash': task_hash, 'task_version_id': vid}
        ts = state['tasks'].setdefault(tid, {
            'task_name': task_name, 'task_hash': task_hash,
            'task_version_id': vid, 'new_entries': {}, 'failures': []})
        added = 0
        for p in picks:
            if p['id'] in ts['new_entries']:
                added += 1; continue
            src_keys = list_keys(src, p['trial_s3_key'])
            canon_src = pick_canonical(src, p['trial_s3_key'], src_keys)
            policy, allowlist_n, _ = classify_network_policy(
                src, p['trial_s3_key'], canon_src)
            try:
                canonical, _ = upload_one_trial(src, dst, sel_meta, p, task_name)
                if canonical is None:
                    ts['failures'].append({'trial_id': p['id'], 'reason': 'no keys'})
                    continue
                score = fetch_partial_score(src, p['trial_s3_key'], canonical)
                entry = build_entry(p, sel_meta, canonical, task_name,
                                    partial_score=score, network_policy=policy,
                                    network_allowlist_size=allowlist_n)
                ts['new_entries'][p['id']] = entry
                added += 1
                with open(state_path,'w') as f: json.dump(state, f, indent=2, default=str)
            except Exception as e:
                ts['failures'].append({'trial_id': p['id'], 'reason': str(e)})
                with open(state_path,'w') as f: json.dump(state, f, indent=2, default=str)

        if not ts['new_entries']:
            summary.append((task_name, 0, 'no strict-closed-valid picks'))
            continue

        # Merge with existing manifest if present, otherwise create fresh
        em = existing_manifests.get(task_name)
        if em is not None:
            merged = dict(em['trials'])
            merged.update(ts['new_entries'])
        else:
            merged = dict(ts['new_entries'])
        def _idx(t):
            tail = t.rsplit('-',1)[-1]
            return (int(tail) if tail.isdigit() else 1<<30, t)
        sorted_entries = dict(sorted(merged.items(), key=lambda kv: _idx(kv[0])))
        pair_counts = Counter((e['agent'], e['model']) for e in sorted_entries.values())
        n_pass = sum(1 for e in sorted_entries.values() if e.get('reward') == 1.0)
        n_timeout = sum(1 for e in sorted_entries.values()
                        if e.get('reward') == 0.0 and
                        set(e.get('exception_classes', [])) == {'AgentTimeoutError'})
        n_clean_fail = sum(1 for e in sorted_entries.values()
                           if e.get('reward') == 0.0 and
                           not set(e.get('exception_classes', [])))
        new_rule = ('exception_classes ⊆ {AgentTimeoutError} '
                    '(reward=0 with empty or AgentTimeoutError-only exceptions; '
                    'any reward=1; everything else rejected). Same shape as '
                    'the open-bucket strict rule — the only differentiator '
                    'between open- and closed-task halves of the bucket is the '
                    'runtime Modal CIDR allowlist size.')
        if em is None:
            manifest = {
                'experiment_id': EXPERIMENT_ID,
                'experiment_ids': [EXPERIMENT_ID],
                'note': (
                    f"Created at {datetime.now(timezone.utc).isoformat()} from "
                    f"Oddish experiment {EXPERIMENT_ID}. CLOSED-INTERNET task "
                    f"variant. Up to 5 trials matching the closed-task validity "
                    f"rule: {new_rule}"
                ),
                'task': tid, 'task_name': task_name,
                'task_hash': task_hash,
                'task_versions': [vid], 'task_hashes': [task_hash],
                'is_multi_version': False,
                'is_closed_internet_task': True,
                'duplicate_fingerprint_count': 0,
                'additional_imports': [],
            }
        else:
            manifest = dict(em)
            # bump the rule string + note since the rule was corrected
            old_note = manifest.get('note','').rstrip()
            if 'rule v2' not in old_note:
                manifest['note'] = (
                    old_note + '\n' +
                    f'Validity rule corrected on '
                    f'{datetime.now(timezone.utc).isoformat()} (rule v2): {new_rule}'
                )
        manifest['task_version_id'] = vid
        manifest['closed_task_validity_rule'] = new_rule
        manifest['n_trials'] = len(sorted_entries)
        manifest['pair_counts'] = {f"{a}|{m}": n for (a,m), n in pair_counts.items()}
        manifest['n_pass'] = n_pass
        manifest['n_timeout_failure'] = n_timeout
        manifest['n_clean_fail'] = n_clean_fail
        manifest['trials'] = sorted_entries
        manifest.setdefault('additional_imports', []).append({
            'imported_at': datetime.now(timezone.utc).isoformat(),
            'source_oddish_experiment_id': EXPERIMENT_ID,
            'task_version_id_at_snapshot': vid,
            'n_trials_added': len(ts['new_entries']),
            'pairs_added': [['glm-claude-code',
                              list(ts['new_entries'].values())[0]['model']]]
                            if ts['new_entries'] else [],
            'note': ('Closed-internet task under v2 closed-task validity rule '
                     '(exception_classes ⊆ {AgentTimeoutError}). Reward=0 with '
                     'no exception now allowed (previously rejected by a '
                     'rule-string typo).'),
        })
        body = json.dumps(manifest, indent=2, default=str).encode('utf-8')
        dst.put_object(Bucket=DST_BUCKET, Key=f"{task_name}/_manifest.json",
                       Body=body, ContentType='application/json')
        with open(f"/tmp/oddish-import/{task_name}_zai_closed_manifest.json",'w') as f:
            f.write(body.decode())
        summary.append((task_name, len(sorted_entries),
                        f"PASS={n_pass} TIMEOUT-fail={n_timeout}"))

    # Tasks that had zero valid picks
    for name in CLOSED_TASKS:
        if not any(s[0] == name for s in summary):
            summary.append((name, 0, 'no strict-closed-valid candidates'))

    print(f"\n=== Closed-internet import summary ===")
    print(f"{'task':24s} {'added':>5s}  detail")
    total = 0
    for tn, n, detail in sorted(summary):
        total += n
        print(f"  {tn:24s} {n:>5d}  {detail}")
    print(f"\nTotal new closed-internet trials uploaded: {total}")

if __name__ == '__main__':
    main()
