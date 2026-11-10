#!/usr/bin/env node
// Opens / updates / closes GitHub issues from E2E results.
//
// Usage: node ci/canary-issue.mjs <dir containing results-<track>/ folders>
// Env:   GITHUB_TOKEN, GITHUB_REPOSITORY, RUN_URL, EXPECTED_TRACKS (comma list),
//        DRY_RUN=1 to print actions instead of performing them.

import { readFile, readdir } from 'node:fs/promises';
import path from 'node:path';

import { HARNESS_LABEL, LABEL, OUTAGE_LABEL, normalizeReports, planIssueActions } from '../lib/canary-issue.mjs';

const dir = process.argv[2] ?? '.';
const repo = process.env.GITHUB_REPOSITORY;
const token = process.env.GITHUB_TOKEN;
const runUrl = process.env.RUN_URL ?? '';
const dryRun = process.env.DRY_RUN === '1';
const expected = (process.env.EXPECTED_TRACKS ?? 'image,locked,latest').split(',').map((s) => s.trim()).filter(Boolean);

async function gh(method, endpoint, body) {
  const res = await fetch(`https://api.github.com/repos/${repo}${endpoint}`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      ...(body ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok && !(method === 'POST' && endpoint === '/labels' && res.status === 422)) {
    throw new Error(`GitHub ${method} ${endpoint} -> ${res.status} ${await res.text()}`);
  }
  return res.status === 204 ? null : res.json();
}

async function loadReports() {
  const reports = {};
  const technical = {};
  for (const entry of await readdir(dir, { withFileTypes: true }).catch(() => [])) {
    if (!entry.isDirectory() || !entry.name.startsWith('results-')) continue;
    const track = entry.name.slice('results-'.length);
    // artifacts contain results/ (final attempt) and maybe results-attempt-1/
    for (const sub of ['results', '.']) {
      try {
        const base = path.join(dir, entry.name, sub);
        reports[track] = JSON.parse(await readFile(path.join(base, 'report.json'), 'utf8'));
        technical[track] = await readFile(path.join(base, 'summary.md'), 'utf8').catch(() => '');
        break;
      } catch {
        // try next layout; a track with no report is treated as missing
      }
    }
  }
  return { reports: normalizeReports(reports, expected), technical };
}

const { reports, technical } = await loadReports();
const openIssues = dryRun && !token ? [] : await gh('GET', `/issues?labels=${LABEL}&state=open&per_page=100`);
const actions = planIssueActions({
  reports,
  openIssues: openIssues.filter((i) => !i.pull_request),
  runUrl,
  date: new Date().toISOString().slice(0, 10),
  technicalByTrack: technical,
});

if (dryRun) {
  console.log(JSON.stringify(actions, null, 2));
  process.exit(0);
}

for (const [name, color, description] of [
  [LABEL, 'b60205', 'Detected automatically by the end-to-end Discord tests'],
  [HARNESS_LABEL, 'fbca04', 'E2E test infrastructure problem'],
  [OUTAGE_LABEL, 'c5def5', 'Discord reported an incident at the time of failure'],
]) {
  await gh('POST', '/labels', { name, color, description });
}

for (const action of actions) {
  if (action.type === 'create') {
    const issue = await gh('POST', '/issues', { title: action.title, body: action.body, labels: action.labels });
    console.log(`opened #${issue.number}: ${action.title}`);
  } else if (action.type === 'comment') {
    await gh('POST', `/issues/${action.issue}/comments`, { body: action.body });
    console.log(`updated #${action.issue}`);
  } else if (action.type === 'close') {
    await gh('POST', `/issues/${action.issue}/comments`, { body: action.body });
    await gh('PATCH', `/issues/${action.issue}`, { state: 'closed', state_reason: 'completed' });
    console.log(`closed #${action.issue}`);
  }
}
if (!actions.length) console.log('nothing to report');
