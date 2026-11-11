import assert from 'node:assert/strict';
import { test } from 'node:test';

import { Redactor } from './redact.mjs';

// Token-shaped fixtures are assembled at runtime so no token-like literal sits
// in the source (secret scanners would rightly flag it). They are fake.
const fakeToken = (seed) =>
  [Buffer.from('123456789012345678').toString('base64'), `G${seed}bcde`, `${seed}`.repeat(27) + 'XyZ09'].join('.');
const KNOWN = fakeToken('a');
const UNKNOWN = fakeToken('q');

const r = new Redactor({ 'sut-token': KNOWN, 'guild-id': '111111111111111111' });

test('replaces known secrets by name', () => {
  assert.equal(r.text('guild 111111111111111111 joined'), 'guild [guild-id] joined');
  assert.equal(r.text(`t=${KNOWN}`), 't=[sut-token]');
});

test('catches unknown Discord tokens', () => {
  assert.equal(r.text(`x ${UNKNOWN} y`), 'x [discord-token] y');
});

test('redacts voice session ids in every spelling', () => {
  for (const line of [
    `"session_id":"08a840e77ddfcba1256153c929b4a961"`,
    `'session_id': '08a840e77ddfcba1256153c929b4a961'`,
    `"sessionId":"08a840e77ddfcba1256153c929b4a961"`,
    `ApiKey=96c1d01f68df41a193dc64f2fe3607eb&x=1`,
    `"token":"abcdef123456"`,
  ]) {
    assert.doesNotMatch(r.text(line), /08a840e7|96c1d01f|abcdef123456/, line);
  }
});

test('redacts voice transport secret keys logged as arrays, objects and buffers', () => {
  for (const line of [
    `{"op":4,"d":{"secret_key":[251,7,171,208,69],"mode":"x"}}`,
    `'secret_key': [189, 22, 7, 99]`,
    `"secretKey":{"0":251,"1":7,"2":171}`,
    `secretKey: <Buffer fb 07 ab d0>`,
  ]) {
    const out = r.text(line);
    assert.doesNotMatch(out, /251|189|fb 07/, `${line} -> ${out}`);
    assert.match(out, /\[redacted\]/);
  }
});

test('json() keeps structure valid while redacting nested strings', () => {
  const input = { ok: true, n: 3, log: [`'secret_key': [189, 22]`, `"sessionId":"08a840e77ddfcba1256153c929b4a961"`], meta: { guild: '111111111111111111' } };
  const out = r.json(input);
  assert.equal(out.ok, true);
  assert.equal(out.n, 3);
  assert.doesNotMatch(JSON.stringify(out), /189|08a840e7|111111111111111111/);
  assert.deepEqual(JSON.parse(JSON.stringify(out)), out);
});
