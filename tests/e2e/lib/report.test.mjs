import assert from 'node:assert/strict';
import { test } from 'node:test';

import { issueKeys, planIssueActions } from './canary-issue.mjs';
import { Report, renderMarkdown } from './report.mjs';

const quiet = () => {};

test('a skipped step does not fail the run or open an issue', async () => {
  const report = new Report({ log: quiet });
  await report.step('gateway', 'bot logs in', async () => {});
  report.skip('harness', 'second driver probe', 'image predates extra drivers');

  const json = report.toJSON();
  assert.equal(json.ok, true);
  assert.equal(json.failure, null);
  assert.equal(json.steps[1].status, 'skipped');
  assert.match(renderMarkdown(json), /⏭️ \| harness \| second driver probe \(skipped: image predates extra drivers\)/);

  const reports = { image: json };
  assert.deepEqual(issueKeys(reports), []);
  // and an open harness issue still gets closed
  const actions = planIssueActions({
    reports,
    openIssues: [{ number: 5, body: '<!-- e2e-canary-key: harness -->' }],
    runUrl: 'https://github.com/o/r/actions/runs/1',
    date: '2026-11-10',
  });
  assert.deepEqual(actions.map((a) => [a.type, a.issue]), [['close', 5]]);
});

test('a failed step after a skipped one is still the failure', async () => {
  const report = new Report({ log: quiet });
  report.skip('harness', 'second driver probe', 'image predates extra drivers');
  await assert.rejects(report.step('bot-behavior', 'stop', async () => { throw new Error('boom'); }));

  const json = report.toJSON();
  assert.equal(json.ok, false);
  assert.equal(json.failure.step, 'stop');
});
