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

function runApi(args, fixture, extraEnv) {
  const res = spawnSync('node', ['./oddish-query', ...args], {
    cwd: new URL('.', import.meta.url).pathname,
    env: { ...process.env, ODDISH_API_BASE_URL: 'http://x', ODDISH_API_KEY: 'k',
           ODDISH_PROBE_TASK_ID: 'task-123',
           ODDISH_QUERY_TEST_FIXTURE: JSON.stringify(fixture), ...(extraEnv || {}) },
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

test('solution cat fetches file content from the API behind the banner', () => {
  const out = runApi(['solution', 'cat', 'a.txt'],
    { '/tasks/task-123/files/solution/a.txt': { path: 'solution/a.txt', content: 'HELLO-SOLUTION' } });
  assert.match(out, /PROBE-ONLY/);
  assert.match(out, /HELLO-SOLUTION/);
  assert.ok(out.indexOf('PROBE-ONLY') < out.indexOf('HELLO-SOLUTION'));
});

test('solution cat without task id dies clearly', () => {
  const out = runApi(['solution', 'cat', 'a.txt'], {}, { ODDISH_PROBE_TASK_ID: '' });
  assert.match(out, /task id/i);
});


test('harbor src reports the repo+ref it fetches and the destination', () => {
  const dest = fs.mkdtempSync(path.join(os.tmpdir(), 'harbor-'));
  const out = runApi(['harbor', 'src', '--into', dest], {},
    { ODDISH_PROBE_HARBOR_REPO: 'owner/repo', ODDISH_PROBE_HARBOR_REF: 'abc123',
      ODDISH_QUERY_HARBOR_DRYRUN: '1' });
  assert.match(out, /PROBE-ONLY/);
  assert.match(out, /owner\/repo/);
  assert.match(out, /abc123/);
  assert.match(out, new RegExp(dest));
});

test('harbor src without a pin dies clearly', () => {
  const out = runApi(['harbor', 'src'], {},
    { ODDISH_PROBE_HARBOR_REPO: '', ODDISH_PROBE_HARBOR_REF: '' });
  assert.match(out, /harbor (repo|ref)/i);
});

test('verifier source prints test.sh from the API', () => {
  const out = runApi(['verifier', 'source'],
    { '/tasks/task-123/files/test.sh': { path: 'test.sh', content: '#!/bin/bash\necho SCORER\n' } });
  assert.match(out, /PROBE-ONLY/);
  assert.match(out, /SCORER/);
});

test('solution fetch downloads the solution tree into --into', () => {
  const dest = fs.mkdtempSync(path.join(os.tmpdir(), 'dest-'));
  const out = runApi(['solution', 'fetch', '--into', dest],
    { '/tasks/task-123/files': { files: [
        { path: 'solution/a.txt', content: 'HELLO-SOLUTION' },
        { path: 'solution/sub/b.txt', content: 'NESTED' },
        { path: 'tests/test.sh', content: 'IGNORED' } ] } });
  assert.match(out, /PROBE-ONLY/);
  assert.match(out, new RegExp(dest));
  assert.equal(fs.readFileSync(path.join(dest, 'a.txt'), 'utf8'), 'HELLO-SOLUTION');
  assert.equal(fs.readFileSync(path.join(dest, 'sub', 'b.txt'), 'utf8'), 'NESTED');
  assert.ok(!fs.existsSync(path.join(dest, 'test.sh')));  // only solution/ subtree
});

test('verify run materializes test.sh from the API, runs it, returns JSON', () => {
  const out = runApi(['verify', 'run'],
    { '/tasks/task-123/files': { files: [
        { path: 'test.sh', content: '#!/bin/bash\necho RUNNING_VERIFIER\n' } ] } });
  const obj = JSON.parse(out);
  assert.match(obj.note, /PROBE-ONLY/);
  assert.equal(obj.exit, 0);
  assert.match(obj.build_log_tail, /RUNNING_VERIFIER/);
});

test('verify run captures stderr in build_log_tail', () => {
  const out = runApi(['verify', 'run'],
    { '/tasks/task-123/files': { files: [
        { path: 'test.sh', content: '#!/bin/bash\necho STDERR_MARKER >&2\n' } ] } });
  assert.match(JSON.parse(out).build_log_tail, /STDERR_MARKER/);
});

test('--help lists the command groups', () => {
  const out = runApi(['--help'], {});
  assert.match(out, /solution/);
  assert.match(out, /verifier/);
  assert.match(out, /verify run/);
  assert.match(out, /harbor src/);
  assert.match(out, /trials logs/);
});

test('per-command help explains usage', () => {
  const out = runApi(['solution', '--help'], {});
  assert.match(out, /solution (cat|fetch)/);
});

test('no stage-dir env is referenced', () => {
  const src = fs.readFileSync(new URL('./oddish-query', import.meta.url).pathname, 'utf8');
  assert.doesNotMatch(src, /ODDISH_PROBE_STAGE_DIR/);
  assert.doesNotMatch(src, /requireStage|stagePath/);
});
