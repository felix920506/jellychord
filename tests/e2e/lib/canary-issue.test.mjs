import assert from 'node:assert/strict';
import { test } from 'node:test';

import { keyOfIssue, normalizeReports, planIssueActions } from './canary-issue.mjs';
import { LAYERS } from './report.mjs';

const pass = () => ({ ok: true, diagnostics: {}, meta: {} });
const fail = (layer, extra = {}) => ({
  ok: false,
  failure: { layer, component: LAYERS[layer].component, title: LAYERS[layer].title, description: LAYERS[layer].description, step: 'some step', error: 'boom' },
  diagnostics: { discordStatus: { indicator: 'none', description: 'All Systems Operational', incidents: [] }, ...extra },
  meta: {},
});
const ctx = { runUrl: 'https://github.com/o/r/actions/runs/1', date: '2026-11-10' };

test('all passing: nothing to do, open issues get closed', () => {
  const reports = { image: pass(), locked: pass() };
  assert.deepEqual(planIssueActions({ reports, openIssues: [], ...ctx }), []);
  const actions = planIssueActions({ reports, openIssues: [{ number: 7, body: '<!-- e2e-canary-key: bot:voice-encryption -->' }], ...ctx });
  assert.equal(actions.length, 1);
  assert.equal(actions[0].type, 'close');
  assert.equal(actions[0].issue, 7);
});

test('released build broken: one user-facing issue', () => {
  const reports = { image: fail('voice-encryption'), locked: fail('voice-encryption') };
  const [action, ...rest] = planIssueActions({ reports, openIssues: [], ...ctx });
  assert.equal(rest.length, 0);
  assert.equal(action.type, 'create');
  assert.match(action.title, /^\[Automated\] Voice encryption/);
  assert.match(action.body, /opened automatically/);
  assert.match(action.body, /\*\*Workaround:\*\* None known yet\./);
  assert.equal(keyOfIssue(action), 'bot:voice-encryption');
  assert.deepEqual(action.labels, ['e2e-canary']);
});

test('existing issue for the same failure gets a comment instead of a duplicate', () => {
  const reports = { image: fail('audio-transport'), locked: fail('audio-transport') };
  const actions = planIssueActions({ reports, openIssues: [{ number: 3, body: 'x <!-- e2e-canary-key: bot:audio-transport --> y' }], ...ctx });
  assert.deepEqual(actions.map((a) => [a.type, a.issue]), [['comment', 3]]);
  assert.match(actions[0].body, /Still failing/);
});

test('harness failures are labelled separately', () => {
  const reports = { image: pass(), locked: fail('harness') };
  const [action] = planIssueActions({ reports, openIssues: [], ...ctx });
  assert.deepEqual(action.labels, ['e2e-canary', 'e2e-harness']);
  assert.doesNotMatch(action.body, /Workaround/);
});

test('missing reports count as harness failures and block auto-closing', () => {
  const reports = normalizeReports({ image: pass() }, ['image', 'locked']);
  assert.equal(reports.locked.failure.layer, 'harness');
  const actions = planIssueActions({ reports, openIssues: [{ number: 9, body: '<!-- e2e-canary-key: bot:gateway -->' }], ...ctx });
  assert.ok(!actions.some((a) => a.type === 'close'), 'must not close while a track is missing');
});

test('Discord incidents add the possible-outage label', () => {
  const outage = fail('voice-connect', { discordStatus: { indicator: 'major', description: 'Major outage', incidents: [{ name: 'Voice issues', status: 'investigating', url: 'https://stspg.io/x' }] } });
  const reports = { image: outage, locked: fail('voice-connect') };
  const [action] = planIssueActions({ reports, openIssues: [], ...ctx });
  assert.ok(action.labels.includes('possible-outage'));
  assert.match(action.body, /temporary Discord outage/);
  assert.match(action.body, /Voice issues/);
});
