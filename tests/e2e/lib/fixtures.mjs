// Test media library: pure sine tones at distinct, non-harmonic frequencies,
// tagged so Jellyfin indexes them as tracks/albums by "E2E Artist".
//
// Names are single distinctive words so Jellyfin's search returns the intended
// item first.

import { mkdir, rm } from 'node:fs/promises';
import path from 'node:path';

import { run } from './proc.mjs';

export const ARTIST = 'E2E Artist';
export const TONE_ALBUM = 'Tonealbum';
export const LONG_ALBUM = 'Longalbum';

export const TONES = [
  { title: 'Alpha', freq: 440, seconds: 8, album: TONE_ALBUM, track: 1 },
  { title: 'Bravo', freq: 620, seconds: 8, album: TONE_ALBUM, track: 2 },
  { title: 'Charlie', freq: 970, seconds: 8, album: TONE_ALBUM, track: 3 },
  { title: 'Delta', freq: 750, seconds: 60, album: LONG_ALBUM, track: 1 },
];

export const tone = (title) => {
  const t = TONES.find((x) => x.title === title);
  if (!t) throw new Error(`unknown fixture tone ${title}`);
  return t;
};

export function relativePath(t) {
  return path.join(ARTIST, t.album, `${String(t.track).padStart(2, '0')} - ${t.title}.flac`);
}

// Generates the library into `dir` using the ffmpeg bundled in the Jellyfin
// image, so the harness needs no host ffmpeg.
export async function generateMedia(dir, jellyfinImage) {
  await rm(dir, { recursive: true, force: true });
  for (const t of TONES) await mkdir(path.join(dir, path.dirname(relativePath(t))), { recursive: true });

  const commands = TONES.map((t) =>
    [
      '/usr/lib/jellyfin-ffmpeg/ffmpeg -hide_banner -loglevel error -y',
      `-f lavfi -i "sine=frequency=${t.freq}:sample_rate=48000:duration=${t.seconds}"`,
      '-ac 2 -c:a flac',
      `-metadata title="${t.title}"`,
      `-metadata artist="${ARTIST}"`,
      `-metadata album_artist="${ARTIST}"`,
      `-metadata album="${t.album}"`,
      `-metadata track=${t.track}`,
      `"/media/${relativePath(t)}"`,
    ].join(' '),
  );
  const uid = typeof process.getuid === 'function' ? `${process.getuid()}:${process.getgid()}` : null;
  await run('docker', [
    'run', '--rm',
    ...(uid ? ['--user', uid] : []),
    '-v', `${path.resolve(dir)}:/media`,
    '--entrypoint', '/bin/sh',
    jellyfinImage,
    '-c', commands.join(' && '),
  ]);
}
