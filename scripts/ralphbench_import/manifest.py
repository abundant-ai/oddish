"""Build and upload _manifest.json for a task once all trial dirs are in place."""
import os, json, sys, time
import boto3
from botocore.config import Config

DEST_BUCKET = "ralphbench-logs"
DEST_REGION = "us-west-2"

def make_dst_client():
    return boto3.client('s3',
        aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'],
        region_name=DEST_REGION,
        config=Config(max_pool_connections=8, retries={'max_attempts':5,'mode':'standard'}))

def head_object(dst, key):
    try:
        dst.head_object(Bucket=DEST_BUCKET, Key=key)
        return True
    except Exception:
        return False

def validate_trial(dst, task_name, trial_id, entry):
    """Return list of validation errors for one trial (empty = ok)."""
    errs = []
    prefix = f"{task_name}/{trial_id}/"
    # top-level config + result
    if not head_object(dst, prefix + 'config.json'):
        errs.append('missing top-level config.json')
    if not head_object(dst, prefix + 'result.json'):
        errs.append('missing top-level result.json')
    # canonical attempt dir
    canon = entry.get('canonical_attempt_dir')
    if canon:
        if not head_object(dst, prefix + canon + '/config.json'):
            errs.append(f'missing canonical config.json under {canon}/')
        if not head_object(dst, prefix + canon + '/result.json'):
            errs.append(f'missing canonical result.json under {canon}/')
    # reward must be numeric
    if not isinstance(entry.get('reward'), (int,float)):
        errs.append('reward is not numeric')
    return errs

def main():
    only_task = sys.argv[1] if len(sys.argv) > 1 else None
    state = json.load(open('/tmp/oddish-import/state.json'))
    dst = make_dst_client()

    for task_id, ts in state['tasks'].items():
        if only_task and task_id != only_task:
            continue
        task_name = ts['task_name']
        task_hash = ts['task_hash']
        task_version_id = ts['task_version_id']
        trials = ts['trials']
        # Validate
        all_errs = {}
        print(f"=== Validating {task_id} ({len(trials)} trials)")
        for tid, entry in trials.items():
            errs = validate_trial(dst, task_name, tid, entry)
            if errs:
                all_errs[tid] = errs
        if all_errs:
            print(f"  Validation errors: {len(all_errs)} trials")
            for tid, errs in list(all_errs.items())[:10]:
                print(f"   {tid}: {errs}")
            sys.exit(2)

        # Build manifest
        from collections import Counter
        pair_counts = Counter()
        n_pass = 0
        for tid, e in trials.items():
            pair_counts[(e['agent'], e['model'])] += 1
            if e.get('reward') == 1.0:
                n_pass += 1
        # Duplicate-fingerprint check
        seen = {}
        dup_count = 0
        for tid, e in trials.items():
            fp = (e['agent'], e['model'], e['reward'], e['started_at'], e['finished_at'], e.get('source_trial_name'))
            if fp in seen:
                dup_count += 1
            else:
                seen[fp] = tid

        # Build manifest payload
        # Strip canonical_attempt_dir from manifest entries since RalphBench convention doesn't include it.
        # Keep it embedded as informational metadata.
        manifest_trials = {}
        for tid, e in trials.items():
            mt = dict(e)
            manifest_trials[tid] = mt
        # Stable ordering by trial id
        manifest_trials = dict(sorted(manifest_trials.items(),
            key=lambda kv: (int(kv[0].rsplit('-',1)[-1]) if kv[0].rsplit('-',1)[-1].isdigit() else 0, kv[0])))

        # Determine note (shortfall info)
        notes = []
        if dup_count:
            notes.append(f"duplicate fingerprint count: {dup_count}")
        pair_n = {f"{a}|{m}": pair_counts.get((a,m),0) for (a,m) in [
            ('claude-code','anthropic/claude-opus-4-7'),
            ('codex','openai/gpt-5.5'),
            ('gemini-cli','gemini/gemini-3.1-pro-preview'),
            ('kimi-cli','openrouter/moonshotai/kimi-k2.6'),
            ('terminus-2','anthropic/claude-opus-4-7'),
            ('terminus-2','openai/gpt-5.5'),
            ('terminus-2','gemini/gemini-3.1-pro-preview'),
            ('terminus-2','openrouter/moonshotai/kimi-k2.6'),
            ('terminus-2','openrouter/deepseek/deepseek-v4-pro'),
            ('terminus-2','openrouter/z-ai/glm-5.1'),
            ('terminus-2','openrouter/minimax/minimax-m2.7'),
            ('nop','default'),
            ('oracle','default'),
        ]}
        for pk, n in pair_n.items():
            if n < 5:
                notes.append(f"shortfall {pk}: {n}/5")

        from datetime import datetime, timezone
        note = (
            f"Generated at {datetime.now(timezone.utc).isoformat()} from Oddish "
            f"experiment cd8c33d8. Contains up to 5 valid trials per task/agent/model "
            f"pair (numeric verifier reward), ranked by reward then trajectory_bytes "
            f"then total artifact bytes then recency."
        )
        if notes:
            note += " Notes: " + "; ".join(notes) + "."

        manifest = {
            'experiment_id': 'cd8c33d8',
            'n_trials': len(manifest_trials),
            'note': note,
            'task': task_id,
            'task_name': task_name,
            'task_hash': task_hash,
            'task_version_id': task_version_id,
            'pair_counts': {f"{a}|{m}": n for (a,m), n in pair_counts.items()},
            'n_pass': n_pass,
            'duplicate_fingerprint_count': dup_count,
            'trials': manifest_trials,
        }
        # Upload
        body = json.dumps(manifest, indent=2, sort_keys=False, default=str).encode('utf-8')
        dst.put_object(Bucket=DEST_BUCKET, Key=f"{task_name}/_manifest.json", Body=body, ContentType='application/json')
        print(f"  Uploaded s3://{DEST_BUCKET}/{task_name}/_manifest.json ({len(body)} bytes; n_trials={len(manifest_trials)}, n_pass={n_pass})")
        for pk, n in pair_n.items():
            mark = 'OK' if n == 5 else f"SHORTFALL {n}/5"
            print(f"    {pk:60} {mark}")
        # Save the local copy for reference
        with open(f'/tmp/oddish-import/{task_name}_manifest.json','w') as f:
            f.write(body.decode())

if __name__ == '__main__':
    main()
