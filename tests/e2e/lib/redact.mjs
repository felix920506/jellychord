// Strips secrets from anything written to logs, reports or issue bodies.
// Workflow logs and artifacts of a public repository are world-readable.

const DISCORD_TOKEN = /[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}/g;
// Scalar values of sensitive keys, in snake_case, camelCase, JSON, Python repr or query strings.
const KEY_VALUE = /((?:api[_-]?key|token|password|secret(?:[_-]?key)?|session[_-]?id|Pw)["']?\s*[:=]\s*["']?)((?!\[)[^"'\s&,}\]]{6,})/gi;
// Voice transport keys are logged as byte arrays / objects / Buffers.
const KEY_COLLECTION = /((?:secret[_-]?key)["']?\s*[:=]\s*)(\[[^\]]*\]|\{[^}]*\}|<Buffer[^>]*>)/gi;

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
    out = out.replace(KEY_COLLECTION, (_, key) => `${key}"[redacted]"`);
    out = out.replace(KEY_VALUE, (_, key) => `${key}[redacted]`);
    return out;
  }

  // Redacts every string inside a JSON-compatible value (redacting the
  // serialized text instead could break the JSON syntax).
  json(value) {
    if (typeof value === 'string') return this.text(value);
    if (Array.isArray(value)) return value.map((v) => this.json(v));
    if (value && typeof value === 'object') {
      return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, this.json(v)]));
    }
    return value;
  }
}
