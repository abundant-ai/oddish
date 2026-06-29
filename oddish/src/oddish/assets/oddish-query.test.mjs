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

test('solution fetch copies the tree into --into and reports the path', () => {
  const stage = mkStage();
  const dest = fs.mkdtempSync(path.join(os.tmpdir(), 'dest-'));
  const out = runStage(['solution', 'fetch', '--into', dest], stage);
  assert.match(out, /PROBE-ONLY/);
  assert.match(out, new RegExp(dest));
  assert.equal(fs.readFileSync(path.join(dest, 'a.txt'), 'utf8'), 'HELLO-SOLUTION');
});

test('harbor src reports unavailable when not staged', () => {
  const out = runStage(['harbor', 'src', '--into', os.tmpdir()], mkStage());
  assert.match(out, /PROBE-ONLY/);
  assert.match(out, /unavailable/);
});

test('solution fetch omits the boundary marker from the copied tree', () => {
  const stage = mkStage();
  fs.writeFileSync(path.join(stage, 'solution', '000-READ-ME-PROBE-ONLY.txt'), 'marker');
  const dest = fs.mkdtempSync(path.join(os.tmpdir(), 'dest-'));
  runStage(['solution', 'fetch', '--into', dest], stage);
  assert.ok(fs.existsSync(path.join(dest, 'a.txt')));
  assert.ok(!fs.existsSync(path.join(dest, '000-READ-ME-PROBE-ONLY.txt')));
});

test('verify run executes test.sh and returns parseable JSON with note banner', () => {
  const stage = fs.mkdtempSync(path.join(os.tmpdir(), 'probe-stage-'));
  // Fake verifier writes a reward file, like the real test.sh does.
  fs.writeFileSync(path.join(stage, 'test.sh'),
    '#!/bin/bash\nmkdir -p /tmp/vlog\necho RUNNING_VERIFIER\n');
  const out = runStage(['verify', 'run'], stage);
  const obj = JSON.parse(out);            // must be a single parseable object
  assert.match(obj.note, /PROBE-ONLY/);
  assert.equal(obj.exit, 0);
  assert.match(obj.build_log_tail, /RUNNING_VERIFIER/);
});
