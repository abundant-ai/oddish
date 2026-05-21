"""End-to-end validation of uploaded data per the user's checklist."""
import os, json, boto3
from collections import Counter
from botocore.config import Config

DEST_BUCKET = "ralphbench-logs"
dst = boto3.client('s3',
    aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
    aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'],
    region_name='us-west-2',
    config=Config(max_pool_connections=16))

def head(key):
    try:
        dst.head_object(Bucket=DEST_BUCKET, Key=key)
        return True
    except Exception:
        return False

def main():
    overall_ok = True
    for task_name in ('mastodon-clone', 'slack-clone'):
        print(f"=== {task_name} ===")
        # Load manifest
        obj = dst.get_object(Bucket=DEST_BUCKET, Key=f"{task_name}/_manifest.json")
        manifest = json.loads(obj['Body'].read())
        trials = manifest['trials']
        print(f"  manifest n_trials = {manifest['n_trials']}, len(trials) = {len(trials)}, match = {manifest['n_trials']==len(trials)}")
        print(f"  task_version_id = {manifest['task_version_id']}")
        print(f"  task_hash = {manifest['task_hash']}")

        pair_counter = Counter()
        n_pass = 0
        n_numeric = 0
        seen_fp = {}
        dup_count = 0
        missing_top_cfg = 0
        missing_top_res = 0
        missing_canonical = 0
        bad_prefix = 0
        wrong_taskmeta = 0

        for tid, e in trials.items():
            pair_counter[(e['agent'], e['model'])] += 1
            if isinstance(e['reward'], (int, float)):
                n_numeric += 1
                if e['reward'] == 1.0:
                    n_pass += 1
            # dedup fingerprint
            fp = (e['agent'], e['model'], e['reward'], e['started_at'], e['finished_at'], e.get('source_trial_name'))
            if fp in seen_fp:
                dup_count += 1
            else:
                seen_fp[fp] = tid

            # Check artifact prefix matches
            expected_prefix = f"s3://{DEST_BUCKET}/{task_name}/{tid}/"
            if e['artifact_prefix'] != expected_prefix:
                bad_prefix += 1
            # Check artifact_prefix exists (top-level config/result)
            base = f"{task_name}/{tid}/"
            if not head(base + 'config.json'):
                missing_top_cfg += 1
            if not head(base + 'result.json'):
                missing_top_res += 1
            canon = e.get('canonical_attempt_dir')
            if canon and not head(base + canon + '/result.json'):
                missing_canonical += 1
            # required fields
            for k in ('agent','model','provider','reward','status','attempts','max_attempts','task_version_id','started_at','finished_at','origin','artifact_layout','source_trial_name','source_oddish_task_id','source_oddish_trial_id','source_oddish_experiment_id'):
                if k not in e:
                    print(f"   MISSING FIELD: trial {tid} missing {k}")
                    overall_ok = False

        print(f"  numeric reward: {n_numeric}/{len(trials)}  pass count (reward=1.0): {n_pass}")
        print(f"  duplicate fingerprint count: {dup_count}")
        print(f"  missing top-level config: {missing_top_cfg}, result: {missing_top_res}, canonical: {missing_canonical}")
        print(f"  artifact_prefix mismatches: {bad_prefix}")
        print(f"  pair counts:")
        # Expected 13 pairs
        expected_pairs = [
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
        ]
        for p in expected_pairs:
            c = pair_counter.get(p, 0)
            mark = 'OK' if c == 5 else f'GAP {c}/5'
            print(f"    {p[0]:12s} / {p[1]:40s}  {c}  {mark}")
        if dup_count or missing_top_cfg or missing_top_res or missing_canonical or bad_prefix:
            overall_ok = False
        # Also verify task_name/task_hash/task_version_id appear in top-level config/result for one trial
        sample_tid = next(iter(trials))
        obj = dst.get_object(Bucket=DEST_BUCKET, Key=f"{task_name}/{sample_tid}/config.json")
        cfg = json.loads(obj['Body'].read())
        obj = dst.get_object(Bucket=DEST_BUCKET, Key=f"{task_name}/{sample_tid}/result.json")
        res = json.loads(obj['Body'].read())
        print(f"  sample {sample_tid}/config.json task_name={cfg.get('task_name')} task_hash={cfg.get('task_hash')} task_version_id={cfg.get('task_version_id')}")
        print(f"  sample {sample_tid}/result.json task_name={res.get('task_name')} task_hash={res.get('task_hash')} task_version_id={res.get('task_version_id')}")

    print()
    print('OVERALL:', 'OK' if overall_ok else 'ERRORS')

main()
