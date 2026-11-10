// Driver / listener bot.
//
// A second bot account that:
//   * sits in the test voice channel, so it is the "author in voice" for commands
//   * sends control requests to the bot under test (SUT) by @mention and awaits replies
//   * receives the SUT's voice audio through Discord (DAVE E2EE included),
//     counting raw RTP packets, decrypted Opus frames and decoded PCM separately
//     so a failure can be pinned to a specific layer, and feeds PCM to a ToneTracker.

import { randomBytes } from 'node:crypto';

import {
  EndBehaviorType,
  VoiceConnectionStatus,
  entersState,
  getVoiceConnection,
  joinVoiceChannel,
} from '@discordjs/voice';
import { Client, Events, GatewayIntentBits, REST, Routes } from 'discord.js';
import prism from 'prism-media';

import { withTimeout } from './proc.mjs';
import { ToneTracker } from './tone.mjs';

export function userIdFromToken(token) {
  return Buffer.from(token.split('.')[0], 'base64').toString('utf8');
}

export function quoteArg(value) {
  return `"${String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`;
}

export class Driver {
  constructor({ token, guildId, controlChannelId, voiceChannelId, sutId, nonce, toneCandidates, log }) {
    this.token = token;
    this.guildId = guildId;
    this.controlChannelId = controlChannelId;
    this.voiceChannelId = voiceChannelId;
    this.sutId = sutId;
    this.nonce = nonce;
    this.log = log ?? (() => {});
    this.toneCandidates = toneCandidates;
    this.tracker = new ToneTracker(toneCandidates);
    this.stats = { rtpPackets: 0, opusFrames: 0, decodeErrors: 0, streamErrors: [], subscriptions: 0 };
    this.sutVoiceChannel = null;
    this.sutVoiceEvents = [];
    this.replyWaiters = new Map();
    this.readyWaiters = [];
    this.readyCount = 0;
    this.connection = null;
    this.subscription = null;
    this.voiceEventWaiters = new Set();

    this.client = new Client({
      intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildVoiceStates, GatewayIntentBits.GuildMessages],
    });
    this.client.on(Events.MessageCreate, (m) => this.onMessage(m));
    this.client.on(Events.VoiceStateUpdate, (oldState, newState) => this.onVoiceState(oldState, newState));
  }

  get userId() {
    return this.client.user?.id;
  }

  async login() {
    const ready = new Promise((resolve) => this.client.once(Events.ClientReady, resolve));
    await this.client.login(this.token);
    await withTimeout(ready, 60_000, 'driver bot gateway READY');
    this.guild = await this.client.guilds.fetch(this.guildId);
    this.controlChannel = await this.client.channels.fetch(this.controlChannelId);
    await this.client.channels.fetch(this.voiceChannelId); // fail early if inaccessible
    // seed SUT voice state from cache in case it is lingering in a channel
    this.sutVoiceChannel = this.guild.voiceStates.cache.get(this.sutId)?.channelId ?? null;
  }

  // ---- control channel -------------------------------------------------

  onMessage(message) {
    if (message.author.id !== this.sutId || message.channelId !== this.controlChannelId) return;
    const parts = message.content.split(' ');
    // <@driver> e2e <nonce> <request-id> <text...>
    if (parts.length < 4 || parts[1] !== 'e2e' || parts[2] !== this.nonce) return;
    const requestId = parts[3];
    const text = parts.slice(4).join(' ');
    if (requestId === '-' && text === 'ready') {
      this.readyCount++;
      for (const resolve of this.readyWaiters.splice(0)) resolve();
      return;
    }
    const waiter = this.replyWaiters.get(requestId);
    if (waiter) {
      waiter.replies.push({ text, components: message.components.length, at: Date.now() });
      waiter.notify();
    }
  }

  // Resolves once the bot under test has announced ready `count` times in
  // this run (it announces again after a restart, or a gateway reconnect).
  waitForSutReady(timeoutMs, count = 1) {
    return withTimeout(
      new Promise((resolve) => {
        const check = () => (this.readyCount >= count ? resolve() : this.readyWaiters.push(check));
        check();
      }),
      timeoutMs,
      'bot under test to announce ready',
    );
  }

  // Sends a command to the SUT and resolves with its first reply text.
  async command(name, args = {}, { timeoutMs = 30_000 } = {}) {
    const requestId = randomBytes(4).toString('hex');
    const argStr = Object.entries(args)
      .filter(([, v]) => v !== undefined && v !== null)
      .map(([k, v]) => `${k}=${quoteArg(v)}`)
      .join(' ');
    const waiter = { replies: [], notify: () => {} };
    this.replyWaiters.set(requestId, waiter);
    const content = `<@${this.sutId}> e2e ${this.nonce} ${requestId} ${name}${argStr ? ' ' + argStr : ''}`;
    this.log(`-> ${name} ${argStr}`);
    try {
      await this.controlChannel.send({ content, allowedMentions: { users: [this.sutId] } });
      await withTimeout(
        new Promise((resolve) => {
          waiter.notify = () => waiter.replies.length && resolve();
          waiter.notify();
        }),
        timeoutMs,
        `reply to "${name}"`,
      );
      const reply = waiter.replies[0];
      this.log(`<- ${reply.text}`);
      if (reply.text.startsWith('error ')) throw new Error(`bot raised while handling "${name}": ${reply.text.slice(6)}`);
      return reply.text;
    } finally {
      this.replyWaiters.delete(requestId);
    }
  }

  async jsonCommand(name) {
    const text = await this.command(name);
    return JSON.parse(text.slice(name.length + 1));
  }

  // ---- slash command registration (REST, as the SUT application) -------

  static async fetchGuildCommands(sutToken, guildId) {
    const rest = new REST({ version: '10' }).setToken(sutToken);
    const app = await rest.get(Routes.currentApplication());
    return { applicationId: app.id, commands: await rest.get(Routes.applicationGuildCommands(app.id, guildId)) };
  }

  // ---- voice state of the SUT -----------------------------------------

  onVoiceState(oldState, newState) {
    if (newState.id !== this.sutId) return;
    if (oldState.channelId === newState.channelId) return;
    this.sutVoiceChannel = newState.channelId;
    this.sutVoiceEvents.push({ at: Date.now(), channelId: newState.channelId });
    this.log(`SUT voice: ${oldState.channelId ?? '-'} -> ${newState.channelId ?? '-'}`);
    for (const w of this.voiceEventWaiters) w();
  }

  waitForSutVoice(channelId, timeoutMs) {
    const description = channelId ? 'bot under test to join the voice channel' : 'bot under test to leave voice';
    return withTimeout(
      new Promise((resolve) => {
        const check = () => {
          if (this.sutVoiceChannel === channelId) {
            this.voiceEventWaiters.delete(check);
            resolve();
          }
        };
        this.voiceEventWaiters.add(check);
        check();
      }),
      timeoutMs,
      description,
    );
  }

  // ---- listener's own voice connection --------------------------------

  async joinVoice() {
    this.connection = joinVoiceChannel({
      channelId: this.voiceChannelId,
      guildId: this.guildId,
      adapterCreator: this.guild.voiceAdapterCreator,
      selfDeaf: false,
      selfMute: true,
      daveEncryption: true,
      debug: true,
    });
    this.connection.on('debug', (m) => this.log(`[voice] ${m}`));
    this.connection.on('error', (err) => this.log(`[voice error] ${err.message}`));
    await entersState(this.connection, VoiceConnectionStatus.Ready, 30_000);
    this.instrumentReceiver();
  }

  async leaveVoice() {
    this.unsubscribe();
    const conn = this.connection ?? getVoiceConnection(this.guildId);
    if (conn) {
      conn.destroy();
      this.connection = null;
    }
  }

  instrumentReceiver() {
    const receiver = this.connection.receiver;
    const speaking = receiver.speaking;
    const original = speaking.onPacket.bind(speaking);
    speaking.onPacket = (userId) => {
      if (userId === this.sutId) this.stats.rtpPackets++;
      return original(userId);
    };
    // (re)subscribe whenever the SUT starts sending
    speaking.on('start', (userId) => {
      if (userId === this.sutId && !this.subscription) this.subscribe();
    });
    this.subscribe();
  }

  subscribe() {
    const receiver = this.connection.receiver;
    const opus = receiver.subscribe(this.sutId, { end: { behavior: EndBehaviorType.Manual } });
    const decoder = new prism.opus.Decoder({ rate: 48000, channels: 2, frameSize: 960 });
    this.stats.subscriptions++;
    opus.on('data', () => this.stats.opusFrames++);
    opus.on('error', (err) => {
      this.stats.streamErrors.push(err.message);
      this.log(`[receive error] ${err.message}`);
      this.subscription = null;
    });
    opus.on('close', () => {
      if (this.subscription?.opus === opus) this.subscription = null;
    });
    decoder.on('data', (pcm) => this.tracker.pushPcm(pcm));
    decoder.on('error', () => this.stats.decodeErrors++);
    opus.pipe(decoder);
    this.subscription = { opus, decoder };
  }

  unsubscribe() {
    if (!this.subscription) return;
    this.subscription.opus.destroy();
    this.subscription.decoder.destroy();
    this.subscription = null;
  }

  voiceDiagnostics() {
    const state = this.connection?.state;
    const net = state?.networking?.state;
    return {
      status: state?.status ?? 'none',
      networkingCode: net?.code ?? null,
      encryptionMode: net?.connectionData?.encryptionMode ?? null,
      daveProtocolVersion: net?.dave?.protocolVersion ?? null,
      ...this.stats,
      pcmFrames: this.tracker.framesReceived,
    };
  }

  async destroy() {
    await this.leaveVoice().catch(() => {});
    await this.client.destroy();
  }
}
