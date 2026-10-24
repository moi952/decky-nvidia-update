const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const compiled = ts.transpileModule(fs.readFileSync('src/useControllerScroll.ts', 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020},
}).outputText;
let cleanup, callback, frame, cancelled = 0, unregistered = 0;
const enabled = [], exported = {};
const input = {
  RegisterForControllerAnalogInputMessages(fn) {callback = fn; return {unregister() {unregistered++;}};},
  EnableControllerAnalogInputMessages(value) {enabled.push(value);},
};
vm.runInNewContext(compiled, {
  exports: exported,
  window: {SteamClient: {Input: input}},
  requestAnimationFrame(fn) { frame = fn; return 1; },
  cancelAnimationFrame() { cancelled++; },
  require(name) {
    if (name === 'react') return {useEffect(fn) {cleanup = fn();}};
    if (name === '@decky/ui') return {findModuleExport: () => ({GAMEPAD_ANALOG_SCROLL: 47, GAMEPAD_RIGHTSTICK_CLICK: 41})};
    throw Error(name);
  },
});
const view = {current: {scrollTop: 1000}};
exported.useControllerScroll(view, false);
assert.deepEqual(enabled, [], 'Live/unfocused mode must not claim analog input');
exported.useControllerScroll(view, true);
assert.deepEqual(enabled, [true]);
const emit = (y, controller = 0, button = 47) => callback(controller, button, false, 0, y);
emit(-5000); // An existing cumulative position must not jump on activation.
assert.equal(frame, undefined);
emit(-5010); emit(-5020);
assert.equal(view.current.scrollTop, 1000, 'Batch high-frequency input until the animation frame');
frame();
assert.equal(view.current.scrollTop, 993, 'Integrate differences, not cumulative values');
emit(-5000); frame();
assert.equal(view.current.scrollTop, 1000, 'Right-stick reversal scrolls back down');
emit(-30000, 0, 48); emit(NaN); emit(Infinity);
emit(9000, 1); // A different controller gets its own baseline.
assert.equal(view.current.scrollTop, 1000);
emit(9010, 1); frame();
assert.equal(view.current.scrollTop, 1003.5);
emit(0, 0); // A reset in the cumulative stream must not jump the log.
assert.equal(view.current.scrollTop, 1003.5);
emit(-20, 0);
cleanup();
frame(); emit(-40, 0);
assert.equal(view.current.scrollTop, 1003.5, 'No queued or late callback may scroll a closed viewer');
assert.equal(unregistered, 1);
assert.equal(cancelled, 1);
assert.deepEqual(enabled, [true, false], 'Release analog input on blur, Live mode or close');
console.log('Controller scroll passed: cumulative Steam input, reversal, batching, separate baselines, invalid/reset input, cleanup.');
