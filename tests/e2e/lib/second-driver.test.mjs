import assert from 'node:assert/strict';
import { test } from 'node:test';

import { secondDriverProbeOutcome } from './second-driver.mjs';

const unsupported = new Error('bot raised while handling "drivers": UnknownCommand: drivers');

test('a probe that passes means supported', () => {
  assert.equal(secondDriverProbeOutcome(null, 'docker'), 'supported');
  assert.equal(secondDriverProbeOutcome(null, 'source'), 'supported');
});

test('an image without the drivers command skips', () => {
  assert.equal(secondDriverProbeOutcome(unsupported, 'docker'), 'skip');
});

test('the checkout without the drivers command fails', () => {
  assert.equal(secondDriverProbeOutcome(unsupported, 'source'), 'fail');
});

test('other probe failures on the image fail', () => {
  for (const message of [
    'timed out after 30000 ms waiting for reply to "drivers"',
    'bot raised while handling "drivers": UnknownCommand: driversx',
    'bot raised while handling "queue": UnknownCommand: drivers',
    'bot raised while handling "drivers": RuntimeError: boom',
  ]) {
    assert.equal(secondDriverProbeOutcome(new Error(message), 'docker'), 'fail', message);
  }
});
