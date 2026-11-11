import assert from 'node:assert/strict';
import { test } from 'node:test';

import { changedDependencies, issueKeys, keyOfIssue, normalizeReports, planIssueActions, renderIssue } from './canary-issue.mjs';
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
  assert.match(issue.title, /Newest py-cord \/ davey break: /);
  assert.match(issue.body, /only happens with the newest py-cord \/ davey releases/);
  assert.doesNotMatch(issue.body, /probably why/);
});

test('"upcoming" issue names the dependency that differs from the locked build', () => {
  const versions = (davey) => ({ meta: { sutVersions: { PyNaCl: '1.6.2', aiohttp: '3.13.5', davey, 'py-cord': '2.8.1', python: '3.14.10' } } });
  const reports = {
    image: pass(),
    locked: { ...pass(), ...versions('0.1.5') },
    latest: { ...fail('voice-encryption'), ...versions('0.2.0') },
  };
  assert.deepEqual(changedDependencies(reports), [{ name: 'davey', from: '0.1.5', to: '0.2.0' }]);
  const issue = renderIssue(issueKeys(reports)[0], reports, ctx);
  assert.equal(issue.title, '[Automated] Newest davey breaks: voice encryption (DAVE / transport) negotiation with Discord is failing');
  assert.match(issue.body, /only happens with davey 0\.2\.0 \(`poetry\.lock` has 0\.1\.5\), so hold off/);
  assert.doesNotMatch(issue.body, /py-cord \d/);
});

test('changed dependencies are unknown when a track could not report its versions', () => {
  const latest = { ...fail('gateway'), meta: { sutVersions: { error: 'timed out' } } };
  const locked = { ...pass(), meta: { sutVersions: { davey: '0.1.5', 'py-cord': '2.8.1' } } };
  assert.deepEqual(changedDependencies({ image: pass(), locked, latest }), []);
  assert.deepEqual(changedDependencies({ image: pass(), latest: { ...fail('gateway'), meta: { sutVersions: { davey: '0.2.0' } } } }), []);
  // A different Python on the two tracks is not a dependency release.
  const python = (v) => ({ meta: { sutVersions: { davey: '0.1.5', python: v } } });
  assert.deepEqual(changedDependencies({ locked: { ...pass(), ...python('3.14.9') }, latest: { ...fail('gateway'), ...python('3.14.10') } }), []);
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
