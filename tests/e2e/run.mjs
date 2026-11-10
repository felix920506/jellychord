#!/usr/bin/env node
// JellyChord end-to-end canary.
//
// Launches the real bot, connects it to Discord, plays tone tracks from a real
// Jellyfin server through Discord's voice servers and verifies what a second
// bot actually hears. See tests/e2e/README.md.

import { randomBytes } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { Driver, userIdFromToken } from './lib/driver.mjs';
import { TONES, TONE_ALBUM, generateMedia, tone } from './lib/fixtures.mjs';
import { Jellyfin } from './lib/jellyfin.mjs';
import { poll, sleep } from './lib/proc.mjs';
import { Redactor } from './lib/redact.mjs';
import { Report, StepFailure, renderMarkdown } from './lib/report.mjs';
import { Sut } from './lib/sut.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, '..', '..');
export const DEFAULT_JELLYFIN_IMAGE = 'jellyfin/jellyfin:12.4.20261026-211555';
const COMMAND_GROUP = 'jellychord';
const EXPECTED_SUBCOMMANDS = [
  'search', 'play', 'skip', 'nowplaying', 'queue', 'start', 'pause', 'resume',
  'stop', 'shuffle', 'remove', 'promote', 'demote', 'playnow', 'clear',
];

function loadConfig(env = process.env) {
  const required = ['E2E_SUT_TOKEN', 'E2E_DRIVER_TOKEN', 'E2E_GUILD_ID', 'E2E_CONTROL_CHANNEL_ID', 'E2E_VOICE_CHANNEL_ID'];
  const missing = required.filter((k) => !env[k]);
  if (missing.length) {
    console.error(`Missing required environment variables: ${missing.join(', ')}\nSee tests/e2e/README.md for setup.`);
    process.exit(2);
  }
  return {
    sutToken: env.E2E_SUT_TOKEN,
    driverToken: env.E2E_DRIVER_TOKEN,
    guildId: env.E2E_GUILD_ID,
    controlChannelId: env.E2E_CONTROL_CHANNEL_ID,
    voiceChannelId: env.E2E_VOICE_CHANNEL_ID,
    sutMode: env.E2E_SUT_MODE ?? 'source',
    sutImage: env.E2E_SUT_IMAGE ?? 'ghcr.io/felix920506/jellychord:latest',
    python: env.E2E_PYTHON ?? 'python3',
    jellyfinImage: env.E2E_JELLYFIN_IMAGE ?? DEFAULT_JELLYFIN_IMAGE,
    resultsDir: path.resolve(env.E2E_RESULTS_DIR ?? path.join(HERE, 'results')),
    workDir: path.resolve(env.E2E_WORK_DIR ?? path.join(HERE, '.work')),
    track: env.E2E_TRACK ?? 'local',
    // 'smoke' = startup, registration and one track through Discord voice; 'full' = everything
    scenarios: env.E2E_SCENARIOS ?? 'full',
  };
}

const cfg = loadConfig();
const nonce = `${Date.now().toString(36)}${randomBytes(3).toString('hex')}`;
const redactor = new Redactor({
  'sut-token': cfg.sutToken,
  'driver-token': cfg.driverToken,
  'guild-id': cfg.guildId,
  'control-channel-id': cfg.controlChannelId,
  'voice-channel-id': cfg.voiceChannelId,
});
const driverLog = [];
const logDriver = (line) => driverLog.push(`${new Date().toISOString()} ${line}`);
const report = new Report({ log: (l) => console.log(redactor.text(l)) });
report.meta = { track: cfg.track, sutMode: cfg.sutMode, sutImage: cfg.sutMode === 'docker' ? cfg.sutImage : null, jellyfinImage: cfg.jellyfinImage, scenarios: cfg.scenarios, nonce };

let jellyfin;
let driver;
let sut;

const expectReply = (reply, expected) => {
  const ok = expected instanceof RegExp ? expected.test(reply) : reply.includes(expected);
  if (!ok) throw new Error(`expected reply matching ${expected}, got: ${JSON.stringify(reply)}`);
};
const orDied = (promise) => Promise.race([promise, sut.died]);

async function waitForSutVoiceDiag(predicate, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let diag;
  while (Date.now() < deadline) {
    diag = await driver.jsonCommand('diag');
    if (predicate(diag)) return diag;
    await sleep(1000);
  }
  throw new Error(`voice session not established within ${timeoutMs} ms; bot reports ${JSON.stringify(diag)}`);
}

// Classify "didn't hear the expected tone" by how far the audio got.
function audioLayer(baseline) {
  return (err) => {
    if (!err) return 'audio-content';
    const s = driver.stats;
    if (s.rtpPackets === baseline.rtpPackets) return 'audio-transport';
    return 'audio-content';
  };
}

async function hear(name, freq, { since, timeoutMs = 30_000 }) {
  const baseline = { ...driver.stats };
  await report.classifiedStep(
    `${name}: listener hears ${freq} Hz through Discord`,
    () => orDied(driver.tracker.waitForTone(freq, timeoutMs, { since })),
    audioLayer(baseline),
  );
}

async function resetPlayer() {
  if (driver.sutVoiceChannel) {
    await driver.command('stop').catch(() => {});
    await driver.waitForSutVoice(null, 20_000).catch(() => {});
  }
  await driver.command('clear').catch(() => {});
}

function parseElapsed(reply) {
  const m = reply.match(/((?:\d+:)?\d{2}:\d{2})\/(?:\d+:)?\d{2}:\d{2}/);
  if (!m) throw new Error(`could not parse elapsed time from ${JSON.stringify(reply)}`);
  return m[1].split(':').reduce((acc, part) => acc * 60 + Number(part), 0);
}

// ---------------------------------------------------------------------------
// Scenarios
// ---------------------------------------------------------------------------

async function scenarioSingleTrack() {
  const alpha = tone('Alpha');
  const since = Date.now();
  await report.step('bot-behavior', 'play: single track is found and queued', async () =>
    expectReply(await driver.command('play', { term: alpha.title, type: 'Soundtrack' }), `Playing Track: E2E Artist - ${alpha.title}`),
  );
  await report.step('voice-connect', 'bot joins the voice channel', () => orDied(driver.waitForSutVoice(cfg.voiceChannelId, 30_000)));
  const diag = await report.step('voice-encryption', 'bot establishes the encrypted voice session', () =>
    waitForSutVoiceDiag(
      (d) => d.connected && Boolean(d.encryption_mode) && (!(d.dave_protocol_version > 0) || d.dave_ready),
      20_000,
    ),
  );
  report.diagnostics.sutVoice = diag;
  await hear('single track', alpha.freq, { since });
  report.diagnostics.listenerVoice = driver.voiceDiagnostics();
  await report.step('bot-behavior', 'bot leaves voice after the queue finishes', () =>
    orDied(driver.waitForSutVoice(null, alpha.seconds * 1000 + 30_000)),
  );
}

async function scenarioAlbumOrder() {
  const [a, b, c] = TONES.filter((t) => t.album === TONE_ALBUM);
  const since = Date.now();
  await report.step('bot-behavior', 'play: album is queued', async () =>
    expectReply(await driver.command('play', { term: TONE_ALBUM, type: 'Album' }), `Playing Album: E2E Artist - ${TONE_ALBUM}`),
  );
  await hear('album', a.freq, { since });
  await report.step('bot-behavior', 'queue lists the remaining album tracks in order', async () =>
    expectReply(await driver.command('queue'), new RegExp(`${b.title}[\\s\\S]*${c.title}`)),
  );
  await report.step('audio-content', 'album tracks play in order through Discord', () =>
    orDied(
      driver.tracker.waitFor(
        () => driver.tracker.sequence({ since }).join() === [a.freq, b.freq, c.freq].join(),
        (b.seconds + c.seconds) * 1000 + 30_000,
        `tones ${a.freq} → ${b.freq} → ${c.freq}`,
      ),
    ),
  );
  await report.step('bot-behavior', 'bot leaves voice after the album finishes', () => orDied(driver.waitForSutVoice(null, 30_000)));
}

async function scenarioSkip() {
  const [a, b] = TONES.filter((t) => t.album === TONE_ALBUM);
  const since = Date.now();
  await driver.command('play', { term: TONE_ALBUM, type: 'Album' });
  await hear('skip setup', a.freq, { since });
  const skippedAt = Date.now();
  await report.step('bot-behavior', 'skip moves to the next track', async () => {
    expectReply(await driver.command('skip'), 'Skipping current track');
    await orDied(driver.tracker.waitForTone(b.freq, 15_000, { since: skippedAt }));
  });
  await resetPlayer();
}

async function scenarioPlayNow() {
  const delta = tone('Delta');
  const charlie = tone('Charlie');
  const since = Date.now();
  await driver.command('play', { term: delta.title, type: 'Soundtrack' });
  await hear('play-now setup', delta.freq, { since });
  const at = Date.now();
  await report.step('bot-behavior', 'play when=now interrupts the current track', async () => {
    expectReply(await driver.command('play', { term: charlie.title, type: 'Soundtrack', when: 'now' }), charlie.title);
    await orDied(driver.tracker.waitForTone(charlie.freq, 15_000, { since: at }));
  });
  await resetPlayer();
}

async function scenarioPauseResumeStop() {
  const delta = tone('Delta');
  const since = Date.now();
  await driver.command('play', { term: delta.title, type: 'Soundtrack' });
  await hear('pause setup', delta.freq, { since });

  await report.step('bot-behavior', 'pause silences playback and freezes the position', async () => {
    expectReply(await driver.command('pause'), 'Pausing playback');
    await sleep(1500); // let in-flight audio drain
    const quietFrom = Date.now();
    const first = parseElapsed(await driver.command('nowplaying'));
    await sleep(4000);
    const second = parseElapsed(await driver.command('nowplaying'));
    const heard = driver.tracker.toneWindowsSince(quietFrom);
    if (heard > 2) throw new Error(`still heard ${heard} windows (~${heard * 100} ms) of audio while paused`);
    if (second !== first) throw new Error(`nowplaying position advanced while paused: ${first}s -> ${second}s`);
  });

  const resumedAt = Date.now();
  await report.step('bot-behavior', 'resume restarts playback', async () => {
    expectReply(await driver.command('resume'), 'Resuming playback');
    await orDied(driver.tracker.waitForTone(delta.freq, 15_000, { since: resumedAt }));
  });

  await report.step('bot-behavior', 'stop leaves voice and empties the queue', async () => {
    expectReply(await driver.command('stop'), 'Stopping playback');
    await orDied(driver.waitForSutVoice(null, 20_000));
    expectReply(await driver.command('queue'), 'Empty Queue');
    expectReply(await driver.command('nowplaying'), 'Not Currently Playing');
  });
}

async function checkCommandRegistration() {
  const { applicationId, commands } = await Driver.fetchGuildCommands(cfg.sutToken, cfg.guildId);
  report.meta.sutApplicationId = applicationId;
  const group = commands.find((c) => c.name === COMMAND_GROUP);
  if (!group) throw new Error(`command group /${COMMAND_GROUP} not registered; found ${JSON.stringify(commands.map((c) => c.name))}`);
  const sub = Object.fromEntries((group.options ?? []).map((o) => [o.name, o]));
  const missing = EXPECTED_SUBCOMMANDS.filter((n) => !sub[n]);
  if (missing.length) throw new Error(`missing subcommands: ${missing.join(', ')}`);
  const playOpts = Object.fromEntries((sub.play.options ?? []).map((o) => [o.name, o]));
  if (!playOpts.term?.required) throw new Error('/play is missing its required "term" option');
  const whenChoices = (playOpts.when?.choices ?? []).map((c) => c.value).sort();
  if (whenChoices.join() !== 'last,next,now') throw new Error(`/play "when" choices are ${JSON.stringify(whenChoices)}`);
}

// ---------------------------------------------------------------------------

async function main() {
  await mkdir(cfg.resultsDir, { recursive: true });
  await mkdir(cfg.workDir, { recursive: true });

  await report.step('harness', 'generate tone library', () => generateMedia(path.join(cfg.workDir, 'media'), cfg.jellyfinImage));
  jellyfin = new Jellyfin({ image: cfg.jellyfinImage, mediaDir: path.join(cfg.workDir, 'media'), name: `jellychord-e2e-jf-${nonce}` });
  await report.step('harness', 'start and seed Jellyfin', async () => {
    await jellyfin.start();
    await jellyfin.seed();
    redactor.add('jellyfin-api-key', jellyfin.apiKey);
    redactor.add('jellyfin-user-token', jellyfin.userToken);
  });

  const sutId = userIdFromToken(cfg.sutToken);
  redactor.add('sut-user-id', sutId);
  driver = new Driver({
    token: cfg.driverToken,
    guildId: cfg.guildId,
    controlChannelId: cfg.controlChannelId,
    voiceChannelId: cfg.voiceChannelId,
    sutId,
    nonce,
    toneCandidates: TONES.map((t) => t.freq),
    log: logDriver,
  });
  await report.step('harness', 'listener bot logs in to Discord', async () => {
    await driver.login();
    redactor.add('driver-user-id', driver.userId);
  });

  sut = new Sut({
    mode: cfg.sutMode,
    repoRoot: REPO_ROOT,
    python: cfg.python,
    image: cfg.sutImage,
    name: `jellychord-e2e-sut-${nonce}`,
    env: {
      JELLYCHORD_USE_CONF_FILE: '0',
      JELLYCHORD_DC_TOKEN: cfg.sutToken,
      JELLYCHORD_JF_SERVER: jellyfin.url,
      JELLYCHORD_JF_APIKEY: jellyfin.apiKey,
      JELLYCHORD_COMMAND_GROUP: COMMAND_GROUP,
      JELLYCHORD_SEARCH_LIMIT: '25',
      JELLYCHORD_ENABLE_FAIRPLAY: '0',
      JELLYCHORD_ENABLE_DEBUG: '1',
      JELLYCHORD_DEBUG_SERVER: cfg.guildId,
      JELLYCHORD_E2E_DRIVER_ID: driver.userId,
      JELLYCHORD_E2E_CHANNEL_ID: cfg.controlChannelId,
      JELLYCHORD_E2E_NONCE: nonce,
      JELLYCHORD_E2E_LOG_LEVEL: 'DEBUG',
      PYTHONUNBUFFERED: '1',
    },
  });

  // The hook logs before the bot touches Discord. An image built before the
  // hook existed must not be reported as "can't log in to Discord".
  await report.step('harness', 'bot under test loads the E2E control hook', async () => {
    sut.start();
    await orDied(
      poll(() => sut.lines.some((l) => l.includes('E2E control hook enabled')), {
        timeoutMs: 60_000,
        intervalMs: 250,
        description: 'the "E2E control hook enabled" log line (is the bot/image older than the hook?)',
      }),
    );
  });
  await report.step('gateway', 'bot starts and logs in to Discord', () => orDied(driver.waitForSutReady(90_000)));
  await report.step('gateway', 'bot receives and answers messages', async () => expectReply(await driver.command('ping'), 'pong'));
  report.meta.sutVersions = await driver.jsonCommand('versions').catch((e) => ({ error: e.message }));

  await report.step('commands', 'slash commands are registered with the expected schema', checkCommandRegistration);

  // Behaviour that needs the listener *outside* voice.
  await report.step('bot-behavior', 'play reports when nothing matches', async () =>
    expectReply(await driver.command('play', { term: 'zzz-no-such-track-zzz' }), 'No items match your query'),
  );
  await report.step('bot-behavior', 'play refuses when the requester is not in voice', async () =>
    expectReply(await driver.command('play', { term: 'Alpha' }), 'You are not in any voice channel'),
  );

  await report.step('harness', 'listener bot joins the voice channel', () => driver.joinVoice());
  await sleep(1000); // let the SUT see the listener's voice state

  await scenarioSingleTrack();
  if (cfg.scenarios === 'smoke') return;
  await scenarioAlbumOrder();
  await scenarioSkip();
  await scenarioPlayNow();
  await scenarioPauseResumeStop();
}

async function discordStatus() {
  try {
    const res = await fetch('https://discordstatus.com/api/v2/summary.json', { signal: AbortSignal.timeout(10_000) });
    const data = await res.json();
    return {
      indicator: data.status?.indicator,
      description: data.status?.description,
      degradedComponents: (data.components ?? []).filter((c) => c.status !== 'operational').map((c) => `${c.name}: ${c.status}`),
      incidents: (data.incidents ?? []).map((i) => ({ name: i.name, status: i.status, url: i.shortlink })),
    };
  } catch (err) {
    return { error: err.message };
  }
}

let exitCode = 0;
try {
  await main();
} catch (err) {
  exitCode = 1;
  if (!(err instanceof StepFailure)) {
    report.steps.push({ layer: 'harness', name: 'unexpected harness error', status: 'failed', error: err.stack ?? String(err), durationMs: 0 });
    console.error(err);
  }
} finally {
  if (driver) report.diagnostics.listenerVoice = driver.voiceDiagnostics();
  if (driver) report.diagnostics.heardSequence = driver.tracker.sequence();
  if (sut) report.diagnostics.sutNotableLog = sut.notableLines();
  if (report.failedStep) report.diagnostics.discordStatus = await discordStatus();

  // Leave nothing behind in the shared guild.
  if (driver && sut && !sut.exit) await resetPlayer().catch(() => {});
  await sut?.stop().catch(() => {});
  await driver?.destroy().catch(() => {});
  const jellyfinLogs = jellyfin ? await jellyfin.logs() : '';
  await jellyfin?.stop();

  const json = redactor.json(report.toJSON());
  await mkdir(cfg.resultsDir, { recursive: true });
  await writeFile(path.join(cfg.resultsDir, 'report.json'), JSON.stringify(json, null, 2));
  await writeFile(path.join(cfg.resultsDir, 'summary.md'), renderMarkdown(json));
  await writeFile(path.join(cfg.resultsDir, 'sut.log'), redactor.text(sut?.lines.join('\n') ?? ''));
  await writeFile(path.join(cfg.resultsDir, 'listener.log'), redactor.text(driverLog.join('\n')));
  await writeFile(path.join(cfg.resultsDir, 'jellyfin.log'), redactor.text(jellyfinLogs));
  console.log(`\n${json.ok ? 'PASSED' : `FAILED at ${json.failure.layer}: ${json.failure.step}`} — results in ${cfg.resultsDir}`);
  process.exit(json.ok ? 0 : exitCode || 1);
}
