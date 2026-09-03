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

test('task cat fetches a task source file without adding a solution prefix', () => {
  const out = runApi(['task', 'cat', 'instruction.md'],
    { '/tasks/task-123/files/instruction.md': { path: 'instruction.md', content: 'TASK-INSTRUCTIONS' } });
  assert.match(out, /PROBE-ONLY/);
  assert.match(out, /TASK-INSTRUCTIONS/);
});

test('task file reads use the pinned task version', () => {
  const out = runApi(
    ['task', 'cat', 'instruction.md'],
    {
      '/tasks/task-123/files/instruction.md?version=7': {
        path: 'instruction.md',
        content: 'VERSION-SEVEN',
      },
    },
    { ODDISH_PROBE_TASK_VERSION: '7' },
  );
  assert.match(out, /VERSION-SEVEN/);
});

test('task fetch downloads the complete source tree into --into', () => {
  const dest = fs.mkdtempSync(path.join(os.tmpdir(), 'task-dest-'));
  const out = runApi(['task', 'fetch', '--into', dest],
    { '/tasks/task-123/files': { files: [
        { path: 'instruction.md', content: 'TASK-INSTRUCTIONS' },
        { path: 'tests/test.sh', content: 'VERIFIER' } ] } });
  assert.match(out, /PROBE-ONLY/);
  assert.equal(fs.readFileSync(path.join(dest, 'instruction.md'), 'utf8'), 'TASK-INSTRUCTIONS');
  assert.equal(fs.readFileSync(path.join(dest, 'tests/test.sh'), 'utf8'), 'VERIFIER');
});

test('verifier source resolves tests/test.sh from the task listing', () => {
  const out = runApi(['verifier', 'source'],
    { '/tasks/task-123/files': { files: [{ path: 'tests/test.sh' }] },
      '/tasks/task-123/files/tests/test.sh': { path: 'tests/test.sh', content: '#!/bin/bash\necho SCORER\n' } });
  assert.match(out, /PROBE-ONLY/);
  assert.match(out, /SCORER/);
});

test('verifier source supports legacy root-level test.sh', () => {
  const out = runApi(['verifier', 'source'],
    { '/tasks/task-123/files': { files: [{ path: 'test.sh' }] },
      '/tasks/task-123/files/test.sh': { path: 'test.sh', content: 'LEGACY-SCORER' } });
  assert.match(out, /LEGACY-SCORER/);
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

test('verify run executes tests/test.sh discovered in the task listing', () => {
  const out = runApi(['verify', 'run'],
    { '/tasks/task-123/files': { files: [
        { path: 'tests/test.sh', content: '#!/bin/bash\necho RUNNING_VERIFIER\n' } ] } });
  const obj = JSON.parse(out);
  assert.match(obj.note, /PROBE-ONLY/);
  assert.equal(obj.exit, 0);
  assert.match(obj.build_log_tail, /RUNNING_VERIFIER/);
});

test('verify run supports a legacy root-level verifier and captures stderr', () => {
  const out = runApi(['verify', 'run'],
    { '/tasks/task-123/files': { files: [
        { path: 'test.sh', content: '#!/bin/bash\necho STDERR_MARKER >&2\n' } ] } });
  assert.match(JSON.parse(out).build_log_tail, /STDERR_MARKER/);
});

test('--help lists the command groups', () => {
  const out = runApi(['--help'], {});
  assert.match(out, /task cat/);
  assert.match(out, /task fetch/);
  assert.match(out, /solution/);
  assert.match(out, /verifier/);
  assert.match(out, /verify run/);
  assert.match(out, /harbor src/);
  assert.match(out, /trials result/);
  assert.match(out, /trials trajectory/);
  assert.match(out, /trials verifier/);
  assert.match(out, /trials logs/);
});

test('trial result prints the complete endpoint JSON without MAX_BYTES slicing', () => {
  const payload = { trial_results: [{ trial_name: 't-1', output: 'r'.repeat(24000) }] };
  const out = runApi(['trials', 'result', 't-1'], { '/trials/t-1/result': payload });
  assert.deepEqual(JSON.parse(out), { trial_id: 't-1', result: payload });
  assert.ok(out.length > 16000);
});

test('trial trajectory prints the complete endpoint JSON without tail slicing', () => {
  const payload = { trial_name: 't-1', steps: [{ message: 's'.repeat(24000) }] };
  const out = runApi(
    ['trials', 'trajectory', 't-1'],
    { '/trials/t-1/trajectory': payload },
  );
  assert.deepEqual(JSON.parse(out), { trial_id: 't-1', trajectory: payload });
  assert.ok(out.length > 16000);
});

test('trial verifier returns complete verifier streams with trial identity', () => {
  const payload = {
    verifier: { stdout: 'PASS\n', stderr: 'warning\n' },
    exception: null,
    agent: { setup: 'ignored by this bounded view' },
  };
  const out = runApi(
    ['trials', 'verifier', 't-1'],
    { '/trials/t-1/logs/structured': payload },
  );
  assert.deepEqual(JSON.parse(out), {
    trial_id: 't-1',
    verifier: payload.verifier,
    exception: null,
  });
});

test('trial verifier falls back to files when structured logs 500', () => {
  const out = runApi(
    ['trials', 'verifier', 't-1'],
    {
      '/trials/t-1/logs/structured': { __http: 500 },
      '/trials/t-1/files/verifier/test-stdout.txt': '{"reward":0,"status":"failed"}\n',
      '/trials/t-1/files/verifier/test-stderr.txt': { __http: 404 },
    },
  );
  assert.deepEqual(JSON.parse(out), {
    trial_id: 't-1',
    verifier: { stdout: '{"reward":0,"status":"failed"}\n', stderr: null },
    exception: null,
  });
});

test('trial logs retain log-specific truncation', () => {
  const payload = { logs: 'l'.repeat(24000) };
  const out = runApi(['trials', 'logs', 't-1'], { '/trials/t-1/logs': payload });
  assert.match(out, /truncated/);
  assert.ok(out.length < 16000);
});

test('the removed logs --trajectory option fails instead of changing resource meaning', () => {
  const out = runApi(['trials', 'logs', 't-1', '--trajectory'], {});
  assert.deepEqual(JSON.parse(out), { error: 'unknown option: --trajectory', status: 2 });
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
