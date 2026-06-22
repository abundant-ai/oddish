import { test } from 'node:test';
import assert from 'node:assert';
import { spawnSync } from 'node:child_process';

function run(args, fetchImpl) {
  const res = spawnSync('node', ['./oddish-query', ...args], {
    cwd: new URL('.', import.meta.url).pathname,
    env: { ...process.env, ODDISH_API_BASE_URL: 'http://x', ODDISH_API_KEY: 'k',
           ODDISH_QUERY_TEST_FIXTURE: JSON.stringify(fetchImpl) },
    encoding: 'utf8',
  });
  return res.stdout.trim();
}

test('experiments trials projects is_probe', () => {
  const out = run(['experiments', 'trials', 'exp1'],
    { '/experiments/exp1/trials': [
        { id: 't1', task_id: 'k', status: 'success', reward: 1, is_probe: false },
        { id: 't2', task_id: 'k', status: 'success', reward: 0, is_probe: true }] });
  const rows = out.split('\n').map(JSON.parse);
  assert.deepEqual(rows[0], { trial_id: 't1', task: 'k', status: 'success', reward: 1, is_probe: false });
  assert.equal(rows[1].is_probe, true);
});

test('401 → credential expired', () => {
  const out = run(['tasks', 'get', 'x'], { __status: 401 });
  assert.deepEqual(JSON.parse(out), { error: 'session credential expired', status: 401 });
});
