import assert from 'node:assert/strict';
import { test } from 'node:test';

import { issueKeys, keyOfIssue, normalizeReports, planIssueActions, renderIssue } from './canary-issue.mjs';
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
  const reports = { image: pass(), locked: pass(), latest: pass() };
  assert.deepEqual(planIssueActions({ reports, openIssues: [], ...ctx }), []);
  const actions = planIssueActions({ reports, openIssues: [{ number: 7, body: '<!-- e2e-canary-key: bot:voice-encryption -->' }], ...ctx });
  assert.equal(actions.length, 1);
  assert.equal(actions[0].type, 'close');
  assert.equal(actions[0].issue, 7);
});

test('released build broken: one user-facing issue with workaround when latest passes', () => {
  const reports = { image: fail('voice-encryption'), locked: fail('voice-encryption'), latest: pass() };
  const [action, ...rest] = planIssueActions({ reports, openIssues: [], ...ctx });
  assert.equal(rest.length, 0);
  assert.equal(action.type, 'create');
  assert.match(action.title, /^\[Automated\] Voice encryption/);
  assert.match(action.body, /opened automatically/);
  assert.match(action.body, /newest py-cord release appears to fix this/);
  assert.equal(keyOfIssue(action), 'bot:voice-encryption');
  assert.deepEqual(action.labels, ['e2e-canary']);
});

test('existing issue for the same failure gets a comment instead of a duplicate', () => {
  const reports = { image: fail('audio-transport'), locked: fail('audio-transport'), latest: fail('audio-transport') };
  const actions = planIssueActions({ reports, openIssues: [{ number: 3, body: 'x <!-- e2e-canary-key: bot:audio-transport --> y' }], ...ctx });
  assert.deepEqual(actions.map((a) => [a.type, a.issue]), [['comment', 3]]);
  assert.match(actions[0].body, /Still failing/);
});

test('only newest deps failing is an "upcoming" maintainer issue, not a user-facing one', () => {
  const reports = { image: pass(), locked: pass(), latest: fail('gateway') };
  const groups = issueKeys(reports);
  assert.deepEqual(groups.map((g) => g.key), ['upcoming:gateway']);
  const issue = renderIssue(groups[0], reports, ctx);
  assert.match(issue.title, /Newest py-cord breaks/);
  assert.doesNotMatch(issue.body, /probably why/);
});

test('harness failures are labelled separately', () => {
  const reports = { image: pass(), locked: fail('harness'), latest: pass() };
  const [action] = planIssueActions({ reports, openIssues: [], ...ctx });
  assert.deepEqual(action.labels, ['e2e-canary', 'e2e-harness']);
});

test('missing reports count as harness failures and block auto-closing', () => {
  const reports = normalizeReports({ image: pass(), latest: pass() }, ['image', 'locked', 'latest']);
  assert.equal(reports.locked.failure.layer, 'harness');
  const actions = planIssueActions({ reports, openIssues: [{ number: 9, body: '<!-- e2e-canary-key: bot:gateway -->' }], ...ctx });
  assert.ok(!actions.some((a) => a.type === 'close'), 'must not close while a track is missing');
});

test('Discord incidents add the possible-outage label', () => {
  const outage = fail('voice-connect', { discordStatus: { indicator: 'major', description: 'Major outage', incidents: [{ name: 'Voice issues', status: 'investigating', url: 'https://stspg.io/x' }] } });
  const reports = { image: outage, locked: fail('voice-connect'), latest: fail('voice-connect') };
  const [action] = planIssueActions({ reports, openIssues: [], ...ctx });
  assert.ok(action.labels.includes('possible-outage'));
  assert.match(action.body, /temporary Discord outage/);
  assert.match(action.body, /Voice issues/);
});
