import assert from 'node:assert/strict';
import { test } from 'node:test';

import { SAMPLE_RATE, ToneTracker, analyzeWindow, pcmToMono, toneShare } from './tone.mjs';
import { TONES } from './fixtures.mjs';

const CANDIDATES = TONES.map((t) => t.freq);

function sine(freq, ms, amplitude = 0.125, rate = SAMPLE_RATE) {
  const n = Math.round((rate * ms) / 1000);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) out[i] = amplitude * Math.sin((2 * Math.PI * freq * i) / rate);
  return out;
}

function noise(ms, amplitude = 0.125, rate = SAMPLE_RATE) {
  const n = Math.round((rate * ms) / 1000);
  const out = new Float32Array(n);
  let seed = 12345;
  for (let i = 0; i < n; i++) {
    seed = (seed * 1103515245 + 12345) & 0x7fffffff;
    out[i] = amplitude * ((seed / 0x7fffffff) * 2 - 1);
  }
  return out;
}

function stereoPcm(mono) {
  const buf = Buffer.alloc(mono.length * 4);
  for (let i = 0; i < mono.length; i++) {
    const v = Math.round(mono[i] * 32767);
    buf.writeInt16LE(v, i * 4);
    buf.writeInt16LE(v, i * 4 + 2);
  }
  return buf;
}

test('fixture tones are distinct and non-harmonic', () => {
  for (const a of CANDIDATES) {
    for (const b of CANDIDATES) {
      if (a >= b) continue;
      assert.notEqual(b % a, 0, `${b} is a harmonic of ${a}`);
      assert.ok(b - a >= 100, `${a} and ${b} are too close`);
    }
  }
});

test('pure tone has share ~1 at its own frequency and ~0 at others', () => {
  const win = sine(440, 100);
  assert.ok(toneShare(win, 440) > 0.95);
  for (const f of CANDIDATES.filter((f) => f !== 440)) assert.ok(toneShare(win, f) < 0.05, `share at ${f}`);
});

test('analyzeWindow identifies each fixture tone', () => {
  for (const freq of CANDIDATES) {
    assert.equal(analyzeWindow(sine(freq, 100), CANDIDATES).tone, freq);
  }
});

test('analyzeWindow tolerates slight detuning from lossy transcoding', () => {
  assert.equal(analyzeWindow(sine(443, 100), CANDIDATES).tone, 440);
});

test('analyzeWindow rejects silence, quiet audio and noise', () => {
  assert.equal(analyzeWindow(new Float32Array(4800), CANDIDATES).tone, null);
  assert.equal(analyzeWindow(sine(440, 100, 0.001), CANDIDATES).tone, null);
  assert.equal(analyzeWindow(noise(100), CANDIDATES).tone, null);
});

test('pcmToMono decodes interleaved s16le stereo', () => {
  const mono = pcmToMono(stereoPcm(Float32Array.from([0.5, -0.5, 0])));
  assert.equal(mono.length, 3);
  assert.ok(Math.abs(mono[0] - 0.5) < 1e-3);
  assert.ok(Math.abs(mono[1] + 0.5) < 1e-3);
});

test('ToneTracker reports the order of tracks and ignores short blips', () => {
  let clock = 0;
  const tracker = new ToneTracker(CANDIDATES, { now: () => clock });
  const feed = (samples) => {
    // deliver in 20 ms opus-frame-sized chunks like the real receiver
    for (let i = 0; i < samples.length; i += 960) {
      clock += 20;
      tracker.pushPcm(stereoPcm(samples.subarray(i, i + 960)));
    }
  };
  feed(sine(440, 1000));
  feed(sine(970, 100)); // 1-window blip
  feed(sine(440, 500));
  feed(new Float32Array(48000)); // 1 s silence (track gap)
  feed(sine(620, 1000));
  assert.deepEqual(tracker.sequence(), [440, 620]);
  assert.ok(tracker.heard(620));
  assert.ok(!tracker.heard(970));
});

test('ToneTracker.waitForTone resolves when the tone arrives and rejects on timeout', async () => {
  const tracker = new ToneTracker(CANDIDATES);
  const waiting = tracker.waitForTone(620, 1000);
  tracker.pushMono(sine(620, 500));
  await waiting;
  await assert.rejects(tracker.waitForTone(970, 50), /timed out/);
});
