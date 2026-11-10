// Strips secrets from anything written to logs, reports or issue bodies.
// Workflow logs and artifacts of a public repository are world-readable.

const DISCORD_TOKEN = /[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}/g;
const KEY_VALUE = /((?:api[_-]?key|token|password|secret|session_id|Pw)["']?\s*[:=]\s*["']?)([^"'\s&,}]{6,})/gi;

export class Redactor {
  constructor(secrets = {}) {
    // longest first so overlapping values are fully replaced
    this.secrets = Object.entries(secrets)
      .filter(([, v]) => v && String(v).length >= 4)
      .map(([name, v]) => [name, String(v)])
      .sort((a, b) => b[1].length - a[1].length);
  }

  add(name, value) {
    if (!value || String(value).length < 4) return;
    this.secrets.push([name, String(value)]);
    this.secrets.sort((a, b) => b[1].length - a[1].length);
  }

  text(input) {
    let out = String(input);
    for (const [name, value] of this.secrets) out = out.split(value).join(`[${name}]`);
    out = out.replace(DISCORD_TOKEN, '[discord-token]');
    out = out.replace(KEY_VALUE, (_, key) => `${key}[redacted]`);
    return out;
  }

  json(value) {
    return JSON.parse(this.text(JSON.stringify(value)));
  }
}
