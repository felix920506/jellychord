// Decides which GitHub issues to open, update or close from a set of E2E
// reports. Pure (no I/O) so it can be unit tested; see ci/canary-issue.mjs.
//
// Tracks:
//   image  - the published Docker image (what most users run)
//   locked - source checkout with poetry.lock (what the next image will contain)
//   latest - source checkout with the newest py-cord/davey (incl. pre-releases)

import { LAYERS } from './report.mjs';

export const LABEL = 'e2e-canary';
export const HARNESS_LABEL = 'e2e-harness';
export const OUTAGE_LABEL = 'possible-outage';
const KEY_MARKER = /<!-- e2e-canary-key: (\S+) -->/;

export const TRACK_NAMES = {
  image: 'Released Docker image (`ghcr.io/felix920506/jellychord:latest`)',
  locked: 'Source with locked dependencies (`poetry.lock`)',
  latest: 'Source with the newest py-cord / davey releases',
};

// A missing report means the job died before writing one: a harness failure.
export function normalizeReports(reportsByTrack, expectedTracks) {
  const out = {};
  for (const track of expectedTracks) {
    out[track] = reportsByTrack[track] ?? {
      ok: false,
      missing: true,
      failure: {
        layer: 'harness',
        component: 'harness',
        title: LAYERS.harness.title,
        description: LAYERS.harness.description,
        step: 'job did not produce a report',
        error: 'The E2E job for this track crashed or was cancelled before writing results.',
      },
      diagnostics: {},
      meta: { track },
    };
  }
  return out;
}

export function issueKeys(reports) {
  const keys = new Map(); // key -> { kind, layer, tracks: [] }
  const add = (key, kind, layer, track) => {
    if (!keys.has(key)) keys.set(key, { key, kind, layer, tracks: [] });
    keys.get(key).tracks.push(track);
  };
  const releasedFailing = ['image', 'locked'].filter((t) => reports[t] && !reports[t].ok);
  for (const [track, report] of Object.entries(reports)) {
    if (report.ok) continue;
    const { layer, component } = report.failure;
    if (component === 'harness') add('harness', 'harness', layer, track);
    else if (track === 'latest' && !releasedFailing.some((t) => reports[t].failure.layer === layer)) add(`upcoming:${layer}`, 'upcoming', layer, track);
    else add(`bot:${layer}`, 'bot', layer, track);
  }
  return [...keys.values()];
}

export function keyOfIssue(issue) {
  return issue.body?.match(KEY_MARKER)?.[1] ?? null;
}

function isOutage(report) {
  const s = report.diagnostics?.discordStatus;
  return Boolean(s && !s.error && ((s.indicator && s.indicator !== 'none') || s.incidents?.length));
}

// What the `latest` track actually upgraded, from the versions the bot reported
// on the `locked` and `latest` tracks: [{ name, from, to }]. Empty when either
// track did not get far enough to report its versions.
export function changedDependencies(reports) {
  const locked = reports.locked?.meta?.sutVersions;
  const latest = reports.latest?.meta?.sutVersions;
  if (!locked || !latest || locked.error || latest.error) return [];
  return Object.keys(latest)
    .filter((name) => name !== 'python' && locked[name] && latest[name] && locked[name] !== latest[name])
    .sort()
    .map((name) => ({ name, from: locked[name], to: latest[name] }));
}

function statusTable(reports) {
  const lines = ['| Build | Result |', '|---|---|'];
  for (const [track, r] of Object.entries(reports)) {
    const result = r.ok ? '✅ passing' : `❌ failing at \`${r.failure.layer}\` (${r.failure.step})`;
    lines.push(`| ${TRACK_NAMES[track] ?? track} | ${result} |`);
  }
  return lines.join('\n');
}

function workaround(reports, kind) {
  if (kind !== 'bot') return null;
  if (reports.latest?.ok) {
    return 'The newest py-cord release appears to fix this, so a dependency update is needed. If you run from source, upgrading py-cord (`pip install -U --pre "py-cord[voice]"`) may work in the meantime.';
  }
  return 'None known yet.';
}

function discordStatusText(report) {
  const s = report.diagnostics?.discordStatus;
  if (!s) return 'not checked';
  if (s.error) return `could not be fetched (${s.error})`;
  const parts = [s.description ?? s.indicator];
  if (s.degradedComponents?.length) parts.push(`degraded: ${s.degradedComponents.join(', ')}`);
  for (const i of s.incidents ?? []) parts.push(`incident: [${i.name}](${i.url}) (${i.status})`);
  return parts.join('; ');
}

// The track whose details go into the issue: prefer one that produced a report.
export function primaryTrack(group, reports) {
  return group.tracks.find((t) => !reports[t].missing) ?? group.tracks[0];
}

export function renderIssue(group, reports, { runUrl, date, technical }) {
  const primary = reports[primaryTrack(group, reports)];
  const layer = LAYERS[group.layer] ?? LAYERS.harness;
  const outage = group.tracks.some((t) => isOutage(reports[t]));
  let title;
  const lines = [`<!-- e2e-canary-key: ${group.key} -->`];

  if (group.kind === 'bot') {
    title = `[Automated] ${layer.title}`;
    lines.push(
      '> [!WARNING]',
      "> **This issue was opened automatically** by JellyChord's end-to-end tests, which run the bot against Discord's real servers every day.",
      '>',
      "> If your bot recently stopped working, this is probably why, and the maintainers have been notified. Please don't open a duplicate. Subscribe to this issue to hear when it's fixed (a 👍 reaction helps show impact). It closes automatically once the tests pass again.",
    );
  } else if (group.kind === 'upcoming') {
    // Name what actually changed: the newest py-cord and davey are installed
    // together, and often only one of them has a new release.
    const changed = changedDependencies(reports);
    const names = changed.length ? changed.map((c) => c.name).join(' / ') : 'py-cord / davey';
    const releases = changed.length
      ? changed.map((c) => `${c.name} ${c.to} (\`poetry.lock\` has ${c.from})`).join(' and ')
      : 'the newest py-cord / davey releases';
    title = `[Automated] Newest ${names} ${changed.length === 1 ? 'breaks' : 'break'}: ${layer.title.charAt(0).toLowerCase()}${layer.title.slice(1)}`;
    lines.push(
      '> [!NOTE]',
      `> **Opened automatically by the end-to-end tests.** The released bot still works. This failure only happens with ${releases}, so hold off on that dependency update until it is resolved.`,
    );
  } else {
    title = '[Automated] E2E test harness is broken';
    lines.push(
      '> [!NOTE]',
      "> **Opened automatically by the end-to-end tests.** The test infrastructure itself failed, so the bot could not be checked against Discord. This doesn't necessarily affect users.",
    );
  }

  lines.push('', "### What's broken", primary.failure.description ?? layer.description, '', '### Status', statusTable(reports), '');
  const wa = workaround(reports, group.kind);
  if (wa) lines.push(`**Workaround:** ${wa}`, '');
  if (outage) lines.push('> [!IMPORTANT]', '> Discord was reporting an incident when this failed, so this may be a temporary Discord outage and not a breaking change.', '');
  lines.push(`**Discord status at time of failure:** ${discordStatusText(primary)}`, `**Detected:** ${date} ([workflow run](${runUrl}))`, '');
  if (technical) lines.push('<details><summary>Technical details</summary>', '', technical, '', '</details>');

  const labels = [LABEL];
  if (group.kind === 'harness') labels.push(HARNESS_LABEL);
  if (outage) labels.push(OUTAGE_LABEL);
  return { title, body: lines.join('\n'), labels };
}

export function renderStillFailing(group, reports, { runUrl, date }) {
  const lines = [`Still failing as of ${date} ([workflow run](${runUrl})).`, '', statusTable(reports)];
  for (const t of group.tracks) {
    const f = reports[t].failure;
    lines.push('', `**${t}**: \`${f.layer}\` / ${f.step}`, '```', String(f.error).slice(0, 1500), '```');
  }
  return lines.join('\n');
}

// openIssues: [{ number, body }]; returns actions to perform.
export function planIssueActions({ reports, openIssues, runUrl, date, technicalByTrack = {} }) {
  const groups = issueKeys(reports);
  const byKey = new Map(openIssues.map((i) => [keyOfIssue(i), i]).filter(([k]) => k));
  const actions = [];
  for (const group of groups) {
    const existing = byKey.get(group.key);
    if (existing) {
      actions.push({ type: 'comment', issue: existing.number, body: renderStillFailing(group, reports, { runUrl, date }) });
    } else {
      actions.push({ type: 'create', ...renderIssue(group, reports, { runUrl, date, technical: technicalByTrack[primaryTrack(group, reports)] }) });
    }
  }
  // Only close issues when every track actually produced a report, so a
  // skipped/cancelled job can't make an issue look resolved.
  const complete = Object.values(reports).every((r) => !r.missing);
  const active = new Set(groups.map((g) => g.key));
  if (complete) {
    for (const [key, issue] of byKey) {
      if (!active.has(key)) {
        actions.push({
          type: 'close',
          issue: issue.number,
          body: `✅ The end-to-end tests pass again as of ${date} ([workflow run](${runUrl})). Closing automatically. If you still have problems, please open a new issue.\n\n${statusTable(reports)}`,
        });
      }
    }
  }
  return actions;
}
