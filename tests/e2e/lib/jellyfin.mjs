// Disposable, seeded Jellyfin server for the E2E run.
//
// Starts the pinned Jellyfin image with the generated tone library mounted,
// completes the startup wizard over the HTTP API, creates an API key and a
// music library, and waits for the scan to index every fixture track.

import { randomBytes } from 'node:crypto';
import path from 'node:path';

import { TONES } from './fixtures.mjs';
import { poll, run } from './proc.mjs';

const AUTH_HEADER = 'MediaBrowser Client="jellychord-e2e", Device="ci", DeviceId="jellychord-e2e", Version="1.0.0"';

export class Jellyfin {
  constructor({ image, mediaDir, name }) {
    this.image = image;
    this.mediaDir = path.resolve(mediaDir);
    this.name = name;
    this.url = null;
    this.apiKey = null;
    this.userToken = null;
    this.password = randomBytes(12).toString('hex');
  }

  async start() {
    await run('docker', ['rm', '-f', this.name]).catch(() => {});
    await run('docker', [
      'run', '-d', '--name', this.name,
      '-p', '127.0.0.1::8096',
      '-v', `${this.mediaDir}:/media:ro`,
      this.image,
    ]);
    const port = (await run('docker', ['port', this.name, '8096/tcp'])).trim().split('\n')[0].split(':').pop();
    this.url = `http://127.0.0.1:${port}`;
    // Jellyfin answers 503 with an HTML status page while migrations run, so
    // wait for a real JSON payload from the API.
    await poll(
      async () => {
        const res = await fetch(`${this.url}/System/Info/Public`);
        if (!res.ok || !(res.headers.get('content-type') ?? '').includes('json')) return false;
        const info = await res.json();
        this.version = info.Version;
        return 'StartupWizardCompleted' in info;
      },
      { timeoutMs: 180_000, intervalMs: 2000, description: 'Jellyfin to start' },
    );
  }

  // Retries while the server reports 503 (still starting up / migrating).
  async request(method, endpoint, opts = {}) {
    const deadline = Date.now() + 120_000;
    for (;;) {
      try {
        return await this.requestOnce(method, endpoint, opts);
      } catch (err) {
        if (err.status !== 503 || Date.now() > deadline) throw err;
        await new Promise((r) => setTimeout(r, 2000));
      }
    }
  }

  async requestOnce(method, endpoint, { body, token, query } = {}) {
    const url = new URL(this.url + endpoint);
    for (const [k, v] of Object.entries(query ?? {})) url.searchParams.set(k, v);
    const headers = { Authorization: token ? `${AUTH_HEADER}, Token="${token}"` : AUTH_HEADER };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const res = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    if (!res.ok) {
      const detail = (await res.text().catch(() => '')).replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 300);
      const err = new Error(`Jellyfin ${method} ${endpoint} -> ${res.status} ${detail}`);
      err.status = res.status;
      throw err;
    }
    const text = await res.text();
    return text ? JSON.parse(text) : null;
  }

  async seed() {
    // Startup wizard (the server rejects most requests until it is completed).
    await this.request('POST', '/Startup/Configuration', {
      body: { UICulture: 'en-US', MetadataCountryCode: 'US', PreferredMetadataLanguage: 'en' },
    });
    await this.request('GET', '/Startup/User');
    await this.request('POST', '/Startup/User', { body: { Name: 'e2e', Password: this.password } });
    await this.request('POST', '/Startup/RemoteAccess', { body: { EnableRemoteAccess: true } });
    await this.request('POST', '/Startup/Complete');

    const auth = await poll(
      () => this.request('POST', '/Users/AuthenticateByName', { body: { Username: 'e2e', Pw: this.password } }),
      { timeoutMs: 60_000, intervalMs: 2000, description: 'Jellyfin login after startup wizard' },
    );
    this.userToken = auth.AccessToken;

    await this.request('POST', '/Auth/Keys', { token: this.userToken, query: { app: 'jellychord-e2e' } });
    const keys = await this.request('GET', '/Auth/Keys', { token: this.userToken });
    this.apiKey = keys.Items.find((k) => k.AppName === 'jellychord-e2e').AccessToken;

    await this.request('POST', '/Library/VirtualFolders', {
      token: this.userToken,
      query: { name: 'Music', collectionType: 'music', paths: '/media', refreshLibrary: 'true' },
      body: {
        LibraryOptions: {
          EnableRealtimeMonitor: false,
          EnableInternetProviders: false,
          MetadataSavers: [],
          PathInfos: [{ Path: '/media' }],
        },
      },
    });

    const folders = await this.request('GET', '/Library/VirtualFolders', { token: this.userToken });
    if (!folders.some((f) => f.Name === 'Music')) throw new Error('music library was not created');

    // A refresh requested while Jellyfin's own startup scan is running can be
    // dropped, so keep nudging it until every fixture track is indexed.
    const titles = TONES.map((t) => t.title);
    let lastRefresh = Date.now();
    await poll(
      async () => {
        const res = await this.request('GET', '/Items', {
          query: { ApiKey: this.apiKey, recursive: 'true', includeItemTypes: 'Audio' },
        });
        const found = new Set(res.Items.map((i) => i.Name));
        if (titles.every((t) => found.has(t))) return true;
        if (Date.now() - lastRefresh > 15_000) {
          lastRefresh = Date.now();
          await this.request('POST', '/Library/Refresh', { token: this.userToken });
        }
        return false;
      },
      { timeoutMs: 180_000, intervalMs: 3000, description: `Jellyfin to index ${titles.join(', ')}` },
    );
  }

  async logs() {
    return run('docker', ['logs', this.name]).catch((err) => `failed to read logs: ${err.message}`);
  }

  async stop() {
    await run('docker', ['rm', '-f', this.name]).catch(() => {});
  }
}
