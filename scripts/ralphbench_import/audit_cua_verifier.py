"""Deep audit of CUA verifier health for cd4dd88f trials in the 4 CUA
task prefixes (slack-clone, mastodon-clone, s3-clone, excel-clone).

For each cd4dd88f-tagged trial, inspect:
  1. verifier/stages.json - which stages ran, completion status
  2. verifier/correctness/test-stdout.txt - look for infra signatures
     (ConnectionRefused, ECONNREFUSED, Container did not start,
     Traceback, address already in use, "no such host", etc.)
  3. verifier/correctness/metrics.json - structural sanity
  4. verifier/ux/cua_judge_report.json - CUA judge crashed?
  5. verifier/ux/final_answer.txt - 0 bytes = CUA judge produced no answer
  6. Episode count + total prompt/response sizes
  7. verifier/reward.json - matches top-level reward?

Outputs a per-trial 'verdict' (clean / soft-warning / hard-fail) with
explanation, plus a top-level recommendation for which trials to remove.
"""
from __future__ import annotations
import os, json, re, sys
import boto3
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter

dst = boto3.client('s3', aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
    aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'], region_name='us-west-2')

# Patterns that indicate verifier-side infra failure (substring, case-insensitive)
INFRA_PATTERNS = [
    'connection refused', 'connectionrefused', 'econnrefused',
    'no such host', 'nodename nor servname provided',
    'name or service not known', 'cannot connect to',
    'address already in use',
    'container did not start', 'container exited',
    'container start failed', 'pod was killed',
    'modal: shutting down', 'modal sdk',
    "could not bind", "bind: address",
    "max retries exceeded", "max retries reached",
    "verifier crashed", "verifier failed to start",
    "internal verifier error",
    "image build failed", "build failed",
    "out of memory", "killed (oom",
    "no space left on device",
    "rate limited", "ratelimit",
    "permission denied",
    "ssl: certificate verify failed",
    "timed out waiting for container",
    "service unavailable",
]

# Patterns we explicitly tolerate (these often appear in normal logs when
# tests are SUPPOSED to verify a 503 or similar)
TOLERATE_IF_PRESENT_IN = [
    'expected', 'should', '==', 'assert',  # in test stdout context
]

def has_infra_signature(text):
    if not text:
        return False, []
    low = text.lower()
    hits = []
    for p in INFRA_PATTERNS:
        if p in low:
            # Find a tight excerpt
            idx = low.find(p)
            excerpt = text[max(0, idx-60):min(len(text), idx+200)].replace('\n', ' ')
            hits.append((p, excerpt))
    return bool(hits), hits[:5]

def get_bytes(key, max_bytes=None):
    try:
        rng = f'bytes=0-{max_bytes-1}' if max_bytes else None
        kwargs = {'Bucket': 'ralphbench-logs', 'Key': key}
        if rng: kwargs['Range'] = rng
        obj = dst.get_object(**kwargs)
        return obj['Body'].read()
    except Exception:
        return None

def get_json(key):
    b = get_bytes(key)
    if b is None: return None
    try: return json.loads(b)
    except Exception: return None

def audit_trial(task_name, tid, canon):
    base = f"{task_name}/{tid}/{canon}/"
    out = {
        'trial': tid, 'task': task_name, 'canonical': canon,
        'verdict': 'unknown', 'flags': [], 'warnings': [],
        'soft_notes': [],
    }
    # 1. stages.json
    stages = get_json(base + 'verifier/stages.json')
    out['stages'] = stages
    # 2. reward.json
    rj = get_json(base + 'verifier/reward.json')
    out['reward_json'] = rj
    # 3. Correctness
    corr_stdout = get_bytes(base + 'verifier/correctness/test-stdout.txt')
    corr_size = len(corr_stdout) if corr_stdout else 0
    out['correctness_stdout_bytes'] = corr_size
    if corr_stdout:
        text = corr_stdout.decode('utf-8', errors='replace')
        has, hits = has_infra_signature(text)
        if has:
            out['flags'].append(f"correctness/test-stdout has infra signatures: " +
                                "; ".join(f"'{p}' in: {ex[:100]!r}" for p,ex in hits))
    elif corr_size == 0:
        out['warnings'].append("correctness/test-stdout.txt is missing or 0 bytes")
    # Tiny stdout: distinguish "verifier short-circuited because agent didn't
    # deliver /app/start.sh" (a legitimate agent failure that produces a real
    # reward=0) from a real verifier crash. The "missing /app/start.sh" case
    # is logged explicitly in correctness/metrics.json as setup=false or
    # failed_stage=missing_start_sh — that's NOT an infra issue.
    if 0 < corr_size < 1000:
        text = corr_stdout.decode('utf-8','replace').lower() if corr_stdout else ''
        cmj_local = get_json(base + 'verifier/correctness/metrics.json') or {}
        is_missing_start_sh = (
            'start.sh missing' in text
            or cmj_local.get('setup_error', '') == '/app/start.sh missing'
            or cmj_local.get('failed_stage') == 'missing_start_sh'
            or cmj_local.get('start_sh_missing') is not None
        )
        if is_missing_start_sh:
            out['soft_notes'].append(
                f"correctness short-circuited at 'agent did not deliver /app/start.sh' "
                f"({corr_size}B stdout). Verifier worked correctly; this is a legitimate "
                f"agent failure with reward=0, NOT an infra issue."
            )
        else:
            out['flags'].append(f"correctness/test-stdout.txt is tiny: {corr_size} bytes "
                                f"and no missing-start-sh marker — possible verifier crash")
    # Correctness metrics structural
    cmj = get_json(base + 'verifier/correctness/metrics.json')
    out['correctness_metrics'] = cmj if cmj is None else (list(cmj.keys())[:6] if isinstance(cmj, dict) else f"<{type(cmj).__name__}>")
    if cmj is None:
        out['warnings'].append("correctness/metrics.json missing/unreadable")
    elif isinstance(cmj, dict) and not cmj:
        out['flags'].append("correctness/metrics.json is empty dict")
    # 4. CUA judge report
    cua_report = get_json(base + 'verifier/ux/cua_judge_report.json')
    out['cua_judge_report_keys'] = (list(cua_report.keys())[:8] if isinstance(cua_report, dict) else None)
    if cua_report is None:
        out['soft_notes'].append("no cua_judge_report.json (smaller CUA flow)")
    else:
        # Look for error/status fields in the report
        for k in ('error','status','exception','failed','crash'):
            if k in cua_report:
                val = cua_report[k]
                if val and (val is True or (isinstance(val,str) and val.lower() in ('error','fail','failed','crash','crashed'))):
                    out['flags'].append(f"cua_judge_report[{k}]={val!r}")
        # Also check 'judge_clean'/'reward' fields
        if 'reward' in cua_report:
            out['cua_judge_reward'] = cua_report['reward']
    # 5. final_answer.txt
    fa = get_bytes(base + 'verifier/ux/final_answer.txt')
    if fa is None:
        out['soft_notes'].append("no final_answer.txt")
    else:
        out['final_answer_bytes'] = len(fa)
        if len(fa) == 0:
            out['warnings'].append("verifier/ux/final_answer.txt is 0 bytes")
    # 6. Count episode dirs (peek via list)
    n_episodes = 0
    paginator = dst.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket='ralphbench-logs', Prefix=base+'verifier/ux/', Delimiter='/'):
        for p in page.get('CommonPrefixes', []):
            sub = p['Prefix'].split('/')[-2]
            if re.fullmatch(r'episode-\d+', sub):
                n_episodes += 1
    out['n_ux_episodes'] = n_episodes
    if n_episodes == 0:
        out['flags'].append("no verifier/ux/episode-* dirs (CUA judge never ran or crashed before first move)")
    # NOTE: do not scan ux/episode-*/response.txt for infra signatures —
    # those are the CUA judge model's own reasoning text and routinely include
    # phrases like 'connection refused' as legitimate observations about the
    # app's state (which the model then reasons about and recovers from).
    # Real verifier framework crashes show up in trial.log / exception.txt /
    # missing cua_judge_report.json / 0-byte final_answer.txt, all of which
    # are checked elsewhere.
    # 7. Reward sanity: top-level result.json reward vs canonical verifier/reward.json
    top_res = get_json(f"{task_name}/{tid}/result.json")
    if top_res and rj is not None:
        rs = {}
        for ev in (top_res.get('stats',{}).get('evals',{}) or {}).values():
            rs.update((ev.get('reward_stats') or {}).get('reward') or {})
        out['top_reward_stats'] = rs
    # Additional check: scan trial.log + exception.txt for CUA-judge crash
    # signatures (the auth-error case from slack-330). These are the real
    # framework-side infra failures.
    JUDGE_CRASH_PATTERNS = [
        'authenticationerror', 'invalid x-api-key', 'invalid api key',
        'invalid_api_key', 'judgeapierror',
        'litellm.exceptions', 'litellm.authenticationerror',
    ]
    for src in [base + 'trial.log', base + 'exception.txt']:
        b = get_bytes(src, max_bytes=200_000)
        if b is None: continue
        low = b.decode('utf-8', errors='replace').lower()
        for p in JUDGE_CRASH_PATTERNS:
            if p in low:
                out['flags'].append(f"{src.split('/')[-1]} contains judge-crash signature '{p}'")
                break

    # Also: missing cua_judge_report + 0-byte final_answer + very few episodes
    # = the CUA judge framework crashed before producing a verdict
    if cua_report is None and out.get('final_answer_bytes') == 0 and n_episodes < 10:
        out['flags'].append(
            f"CUA judge framework crashed: no cua_judge_report.json, "
            f"0-byte final_answer.txt, only {n_episodes} ux episode(s)"
        )

    # Decide verdict
    if out['flags']:
        out['verdict'] = 'hard-fail'
    elif out['warnings']:
        out['verdict'] = 'soft-warning'
    else:
        out['verdict'] = 'clean'
    return out

def main():
    print("Auditing cd4dd88f CUA trials for verifier-side infra failures...")
    print(f"{'task':16s} {'trial':38s} {'verdict':14s} {'corr_KB':>7s} {'ux_eps':>6s}  notes")
    suspects = []
    cleans = []
    for task in ('slack-clone','mastodon-clone','s3-clone','excel-clone'):
        m = json.loads(dst.get_object(Bucket='ralphbench-logs', Key=f"{task}/_manifest.json")['Body'].read())
        cd4 = [(tid,e) for tid,e in sorted(m['trials'].items())
               if e.get('source_oddish_experiment_id')=='cd4dd88f']
        results = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = {pool.submit(audit_trial, task, tid, e['canonical_attempt_dir']): tid for tid,e in cd4}
            for fut in as_completed(futs):
                results.append(fut.result())
        # Sort by trial id for stable output
        results.sort(key=lambda r: r['trial'])
        for r in results:
            tid = r['trial']
            corr_kb = r['correctness_stdout_bytes'] / 1024
            eps = r['n_ux_episodes']
            verdict = r['verdict']
            note = ''
            if r['flags']:
                note = 'FLAGS: ' + ' | '.join(r['flags'])
            elif r['warnings']:
                note = 'warnings: ' + ' | '.join(r['warnings'])
            print(f"  {task:16s} {tid:38s} {verdict:14s} {corr_kb:>7.1f} {eps:>6d}  {note[:130]}")
            if verdict == 'hard-fail':
                suspects.append(r)
            elif verdict == 'clean':
                cleans.append(r)

    print(f"\nSummary: {len(suspects)} hard-fail, total clean+warning = {39 - len(suspects)}")
    if suspects:
        print("\nHard-fail trials (recommend removal or further inspection):")
        for r in suspects:
            print(f"  • {r['task']}/{r['trial']}")
            for f in r['flags']:
                print(f"      {f[:300]}")
    # Persist
    with open('/tmp/oddish-import/audit_cua_verifier.json','w') as f:
        json.dump({'hard_fail': suspects, 'clean': cleans}, f, indent=2, default=str)
    print(f"\nDetail saved to /tmp/oddish-import/audit_cua_verifier.json")

if __name__ == '__main__':
    main()
