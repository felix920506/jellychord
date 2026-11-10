# End-to-end Discord canary

These tests run the **real bot** against **Discord's real servers** and a **real
Jellyfin server**, play music through Discord voice, and check what a second bot
actually hears. Discord-side changes (voice encryption, DAVE, gateway or API
changes) can break the bot without any commit to this repo, so the suite runs
daily on GitHub Actions and opens a GitHub issue when something breaks.

## How it works

```
 Jellyfin (docker, seeded with sine-tone tracks)
     │  opus HLS
     ▼
 bot under test: python main.py   ◄── or the published Docker image
     │  real gateway + voice (DAVE E2EE)
     ▼
 Discord ── test guild: #control (text), 🔊 voice
     ▲
     │  @mention control messages, voice receive
 listener bot (Node, discord.js + @discordjs/voice)
 + second test bot (same process; a second user for fairplay)
```

* **Fixtures.** The test library contains pure sine tones at distinct,
  non-harmonic frequencies (Alpha 440 Hz, Bravo 620 Hz, Charlie 970 Hz, Delta
  750 Hz). The listener decodes what it receives through Discord and identifies
  which tone, and therefore which track, is playing.
* **Commands.** Discord does not deliver slash-command interactions sent by
  bots. When `JELLYCHORD_E2E_DRIVER_ID` is set, `jellychord/e2e_control.py`
  lets the listener bot trigger the *real* command handlers by @mentioning the
  bot in the control channel. Everything after dispatch runs as in production:
  the queue, Jellyfin, ffmpeg and Discord voice. Slash-command *registration*
  is checked separately via the REST API. The one path left untested is
  Discord delivering an interaction to the bot and the bot answering it
  (`/jellychord ...` typed by a human, and the `/search` dropdown).
* **Fairplay.** Fairplay is a startup setting, so the fairplay scenario
  restarts the bot with `JELLYCHORD_ENABLE_FAIRPLAY=1` and turns it off again
  afterwards; every other scenario runs without it. The second user is a
  second test bot account. The hook also accepts its control messages
  (`JELLYCHORD_E2E_EXTRA_DRIVER_IDS`), and its commands run as that member,
  with its own voice state: the scenario checks that it is refused while it
  is out of voice, then has it join voice, start playback and queue tracks
  alongside the listener. A published image older than extra drivers doesn't
  know the hook's `drivers` command; on the `image` track the scenario is
  then recorded as skipped instead of failing. Running from source, it always
  has to pass.
* **Listener in Node.** py-cord cannot receive audio over DAVE yet
  ([pycord#3139](https://github.com/Pycord-Development/pycord/issues/3139)),
  while `@discordjs/voice` can.
* **Layered results.** Each step is tagged with the layer it exercises, so a
  failure names what broke:

  | Layer | Meaning |
  |---|---|
  | `harness` | Test infrastructure (Jellyfin fixture, listener bot). Not necessarily user-facing. |
  | `gateway` | Bot can't start, log in or exchange messages. |
  | `commands` | Slash commands missing or malformed on Discord's side. |
  | `voice-connect` | Bot never joined the voice channel. |
  | `voice-encryption` | Joined, but the encrypted voice session (transport mode / DAVE) was not established. |
  | `audio-transport` | No audio packets from the bot arrived through Discord. |
  | `audio-content` | Packets arrived, but they could not be decrypted or decoded, or held the wrong track. |
  | `bot-behavior` | A playback control (queue/skip/pause/resume/stop/when=now/fairplay rotation) misbehaved. |

## CI

`.github/workflows/e2e.yml` runs three **tracks**, one at a time because they
share the voice channel:

| Track | What | When |
|---|---|---|
| `image` | published `ghcr.io/felix920506/jellychord:latest` (arm64 runner) | daily, manual |
| `locked` | source with `poetry.lock` | daily, manual, PRs, pushes to `main` |
| `latest` | source with the newest py-cord/davey, pre-releases included | daily, manual |

A failure is retried once (after 10 min on scheduled runs) to ride out short
blips. On scheduled and manual runs on `main`, the `report` job then opens,
comments on, or closes issues labelled `e2e-canary`:

* **`image`/`locked` broken.** Opens a user-facing issue titled after the
  failing layer. If `latest` passes, the issue says a dependency update
  should fix it.
* **Only `latest` broken.** Opens a maintainer issue: hold off on that update.
* **Harness broken.** Opens an issue with the extra label `e2e-harness`.
* **Discord status page reported an incident.** Adds the `possible-outage`
  label.
* **Same failure on a later run.** Comments on the existing issue instead of
  opening a duplicate.
* **Tests pass again.** Closes the issue automatically.

All logs and artifacts go through a redactor, because Actions logs and
artifacts are public on a public repo.

CI is split in two:

* **`tests.yml`** runs the credential-free unit tests (bot and harness) on
  every push and every PR, forks included.
* **`e2e.yml`** uses the bot tokens. It only runs for schedules, manual runs,
  pushes to `main`, and PRs whose branch lives in this repository. Only
  people with write access can push such a branch, so outside contributors'
  fork PRs never reach it. It uses `pull_request`, never
  `pull_request_target`, so fork code is never run with secrets.

> [!NOTE]
> The `image` track only works once an image containing
> `jellychord/e2e_control.py` has been published. Until then it fails at the
> `harness` step "bot under test loads the E2E control hook". That is reported
> as a harness problem, never as "can't connect to Discord".

## One-time setup

1. **Test guild.** Create a Discord server used only for this, with one text
   channel (control) and one voice channel. Don't reuse a real server: the
   bots post there constantly.
2. **Three bot applications** in the
   [Developer Portal](https://discord.com/developers/applications):
   * **Bot under test.** A dedicated bot, not your production bot, because
     the test registers guild commands and plays audio.
   * **Listener.**
   * **Second test user.** Sends commands as a second user and joins voice
     (deafened) in the fairplay scenario. It has to be a bot: automating a
     user account is against Discord's terms.

   None of the bots needs privileged intents. Invite all three with the `bot` and
   `applications.commands` scopes and these permissions on the two channels:
   View Channel, Send Messages, Read Message History, Connect, Speak.
3. **Repository secrets** (Settings → Secrets and variables → Actions):

   | Secret | Value |
   |---|---|
   | `E2E_SUT_TOKEN` | token of the bot under test |
   | `E2E_DRIVER_TOKEN` | token of the listener bot |
   | `E2E_SECOND_DRIVER_TOKEN` | token of the second test bot |
   | `E2E_GUILD_ID` | test guild ID |
   | `E2E_CONTROL_CHANNEL_ID` | control text channel ID |
   | `E2E_VOICE_CHANNEL_ID` | voice channel ID |

## Running locally

Requires Docker, Node ≥ 22.12, and, for source mode, Python 3.14 with the
bot's dependencies and `ffmpeg` on `PATH`.

```sh
cd tests/e2e
npm ci
npm test                  # unit tests for the harness (no Discord needed)

export E2E_SUT_TOKEN=... E2E_DRIVER_TOKEN=... E2E_SECOND_DRIVER_TOKEN=... E2E_GUILD_ID=... \
       E2E_CONTROL_CHANNEL_ID=... E2E_VOICE_CHANNEL_ID=...
export E2E_PYTHON="$(cd ../.. && poetry env info -p)/bin/python"
E2E_SCENARIOS=smoke node run.mjs   # startup + one track (~2 min)
node run.mjs                       # everything (~6 min)
```

Optional settings:

| Variable | Default | Meaning |
|---|---|---|
| `E2E_SUT_MODE` | `source` | `docker` runs `E2E_SUT_IMAGE` instead of `main.py` |
| `E2E_SUT_IMAGE` | `ghcr.io/felix920506/jellychord:latest` | image for docker mode |
| `E2E_JELLYFIN_IMAGE` | pinned in `run.mjs` (updated by Renovate) | Jellyfin image |
| `E2E_SCENARIOS` | `full` | or `smoke` |

Results (`report.json`, `summary.md` and logs for the bot, listener and
Jellyfin) are written to `tests/e2e/results/`.
