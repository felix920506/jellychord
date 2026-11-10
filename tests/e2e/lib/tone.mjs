// Tone detection for received Discord voice audio.
//
// The Jellyfin fixture library contains pure sine tones at distinct, non-harmonic
// frequencies. We decode what the listener bot receives to PCM and, for every
// short window, ask "which (if any) of the known tones is this?". That lets
// tests assert not just that *some* audio arrived but *which track* is playing
// and in what order.

export const SAMPLE_RATE = 48000;
export const CHANNELS = 2;
export const WINDOW_MS = 100;

// Generalised Goertzel: power of `freq` in `samples` (works for non-integer bins).
export function goertzelPower(samples, freq, rate = SAMPLE_RATE) {
  const w = (2 * Math.PI * freq) / rate;
  const coeff = 2 * Math.cos(w);
  let s1 = 0;
  let s2 = 0;
  for (let i = 0; i < samples.length; i++) {
    const s0 = samples[i] + coeff * s1 - s2;
    s2 = s1;
    s1 = s0;
  }
  return s1 * s1 + s2 * s2 - coeff * s1 * s2;
}

// Fraction (0..1) of the window's energy that sits at `freq`.
// A pure sine at exactly `freq` scores ~1, silence/noise/other tones ~0.
export function toneShare(samples, freq, rate = SAMPLE_RATE) {
  let energy = 0;
  for (let i = 0; i < samples.length; i++) energy += samples[i] * samples[i];
  if (energy === 0) return 0;
  return (2 * goertzelPower(samples, freq, rate)) / (samples.length * energy);
}

export function rms(samples) {
  let sum = 0;
  for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
  return samples.length ? Math.sqrt(sum / samples.length) : 0;
}

export function analyzeWindow(samples, candidates, { rate = SAMPLE_RATE, minRms = 0.01, minShare = 0.5 } = {}) {
  const level = rms(samples);
  let best = null;
  let bestShare = 0;
  if (level >= minRms) {
    for (const freq of candidates) {
      const share = toneShare(samples, freq, rate);
      if (share > bestShare) {
        bestShare = share;
        best = freq;
      }
    }
  }
  return { rms: level, tone: bestShare >= minShare ? best : null, share: bestShare };
}

// Interleaved signed 16-bit little-endian stereo PCM -> mono Float32 (-1..1).
export function pcmToMono(buffer, channels = CHANNELS) {
  const frames = Math.floor(buffer.length / (2 * channels));
  const out = new Float32Array(frames);
  for (let f = 0; f < frames; f++) {
    let sum = 0;
    for (let c = 0; c < channels; c++) sum += buffer.readInt16LE((f * channels + c) * 2);
    out[f] = sum / channels / 32768;
  }
  return out;
}

// Accumulates received PCM and keeps a timeline of what tone was heard when.
export class ToneTracker {
  constructor(candidates, { rate = SAMPLE_RATE, windowMs = WINDOW_MS, now = () => Date.now(), ...opts } = {}) {
    this.candidates = candidates;
    this.rate = rate;
    this.windowSize = Math.round((rate * windowMs) / 1000);
    this.now = now;
    this.opts = opts;
    this.pending = new Float32Array(0);
    this.timeline = []; // { t, tone, rms, share }
    this.framesReceived = 0;
    this.waiters = new Set();
  }

  pushPcm(buffer) {
    this.pushMono(pcmToMono(buffer));
  }

  pushMono(mono) {
    this.framesReceived += mono.length;
    const merged = new Float32Array(this.pending.length + mono.length);
    merged.set(this.pending);
    merged.set(mono, this.pending.length);
    let offset = 0;
    const t = this.now();
    while (merged.length - offset >= this.windowSize) {
      const win = merged.subarray(offset, offset + this.windowSize);
      this.timeline.push({ t, ...analyzeWindow(win, this.candidates, { rate: this.rate, ...this.opts }) });
      offset += this.windowSize;
    }
    this.pending = merged.slice(offset);
    for (const waiter of this.waiters) waiter();
  }

  // Collapse the timeline into runs of the same tone, dropping runs shorter
  // than `minWindows` (codec transitions, track boundaries).
  segments({ since = 0, minWindows = 3 } = {}) {
    const runs = [];
    for (const entry of this.timeline) {
      if (entry.t < since || entry.tone === null) continue;
      const last = runs[runs.length - 1];
      if (last && last.tone === entry.tone) {
        last.end = entry.t;
        last.windows++;
      } else {
        runs.push({ tone: entry.tone, start: entry.t, end: entry.t, windows: 1 });
      }
    }
    const kept = runs.filter((r) => r.windows >= minWindows);
    // merge neighbours that became adjacent after dropping short blips
    const merged = [];
    for (const run of kept) {
      const last = merged[merged.length - 1];
      if (last && last.tone === run.tone) {
        last.end = run.end;
        last.windows += run.windows;
      } else {
        merged.push({ ...run });
      }
    }
    return merged;
  }

  sequence(opts) {
    return this.segments(opts).map((s) => s.tone);
  }

  heard(tone, { since = 0, minWindows = 3 } = {}) {
    return this.segments({ since, minWindows }).some((s) => s.tone === tone);
  }

  // Windows with any recognised tone since `since`.
  toneWindowsSince(since) {
    return this.timeline.filter((e) => e.t >= since && e.tone !== null).length;
  }

  waitFor(predicate, timeoutMs, description) {
    return new Promise((resolve, reject) => {
      const check = () => {
        if (predicate()) {
          cleanup();
          resolve();
        }
      };
      const timer = setTimeout(() => {
        cleanup();
        reject(new Error(`timed out after ${timeoutMs} ms waiting for ${description}; heard ${JSON.stringify(this.sequence())}`));
      }, timeoutMs);
      const cleanup = () => {
        clearTimeout(timer);
        this.waiters.delete(check);
      };
      this.waiters.add(check);
      check();
    });
  }

  waitForTone(tone, timeoutMs, { since = 0, minWindows = 3 } = {}) {
    return this.waitFor(() => this.heard(tone, { since, minWindows }), timeoutMs, `${tone} Hz tone`);
  }
}
