const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const compiled = ts.transpileModule(fs.readFileSync('src/utils/driverOperation.ts', 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020},
}).outputText;
const exported = {};
vm.runInNewContext(compiled, {exports: exported, setTimeout, clearTimeout});

(async () => {
  assert.equal(exported.launchErrorTitle('launch_failed'), 'operation_request_failed');
  assert.equal(exported.launchErrorTitle('already_installed'), 'install_button_installed');
  assert.equal(exported.launchErrorTitle('already_running'), 'toast_already_running_title');
  assert.equal(exported.launchErrorTitle('status_unavailable'), 'operation_status_unknown');
  assert.equal(exported.launchErrorTitle('connection_lost'), 'operation_status_unknown');
  let dispatches = 0;
  let recoveries = 0;
  const recover = async () => { recoveries++; };
  for (const code of ['already_running', 'launch_failed', 'invalid_version', 'status_unavailable']) {
    const reply = {started: false, code, message: 'Specific reason: ' + code};
    const result = await exported.requestOperation(async () => { dispatches++; return reply; }, recover);
    assert.equal(result, reply);
  }
  assert.equal(dispatches, 4);
  assert.equal(recoveries, 4);

  const lost = await exported.requestOperation(async () => {
    dispatches++;
    throw Error('Decky connection closed after sending the request');
  }, recover);
  assert.equal(lost.code, 'connection_lost');
  assert.equal(dispatches, 5, 'A lost reply must never resubmit the driver operation');
  assert.equal(recoveries, 5);

  const offline = await exported.requestOperation(async () => { throw Error('offline'); }, async () => { throw Error('still offline'); });
  assert.equal(offline.code, 'connection_lost');
  assert.equal(offline.started, false);

  const accepted = await exported.requestOperation(async () => ({started: true, code: null, message: null}), recover);
  assert.equal(accepted.started, true);
  assert.equal(recoveries, 5);

  let finish;
  const pending = new Promise(resolve => { finish = resolve; });
  await assert.rejects(exported.withTimeout(pending, 5), /timed out/);
  finish('operation continued');
  assert.equal(await pending, 'operation continued', 'A UI timeout must not cancel an accepted job');
  assert.equal(await exported.withTimeout(Promise.resolve('ready'), 100), 'ready');
  console.log('Driver operation UI tests passed: explicit failures, recovery, single dispatch, timeout, accepted job.');
})().catch(error => { console.error(error); process.exitCode = 1; });
