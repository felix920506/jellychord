// Launches the bot under test (SUT) exactly as users run it: either
// `python main.py` from the checkout, or the published Docker image.

import { spawn } from 'node:child_process';
import path from 'node:path';

import { run, sleep } from './proc.mjs';

export class Sut {
  constructor({ mode, repoRoot, python, image, name, env }) {
    this.mode = mode; // 'source' | 'docker'
    this.repoRoot = repoRoot;
    this.python = python;
    this.image = image;
    this.name = name;
    this.env = env;
    this.lines = [];
    this.exit = null;
    this.child = null;
  }

  start() {
    let cmd;
    let args;
    let env = { ...process.env, ...this.env };
    if (this.mode === 'docker') {
      cmd = 'docker';
      args = ['run', '--rm', '--name', this.name, '--network', 'host'];
      for (const key of Object.keys(this.env)) args.push('-e', key);
      args.push(this.image);
    } else {
      cmd = this.python;
      args = [path.join(this.repoRoot, 'main.py')];
    }
    this.child = spawn(cmd, args, { cwd: this.repoRoot, env, stdio: ['ignore', 'pipe', 'pipe'] });
    const collect = (stream) => {
      let buf = '';
      stream.on('data', (d) => {
        buf += d;
        let idx;
        while ((idx = buf.indexOf('\n')) >= 0) {
          this.lines.push(buf.slice(0, idx));
          buf = buf.slice(idx + 1);
        }
      });
      stream.on('end', () => buf && this.lines.push(buf));
    };
    collect(this.child.stdout);
    collect(this.child.stderr);
    this.exited = new Promise((resolve) => {
      this.child.on('exit', (code, signal) => {
        this.exit = { code, signal };
        resolve(this.exit);
      });
      this.child.on('error', (err) => {
        this.exit = { code: null, signal: null, error: err.message };
        resolve(this.exit);
      });
    });
  }

  // Rejects as soon as the process dies, so waits elsewhere fail fast.
  get died() {
    return this.exited.then((e) => {
      throw new Error(`bot process exited unexpectedly (${JSON.stringify(e)}). Last output:\n${this.tail(40)}`);
    });
  }

  tail(n = 40) {
    return this.lines.slice(-n).join('\n');
  }

  // Lines that indicate a Discord-side failure (voice close codes, DAVE/MLS
  // errors, HTTP errors), for the failure report.
  notableLines() {
    const re = /(close[ _]?code|code[= ]4\d{3}|\b4\d{3}\b.*voice|voice.*\b4\d{3}\b|dave|mls|Traceback|Error|Exception|WARNING|429|Forbidden|Unauthorized)/i;
    return this.lines.filter((l) => re.test(l)).slice(-60);
  }

  async stop() {
    if (!this.child || this.exit) return;
    if (this.mode === 'docker') {
      await run('docker', ['stop', '-t', '10', this.name]).catch(() => {});
    } else {
      this.child.kill('SIGTERM');
    }
    const done = await Promise.race([this.exited, sleep(12_000).then(() => null)]);
    if (!done) {
      if (this.mode === 'docker') await run('docker', ['rm', '-f', this.name]).catch(() => {});
      this.child.kill('SIGKILL');
      await this.exited;
    }
  }
}
