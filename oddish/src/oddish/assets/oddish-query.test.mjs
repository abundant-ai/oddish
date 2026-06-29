import { test } from 'node:test';
import assert from 'node:assert';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

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

function runStage(args, stageDir) {
  const res = spawnSync('node', ['./oddish-query', ...args], {
    cwd: new URL('.', import.meta.url).pathname,
    env: { ...process.env, ODDISH_PROBE_STAGE_DIR: stageDir },
    encoding: 'utf8',
  });
  return res.stdout.trim();
}

function mkStage() {
  const stage = fs.mkdtempSync(path.join(os.tmpdir(), 'probe-stage-'));
  fs.mkdirSync(path.join(stage, 'solution'), { recursive: true });
  fs.writeFileSync(path.join(stage, 'solution', 'a.txt'), 'HELLO-SOLUTION');
  fs.writeFileSync(path.join(stage, 'test.sh'), '#!/bin/bash\necho SCORER\n');
  return stage;
}

test('solution cat prints the file behind the boundary banner', () => {
  const out = runStage(['solution', 'cat', 'a.txt'], mkStage());
  assert.match(out, /PROBE-ONLY/);
  assert.match(out, /HELLO-SOLUTION/);
  assert.ok(out.indexOf('PROBE-ONLY') < out.indexOf('HELLO-SOLUTION'));
});

test('verifier source prints test.sh behind the banner', () => {
  const out = runStage(['verifier', 'source'], mkStage());
  assert.match(out, /PROBE-ONLY/);
  assert.match(out, /SCORER/);
});

test('missing stage emits banner then error', () => {
  const out = runStage(['verifier', 'source'], '/nonexistent/stage/xyz');
  assert.match(out, /PROBE-ONLY/);
  assert.match(out, /unavailable/);
});

test('solution cat refuses path traversal', () => {
  const out = runStage(['solution', 'cat', '../test.sh'], mkStage());
  assert.match(out, /escapes|unavailable|no such/);
  assert.doesNotMatch(out, /SCORER/);
});
