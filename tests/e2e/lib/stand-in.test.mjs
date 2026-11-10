import assert from 'node:assert/strict';
import { test } from 'node:test';

import { standInProbeOutcome } from './stand-in.mjs';

const unsupported = new Error('bot raised while handling "queue": ParseError: unknown option(s): e2e.as');

test('a probe that passes means supported', () => {
  assert.equal(standInProbeOutcome(null, 'docker'), 'supported');
  assert.equal(standInProbeOutcome(null, 'source'), 'supported');
});

test('an image without e2e.as skips', () => {
  assert.equal(standInProbeOutcome(unsupported, 'docker'), 'skip');
});

test('the checkout without e2e.as fails', () => {
  assert.equal(standInProbeOutcome(unsupported, 'source'), 'fail');
});

test('other probe failures on the image fail', () => {
  for (const message of [
    'timed out after 30000 ms waiting for reply to "queue"',
    'bot raised while handling "queue": ParseError: unknown option(s): bogus, e2e.as',
    'bot raised while handling "queue": ParseError: e2e.as must be a numeric user ID, got \'x\'',
    'expected reply matching Empty Queue, got: "Tracks in playlist:"',
  ]) {
    assert.equal(standInProbeOutcome(new Error(message), 'docker'), 'fail', message);
  }
});
