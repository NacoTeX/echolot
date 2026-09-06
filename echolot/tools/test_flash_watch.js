// Unit tests for the pure half of app/static/flash_watch.js.
// Run: node tools/test_flash_watch.js

const assert = require("node:assert/strict");
const path = require("node:path");

const {
  flashSignature,
  describeFlashState,
} = require(path.join(__dirname, "..", "app", "static", "flash_watch.js"));

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("the two steps the dialog cannot tell apart get different titles", () => {
  const handshake = describeFlashState({ state: "initializing" }, 0);
  const download = describeFlashState({ state: "preparing" }, 0);
  assert.notEqual(handshake.title, download.title);
  assert.match(handshake.title, /Chip/);
  assert.match(download.title, /Firmware/);
});

test("no hint while a step is still young", () => {
  const view = describeFlashState({ state: "initializing" }, 5);
  assert.equal(view.hint, "");
  assert.equal(view.detail, "");
});

test("a stalled handshake points at the download mode, not at the network", () => {
  const view = describeFlashState({ state: "initializing" }, 25);
  assert.match(view.hint, /Download-Modus/);
  assert.match(view.hint, /BOOT/);
  assert.match(view.detail, /25\u00a0s/);
});

test("a stalled download points at the file, not at the chip", () => {
  const view = describeFlashState({ state: "preparing" }, 25);
  assert.match(view.hint, /Firmware herunterladen/);
  assert.doesNotMatch(view.hint, /BOOT/);
});

test("erasing is given two minutes before it is called stalled", () => {
  assert.equal(describeFlashState({ state: "erasing" }, 60).hint, "");
  assert.match(describeFlashState({ state: "erasing" }, 130).hint, /zwei/);
});

test("writing shows its percentage", () => {
  const view = describeFlashState(
    { state: "writing", details: { percentage: 42 } },
    3,
  );
  assert.match(view.title, /42\u00a0%/);
});

test("progress resets the stall clock, a frozen percentage does not", () => {
  const a = flashSignature({ state: "writing", details: { percentage: 10 } });
  const b = flashSignature({ state: "writing", details: { percentage: 11 } });
  const c = flashSignature({ state: "writing", details: { percentage: 11 } });
  assert.notEqual(a, b);
  assert.equal(b, c);
});

test("an error is reported with the dialog's own message", () => {
  const view = describeFlashState(
    { state: "error", message: "Failed to connect with the device" },
    1,
  );
  assert.equal(view.tone, "err");
  assert.match(view.detail, /Failed to connect/);
});

test("a state this file does not know renders nothing", () => {
  assert.equal(describeFlashState({ state: "manifest" }, 99), null);
  assert.equal(describeFlashState(undefined, 1), null);
  assert.equal(describeFlashState({}, 1), null);
  assert.equal(flashSignature(undefined), "");
});

let failed = 0;
for (const [name, fn] of tests) {
  try {
    fn();
    console.log(`ok   ${name}`);
  } catch (err) {
    failed += 1;
    console.error(`FAIL ${name}\n     ${err.message}`);
  }
}
console.log(`\n${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
