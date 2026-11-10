import assert from 'node:assert/strict';
import { test } from 'node:test';

import { Driver } from './driver.mjs';

// No login: only the control-message handling is exercised.
function makeDriver() {
  const driver = new Driver({
    token: 'x.y.z', guildId: 'g', controlChannelId: 'control', voiceChannelId: 'voice',
    sutId: 'sut', nonce: 'run1', toneCandidates: [440],
  });
  const announceReady = () =>
    driver.onMessage({ author: { id: 'sut' }, channelId: 'control', content: '<@driver> e2e run1 - ready', components: [] });
  return { driver, announceReady };
}

test('waitForSutReady waits for the given number of ready announcements', async (t) => {
  const { driver, announceReady } = makeDriver();
  t.after(() => driver.client.destroy());

  announceReady();
  await driver.waitForSutReady(100);
  let second = false;
  const waiting = driver.waitForSutReady(1000, 2).then(() => { second = true; });
  await new Promise((r) => setImmediate(r));
  assert.equal(second, false);
  announceReady();
  await waiting;
  assert.equal(second, true);
  assert.equal(driver.readyWaiters.size, 0);
});

test('a timed-out waiter is dropped and does not affect a newer one', async (t) => {
  const { driver, announceReady } = makeDriver();
  t.after(() => driver.client.destroy());

  await assert.rejects(driver.waitForSutReady(10, 1), /timed out/);
  assert.equal(driver.readyWaiters.size, 0);
  const waiting = driver.waitForSutReady(1000, 2);
  announceReady();
  assert.equal(driver.readyWaiters.size, 1);
  announceReady();
  await waiting;
});
