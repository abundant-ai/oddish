"""Audit every trial dir for content artifacts (trajectory, metrics, reward).

For each of the 1499 trials, check whether the canonical attempt subdir
contains:
  - agent/trajectory.json (non-empty)
  - verifier/reward.txt or verifier/reward.json
  - verifier/metrics.json (or for CUA tasks: verifier/correctness/metrics.json
    + verifier/ux/cua_judge_report.json)
  - presence of a partial-score-like field in metrics

Strategy: do one recursive listing per task prefix (cheap) instead of
1499 head_object calls. Then for the canonical task-* subdir of each
trial, classify the artifact set.
"""
from __future__ import annotations
import os, json, boto3, re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

dst = boto3.client('s3', aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
    aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'], region_name='us-west-2')

TASKS = [
    'biofabric-rust-rewrite','embedding-eval','excel-clone',
    'find-network-alignments','jax-pytorch-rewrite','kubernetes-rust-rewrite',
    'mastodon-clone','nextjs-vite-rewrite','parameter-golf',
    'post-train-ifeval','ruby-rust-port','rust-c-compiler','rust-java-lsp',
    's3-clone','slack-clone','stripe-clone','trimul-cuda',
    'vliw-kernel-optimization','wasm-simd','zstd-decoder',
]
# Tasks that MAY use the two-stage CUA verifier layout
# (verifier/correctness/ + verifier/ux/) in addition to the standard
# verifier/ layout. Some older entries in these prefixes still use the
# standard layout, so we accept either.
CUA_CAPABLE_TASKS = {'slack-clone','mastodon-clone','s3-clone','excel-clone'}
# NOTE: stripe-clone uses the standard layout, not the CUA layout.

def list_task_keys(task):
    """Return {trial_id: {rel_path_under_trial: size}} for every trial."""
    out = defaultdict(dict)
    paginator = dst.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket='ralphbench-logs', Prefix=f"{task}/"):
        for o in page.get('Contents', []):
            rel = o['Key'][len(task)+1:]   # strip "task/"
            if '/' not in rel: continue    # task-level files (like _manifest.json)
            trial_id, sub = rel.split('/', 1)
            out[trial_id][sub] = o['Size']
    return out

def find_canonical(trial_files, manifest_canon):
    """Find the canonical Harbor attempt subdir. Two known naming
    conventions are accepted:

      - 'task-<task-name>-<random>/' (current Oddish/Harbor convention)
      - '<task-name>__<random>/'      (older / alternative convention,
        seen on kubernetes-rust-rewrite-ecef12ce-* and
        nextjs-vite-rewrite-c6cb4791-*)

    Both produce config.json + result.json + verifier/ subdirs at the
    same relative paths.
    """
    if manifest_canon:
        for k in trial_files:
            if k.startswith(manifest_canon + '/'): return manifest_canon
    # Find every subdir that has its own result.json — that's a canonical-shaped attempt.
    candidates = set()
    for k in trial_files:
        if '/' in k:
            top = k.split('/', 1)[0]
            # The two known naming conventions
            if top.startswith('task-') or '__' in top:
                # And it must contain a result.json
                if (top + '/result.json') in trial_files:
                    candidates.add(top)
    if len(candidates) == 1: return next(iter(candidates))
    if candidates: return sorted(candidates)[-1]
    return None

def classify_trial(task, tid, trial_files, manifest_canon, agent):
    canon = find_canonical(trial_files, manifest_canon)
    if not canon:
        return {'verdict': 'NO_CANONICAL_TASK_DIR'}
    cp = canon + '/'
    F = trial_files  # files dict
    def has(rel, min_size=1):
        return F.get(cp+rel, -1) >= min_size
    traj_size = F.get(cp+'agent/trajectory.json', 0)
    # nop/oracle agents don't run a model so they legitimately don't have a
    # trajectory; treat them as 'expected absence'.
    if agent in ('nop', 'oracle'):
        has_traj = True   # not applicable
    else:
        has_traj = traj_size > 100

    # Reward — multiple possible locations
    has_reward = has('verifier/reward.txt') or has('verifier/reward.json')

    # Metrics — accept either the standard or CUA two-stage layout.
    has_std_metrics = has('verifier/metrics.json')
    has_cua_metrics = has('verifier/correctness/metrics.json')
    has_metrics = has_std_metrics or has_cua_metrics
    if has_std_metrics:
        metrics_path = cp + 'verifier/metrics.json'
    elif has_cua_metrics:
        metrics_path = cp + 'verifier/correctness/metrics.json'
    else:
        metrics_path = None
    has_ux = has('verifier/ux/cua_judge_report.json') if task in CUA_CAPABLE_TASKS else None

    return {
        'verdict': 'check',
        'canonical': canon,
        'agent': agent,
        'traj_size': traj_size,
        'has_traj': has_traj,
        'has_reward': has_reward,
        'has_metrics': has_metrics,
        'has_ux_report': has_ux,
        'metrics_path': metrics_path if has_metrics else None,
    }

def fetch_metrics(metrics_path):
    """Read metrics.json and return whether it has a numeric score field."""
    try:
        m = json.loads(dst.get_object(Bucket='ralphbench-logs', Key=metrics_path)['Body'].read())
    except Exception as e:
        return None, f"read error: {e}"
    if not isinstance(m, dict):
        return None, "not a dict"
    candidates = ('partial_score','score','accuracy','binary_strict',
                  'pass_rate','observed_s3','primary_alignment')
    for k in candidates:
        if k in m and isinstance(m[k], (int, float)):
            return m[k], None
    # If reward field exists in metrics:
    if 'reward' in m and isinstance(m['reward'], (int, float)):
        return m['reward'], 'reward-as-score'
    return None, f"no score field; keys={list(m.keys())[:8]}"

# Walk every task
print(f"{'task':24s} {'n':>5s} {'no_canon':>9s} {'no_traj':>8s} {'no_reward':>10s} {'no_metrics':>11s} {'no_score':>9s}")
gross_missing = []
for task in TASKS:
    tfiles = list_task_keys(task)
    m = json.loads(dst.get_object(Bucket='ralphbench-logs', Key=f"{task}/_manifest.json")['Body'].read())
    trials = m['trials']
    n_total = 0; n_no_canon = 0; n_no_traj = 0; n_no_reward = 0; n_no_metrics = 0; n_no_score = 0
    no_score_examples = []
    # Read each trial's classification
    for tid, e in trials.items():
        n_total += 1
        agent = e['agent']
        files = tfiles.get(tid, {})
        if not files:
            n_no_canon += 1
            gross_missing.append((task, tid, 'no S3 keys'))
            continue
        info = classify_trial(task, tid, files, e.get('canonical_attempt_dir'), agent)
        if info['verdict'] == 'NO_CANONICAL_TASK_DIR':
            n_no_canon += 1
            gross_missing.append((task, tid, 'no canonical'))
            continue
        if not info['has_traj']:
            n_no_traj += 1
        if not info['has_reward']:
            n_no_reward += 1
        if not info['has_metrics']:
            n_no_metrics += 1
            if len(no_score_examples) < 3:
                no_score_examples.append((tid, 'metrics absent'))
    # Verify score field in metrics.json for ALL trials. Skip nop/oracle
    # since their metrics.json legitimately has no score (just a reward
    # marker).
    sample = []
    for tid, e in trials.items():
        if e['agent'] in ('nop','oracle'): continue
        files = tfiles.get(tid, {})
        if not files: continue
        info = classify_trial(task, tid, files, e.get('canonical_attempt_dir'), e['agent'])
        if info.get('metrics_path'):
            full_key = f"{task}/{tid}/{info['metrics_path']}"
            sample.append((tid, full_key))
    if sample:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(fetch_metrics, p): tid for tid, p in sample}
            for fut in as_completed(futs):
                score, err = fut.result()
                if score is None:
                    n_no_score += 1
                    if len(no_score_examples) < 4:
                        no_score_examples.append((futs[fut], err[:60]))
    # Print row
    print(f"  {task:24s} {n_total:>5d} {n_no_canon:>9d} {n_no_traj:>8d} {n_no_reward:>10d} {n_no_metrics:>11d} {n_no_score:>9d}"
          + (f"   examples: {no_score_examples[:2]}" if (n_no_traj or n_no_reward or n_no_metrics or n_no_score) else ""))

if gross_missing:
    print()
    print(f"Gross missing trials: {len(gross_missing)}")
    for task, tid, why in gross_missing[:10]:
        print(f"  {task}/{tid}: {why}")
