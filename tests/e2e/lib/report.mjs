// Step runner + failure classification.
//
// Every step is tagged with the layer of the stack it exercises. The first
// failing step determines the reported layer, which drives the GitHub issue
// title so users searching for their symptom find it.

export const COMPONENT = {
  BOT: 'bot', // the user-facing bot is affected
  HARNESS: 'harness', // test infrastructure (Jellyfin fixture, listener bot) broke
};

export const LAYERS = {
  harness: {
    component: COMPONENT.HARNESS,
    title: 'E2E test harness is broken',
    description: 'The test infrastructure failed before the bot could be tested. This does not necessarily affect users.',
  },
  gateway: {
    component: COMPONENT.BOT,
    title: "Bot can't log in or stay connected to Discord",
    description: 'The bot failed to start, log in, or exchange messages over the Discord gateway.',
  },
  commands: {
    component: COMPONENT.BOT,
    title: 'Slash command registration with Discord is failing',
    description: "The bot's slash commands are missing or malformed on Discord's side.",
  },
  'voice-connect': {
    component: COMPONENT.BOT,
    title: "Bot can't join voice channels",
    description: 'The bot accepted a play command but never joined the voice channel.',
  },
  'voice-encryption': {
    component: COMPONENT.BOT,
    title: 'Voice encryption (DAVE / transport) negotiation with Discord is failing',
    description: 'The bot joined voice, but the encrypted voice session was not established.',
  },
  'audio-transport': {
    component: COMPONENT.BOT,
    title: 'Bot joins voice but no audio reaches Discord',
    description: 'The bot is in the voice channel, but no audio packets from it arrived through Discord.',
  },
  'audio-content': {
    component: COMPONENT.BOT,
    title: 'Audio played through Discord is silent or corrupted',
    description: 'Audio packets arrived through Discord but could not be decrypted/decoded, or did not contain the expected track.',
  },
  'bot-behavior': {
    component: COMPONENT.BOT,
    title: 'Playback commands misbehave (queue / skip / pause / stop)',
    description: 'Basic playback works, but a playback control did not behave as expected.',
  },
};

export class StepFailure extends Error {
  constructor(step, cause) {
    super(`${step.layer} / ${step.name}: ${cause.message}`);
    this.step = step;
    this.cause = cause;
  }
}

export class Report {
  constructor({ log } = {}) {
    this.steps = [];
    this.log = log ?? console.log;
    this.diagnostics = {};
    this.meta = {};
    this.startedAt = new Date().toISOString();
  }

  async step(layer, name, fn) {
    if (!LAYERS[layer]) throw new Error(`unknown layer ${layer}`);
    const entry = { layer, name, status: 'running', startedAt: Date.now() };
    this.steps.push(entry);
    this.log(`▶ [${layer}] ${name}`);
    try {
      const result = await fn();
      entry.status = 'passed';
      entry.durationMs = Date.now() - entry.startedAt;
      this.log(`✔ [${layer}] ${name} (${entry.durationMs} ms)`);
      return result;
    } catch (err) {
      entry.status = 'failed';
      entry.durationMs = Date.now() - entry.startedAt;
      entry.error = err instanceof StepFailure ? err.cause.message : err.message;
      this.log(`✖ [${layer}] ${name}: ${entry.error}`);
      throw err instanceof StepFailure ? err : new StepFailure(entry, err);
    }
  }

  // Like step(), but a failure is reported in a layer decided after the fact
  // (e.g. "no tone heard" is audio-transport if zero packets arrived).
  async classifiedStep(name, fn, classify) {
    const entry = { layer: 'audio-content', name, status: 'running', startedAt: Date.now() };
    this.steps.push(entry);
    this.log(`▶ ${name}`);
    try {
      const result = await fn();
      entry.layer = classify(null);
      entry.status = 'passed';
      entry.durationMs = Date.now() - entry.startedAt;
      this.log(`✔ [${entry.layer}] ${name} (${entry.durationMs} ms)`);
      return result;
    } catch (err) {
      entry.layer = classify(err);
      entry.status = 'failed';
      entry.durationMs = Date.now() - entry.startedAt;
      entry.error = err.message;
      this.log(`✖ [${entry.layer}] ${name}: ${entry.error}`);
      throw new StepFailure(entry, err);
    }
  }

  get failedStep() {
    return this.steps.find((s) => s.status === 'failed') ?? null;
  }

  toJSON() {
    const failed = this.failedStep;
    const layer = failed ? LAYERS[failed.layer] : null;
    return {
      ok: !failed && this.steps.length > 0,
      startedAt: this.startedAt,
      finishedAt: new Date().toISOString(),
      failure: failed && {
        layer: failed.layer,
        component: layer.component,
        title: layer.title,
        description: layer.description,
        step: failed.name,
        error: failed.error,
      },
      meta: this.meta,
      steps: this.steps.map(({ startedAt, ...s }) => s),
      diagnostics: this.diagnostics,
    };
  }
}

// Markdown for the job summary and the GitHub issue's technical section.
export function renderMarkdown(report) {
  const lines = [];
  const icon = { passed: '✅', failed: '❌', running: '⏳' };
  lines.push(`**Result:** ${report.ok ? '✅ passed' : `❌ failed at \`${report.failure.layer}\` — ${report.failure.title}`}`);
  if (report.failure) lines.push('', '```', report.failure.error, '```');
  lines.push('', '| | Layer | Step | Time |', '|---|---|---|---|');
  for (const s of report.steps) lines.push(`| ${icon[s.status]} | ${s.layer} | ${s.name} | ${((s.durationMs ?? 0) / 1000).toFixed(1)} s |`);
  if (Object.keys(report.meta).length) {
    lines.push('', '<details><summary>Run metadata</summary>', '', '```json', JSON.stringify(report.meta, null, 2), '```', '</details>');
  }
  if (Object.keys(report.diagnostics).length) {
    lines.push('', '<details><summary>Diagnostics</summary>', '', '```json', JSON.stringify(report.diagnostics, null, 2), '```', '</details>');
  }
  return lines.join('\n');
}
