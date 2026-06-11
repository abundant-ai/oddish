"""Full audit of s3://zai-swe-marathon/ — per trial check for:
  - forbidden exception classes (anything beyond AgentTimeoutError, unless
    override_validity=true)
  - infra signatures in trial.log (litellm auth errors, container start
    failures, connection refused, etc.)
  - CUA verifier health on the fullstack/CUA tasks (slack-clone,
    mastodon-clone, s3-clone, excel-clone, stripe-clone): correctness/
    metrics.json populated, ux/cua_judge_report.json present, UX
    episode count, final_answer.txt non-empty, no corrupted metrics keys
  - standard verifier health on the other tasks: verifier/metrics.json
    has a real score field, verifier/reward.txt or reward.json present,
    agent/trajectory.json non-trivial size
  - network policy still 'open (no allowlist)' for every entry
"""
from __future__ import annotations
import os, json, boto3, re
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter, defaultdict

sess = boto3.session.Session(profile_name='zai', region_name='us-west-2')
dst = sess.client('s3')
BUCKET = 'zai-swe-marathon'

VALID_EXC = {'AgentTimeoutError'}
CUA_TASKS = {'slack-clone','mastodon-clone','s3-clone','excel-clone','stripe-clone'}

JUDGE_INFRA_PATTERNS = [
    'authenticationerror','invalid x-api-key','invalid api key',
    'invalid_api_key','judgeapierror','litellm.exceptions',
    'litellm.authenticationerror','judge crashed','judge failed',
]
GENERIC_INFRA_PATTERNS = [
    'connection refused','econnrefused','no such host',
    'address already in use','container did not start',
    'container exited','container start failed','pod was killed',
    'modal: shutting down','image build failed','build failed',
    'out of memory','no space left on device','rate limited',
    'ratelimit','timed out waiting for container','service unavailable',
]

def get_bytes(key, max_bytes=None):
    try:
        kw = {'Bucket': BUCKET, 'Key': key}
        if max_bytes: kw['Range'] = f'bytes=0-{max_bytes-1}'
        return dst.get_object(**kw)['Body'].read()
    except Exception:
        return None

def get_json(key):
    b = get_bytes(key)
    if b is None: return None
    try: return json.loads(b)
    except Exception: return None

def head(key):
    try: dst.head_object(Bucket=BUCKET, Key=key); return True
    except Exception: return False

def search_infra(text, patterns):
    if not text: return None
    low = text.lower()
    for p in patterns:
        i = low.find(p)
        if i >= 0:
            return p, text[max(0,i-50):i+150].replace('\n',' ')
    return None

def audit_trial(task_name, tid, entry):
    canon = entry.get('canonical_attempt_dir')
    base = f"{task_name}/{tid}/"
    cp = base + canon + '/'
    flags = []
    notes = []

    # 1. Manifest-level: exception_classes
    excs = entry.get('exception_classes', []) or []
    bad = set(excs) - VALID_EXC
    if bad and not entry.get('override_validity'):
        flags.append(f"forbidden exception_classes (no override): {sorted(bad)}")

    # 2. Re-verify by reading top-level result.json (in case manifest is stale)
    res = get_json(base + 'result.json')
    if res:
        live_excs = set()
        for ev in (res.get('stats',{}).get('evals',{}) or {}).values():
            live_excs.update((ev.get('exception_stats') or {}).keys())
        live_bad = live_excs - VALID_EXC
        if live_bad and not entry.get('override_validity'):
            flags.append(f"top-level result.json has forbidden excs (no override): {sorted(live_bad)}")

    # 3. trial.log scan for infra signatures
    tl = get_bytes(cp + 'trial.log', max_bytes=200_000)
    if tl is None:
        flags.append("trial.log missing")
    else:
        text = tl.decode('utf-8','replace')
        hit = search_infra(text, JUDGE_INFRA_PATTERNS)
        if hit:
            flags.append(f"trial.log judge-infra signature '{hit[0]}': {hit[1][:100]!r}")
        hit = search_infra(text, GENERIC_INFRA_PATTERNS)
        if hit:
            notes.append(f"trial.log generic-infra signature '{hit[0]}': {hit[1][:100]!r}")

    # 4. exception.txt — if present and has a real stack
    et = get_bytes(cp + 'exception.txt', max_bytes=100_000)
    if et:
        text = et.decode('utf-8','replace')
        hit = search_infra(text, JUDGE_INFRA_PATTERNS)
        if hit:
            flags.append(f"exception.txt judge-infra: '{hit[0]}'")

    # 5. Network policy. Open is expected for normal tasks; tasks explicitly
    #    flagged is_closed_internet_task in the manifest are allowed to carry
    #    a closed allowlist and get a note instead of a flag.
    pol = entry.get('network_policy') or ''
    is_closed_task = entry.get('_is_closed_internet_task', False)
    if not pol.startswith('open'):
        if is_closed_task:
            notes.append(f"closed-internet task: {pol!r} "
                         f"(allowlist_size={entry.get('network_allowlist_size')})")
        else:
            flags.append(f"network_policy not open: {pol!r}")

    # 6. Verifier health
    is_cua = task_name in CUA_TASKS
    if is_cua:
        # CUA two-stage check (correctness + ux). Some older imports have only
        # standard verifier/, but for zai-swe-marathon these are all cd4dd88f-
        # style? Actually no, e0ef4703 might use either layout. Check both.
        has_correctness = head(cp+'verifier/correctness/metrics.json')
        has_ux_report = head(cp+'verifier/ux/cua_judge_report.json')
        has_std_metrics = head(cp+'verifier/metrics.json')
        if has_correctness:
            cm = get_json(cp+'verifier/correctness/metrics.json')
            if cm is None or not isinstance(cm, dict):
                flags.append("CUA: correctness/metrics.json missing or unparseable")
            elif any(k in ('-','false','true','null') for k in cm.keys()):
                flags.append(f"CUA: corrupted correctness/metrics.json keys: {list(cm.keys())[:6]}")
            # UX side
            if not has_ux_report:
                flags.append("CUA: ux/cua_judge_report.json missing")
            fa = get_bytes(cp+'verifier/ux/final_answer.txt')
            if fa is None:
                flags.append("CUA: ux/final_answer.txt missing")
            elif len(fa) == 0:
                flags.append("CUA: ux/final_answer.txt is 0 bytes (judge produced no answer)")
            # Count episodes
            n_eps = 0
            for page in dst.get_paginator('list_objects_v2').paginate(
                Bucket=BUCKET, Prefix=cp+'verifier/ux/', Delimiter='/'):
                for p in page.get('CommonPrefixes', []):
                    if p['Prefix'].split('/')[-2].startswith('episode-'):
                        n_eps += 1
            if n_eps == 0:
                flags.append("CUA: 0 ux episodes")
            elif n_eps < 5:
                notes.append(f"CUA: only {n_eps} ux episodes (light judge run)")
        elif has_std_metrics:
            # stripe-clone is a fullstack task that intentionally ships only a
            # correctness/pytest verifier and no UX/CUA judge — not a layout
            # bug. Verify the standard metrics.json carries rich pytest data.
            m = get_json(cp+'verifier/metrics.json')
            if not isinstance(m, dict) or not m:
                flags.append("standard verifier: metrics.json empty or unparseable")
            elif 'pytest' not in m and not any(k.endswith('_gate') for k in m):
                flags.append(f"standard verifier: thin metrics, keys={list(m.keys())[:6]}")
            else:
                gates = [k for k in m if k.endswith('_gate')]
                pyct = m.get('pytest',{})
                notes.append(f"correctness-only verifier (no CUA judge): {len(gates)} gates, pytest categories={len(pyct)}, partial_score={m.get('partial_score')}")
        else:
            flags.append("CUA: no verifier metrics found (neither correctness/ nor std)")
    else:
        # Standard verifier check
        m = get_json(cp+'verifier/metrics.json')
        ts = get_bytes(cp+'verifier/test-stdout.txt')
        if m is None:
            # Some task verifiers (biofabric-rust-rewrite) never emit metrics.json
            # by design — they emit reward.txt + test-stdout.txt with counts in text.
            # Others (rust-c-compiler hitting anti-cheat) short-circuit before
            # emitting metrics. Both are legitimate task outputs, not infra
            # failures. Note rather than flag if a substantial test-stdout exists.
            if ts and len(ts) > 1000 and head(cp+'verifier/reward.txt'):
                notes.append(f"verifier/metrics.json absent (task uses reward.txt+test-stdout layout, {len(ts)}B stdout)")
            else:
                flags.append("verifier/metrics.json missing AND no substantial test-stdout fallback")
        elif not isinstance(m, dict):
            flags.append("verifier/metrics.json not a dict")
        else:
            score_keys = ('partial_score','score','binary_strict','pass_rate','accuracy')
            has_score = any(k in m and isinstance(m[k],(int,float)) for k in score_keys)
            if not has_score and 'reward' not in m:
                flags.append(f"verifier/metrics.json has no score field; keys={list(m.keys())[:6]}")
        # reward file
        if not (head(cp+'verifier/reward.txt') or head(cp+'verifier/reward.json')):
            flags.append("no verifier/reward.{txt,json}")
        # trajectory
        traj_key = cp+'agent/trajectory.json'
        if entry['agent'] not in ('nop','oracle'):
            try:
                sz = dst.head_object(Bucket=BUCKET, Key=traj_key)['ContentLength']
                if sz < 100:
                    flags.append(f"trajectory.json suspiciously tiny ({sz}B)")
            except Exception:
                flags.append("agent/trajectory.json missing")

    return {
        'task': task_name, 'trial': tid, 'agent': entry['agent'],
        'reward': entry['reward'], 'verdict': 'CLEAN' if not flags else 'FLAG',
        'flags': flags, 'notes': notes,
    }

def main():
    # Discover tasks
    prefixes = []
    for page in dst.get_paginator('list_objects_v2').paginate(Bucket=BUCKET, Delimiter='/'):
        for p in page.get('CommonPrefixes', []):
            prefixes.append(p['Prefix'].rstrip('/'))
    all_jobs = []
    for tn in prefixes:
        m = get_json(f"{tn}/_manifest.json")
        if not m: continue
        is_closed = bool(m.get('is_closed_internet_task', False))
        for tid, e in m['trials'].items():
            e = dict(e)
            e['_is_closed_internet_task'] = is_closed
            all_jobs.append((tn, tid, e))

    print(f"Auditing {len(all_jobs)} trials across {len(prefixes)} tasks...\n")
    results = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(audit_trial, *j): j for j in all_jobs}
        for fut in as_completed(futs):
            results.append(fut.result())
    results.sort(key=lambda r: (r['task'], r['trial']))

    # Per-trial output
    print(f"{'task':24s} {'trial':38s} {'verdict':7s}  flags/notes")
    flagged = 0
    by_task = defaultdict(lambda: {'n':0,'flag':0,'notes':0})
    for r in results:
        line = f"  {r['task']:24s} {r['trial']:38s} {r['verdict']:7s}"
        if r['flags']:
            flagged += 1
            by_task[r['task']]['flag'] += 1
            for f in r['flags']:
                print(line + f"  FLAG: {f[:130]}")
                line = ' ' * len(line)
        elif r['notes']:
            by_task[r['task']]['notes'] += 1
            for n in r['notes']:
                print(line + f"  note: {n[:130]}")
                line = ' ' * len(line)
        else:
            print(line + '  ✓')
        by_task[r['task']]['n'] += 1

    print()
    print(f"=== Summary ===")
    print(f"  {flagged} flagged / {len(results)} total ({len(results)-flagged} clean or note-only)")
    for tn in prefixes:
        d = by_task[tn]
        if d['flag'] or d['notes']:
            print(f"  {tn:24s} n={d['n']:>2d}  flagged={d['flag']}  noted={d['notes']}")

if __name__ == '__main__':
    main()
