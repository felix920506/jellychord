import { spawn } from 'node:child_process';

// Run a command to completion; resolves with stdout, rejects with stderr tail.
export function run(cmd, args, { env, cwd, input } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { env: env ?? process.env, cwd, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => (stdout += d));
    child.stderr.on('data', (d) => (stderr += d));
    child.on('error', reject);
    child.on('close', (code) => {
      if (code === 0) resolve(stdout);
      else reject(new Error(`${cmd} ${args[0] ?? ''} exited with ${code}: ${stderr.trim().slice(-2000)}`));
    });
    if (input) child.stdin.end(input);
    else child.stdin.end();
  });
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function poll(fn, { timeoutMs, intervalMs = 1000, description }) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    try {
      const value = await fn();
      if (value) return value;
    } catch (err) {
      lastError = err;
    }
    await sleep(intervalMs);
  }
  throw new Error(`timed out after ${timeoutMs} ms waiting for ${description}${lastError ? `: ${lastError.message}` : ''}`);
}

export function withTimeout(promise, ms, description) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`timed out after ${ms} ms waiting for ${description}`)), ms);
    }),
  ]).finally(() => clearTimeout(timer));
}
