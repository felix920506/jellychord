# JellyChord

Jellyfin music bot for Discord. Name courtesy of [@thornbill](https://github.com/thornbill)

## Features

- Listen to music in your Jellyfin library in Discord channels
- Supports multiple channels / servers playing at the same time
- Transcoding on the Jellyfin server reduces network traffic
- Fairplay mode: Users take turns listening to tracks they choose
- Support for Discord DAVE E2EE Voice protocol

## Requirements

Requirements related to the Jellyfin server:

- A Jellyfin Server accessible by the bot through its HTTP API
- An API key to the Jellyfin server
- Transcoding audio to `opus` codec be properly setup on the Jellyfin server

Requirements related to the bot server:

- An internet connection that does not block access to Discord Voice

> [!IMPORTANT]
> Due to how Discord voice works, you NEED a stable internet connection on the bot server, or else music might stutter, play fast/slow or otherwise not work properly. The device hosting the bot SHOULD have a hard wired connection to the internet whenever possible. It SHOULD NOT use Wi-fi or powerline adapters. If you don't have a good internet connection, please find somewhere else to host this bot. Since it isn't actually doing any transcoding, basically anything you can install the environment on will run it without problems. The connection to Jellyfin is buffered, so you don't need to worry about internet quality that much.

## How to run

### Using docker compose

x64 and arm64 images are provided.
Sample docker compose config:

```yml
services:
  jellychord:
    image: ghcr.io/felix920506/jellychord:latest
    environment:
      JELLYCHORD_DC_TOKEN: discord_token_here
      JELLYCHORD_JF_SERVER: https://media.example.com/jellyfin
      JELLYCHORD_JF_APIKEY: your_jellyfin_apikey
      JELLYCHORD_COMMAND_GROUP: jellychord
      JELLYCHORD_SEARCH_LIMIT: 25
      JELLYCHORD_ENABLE_FAIRPLAY: 0
```

This can also be found in the repository as `compose.yaml`

#### Image tags

Images are published to `ghcr.io/felix920506/jellychord` only after the unit tests pass for that commit.

| Tag | Points to | Moves? |
| --- | --- | --- |
| `latest` | the newest commit on `main` | yes, on every push to `main` |
| `sha-<commit>` (for example `sha-1de0eab`) | one commit, by the first 7 characters of its hash | no |
| `X.Y.Z` (for example `0.1.0`) | release `vX.Y.Z` | no |
| `X.Y` (for example `0.1`) | the newest `X.Y.*` release (not set for pre-releases) | yes, on each `X.Y.*` release |

Each tag is a multi-arch image (amd64 and arm64). Single-architecture tags are also published as `latest-amd64`, `latest-arm64`, `sha-<commit>-amd64` and `sha-<commit>-arm64`. Releases do not move `latest`.

To pin a version, or roll back after a bad update, replace `latest` in your compose file with a release or commit tag and recreate the container:

```yml
    image: ghcr.io/felix920506/jellychord:sha-1de0eab
```

```sh
docker compose pull && docker compose up -d
```

The available tags are listed on the [package page](https://github.com/felix920506/jellychord/pkgs/container/jellychord). Switch back to `latest` to follow `main` again.

Logs go to stderr at the `INFO` level by default. Set `JELLYCHORD_LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING`, `ERROR`) to change it.

You will need to create a Discord bot account to run this application.

### Directly from source

You will need the following setup before running the bot:

- A valid install of `ffmpeg` added to PATH
- Python and Poetry

Steps:

1. Clone the repo
2. Create a copy of `config.yml.example` and name it `config.yml`, confirm rename the extension if asked.
3. Create a Discord application and get a bot token and supply it in `config.yml`
4. Supply your Jellyfin server address in `config.yml`
5. Create a Jellyfin API key in the dashboard and supply it in `config.yml`
6. Open a terminal in the bot folder
7. run `poetry install` to install dependencies
8. Open a terminal in the bot folder
9. run `poetry run python3 main.py` to start the bot. You may need to run `poetry run python main.py` if you are on Windows.
10. Press `Ctrl+C` in the terminal window to exit the bot. MacOS uses the same key bind.

### Settings

The same settings are read from `config.yml`, or from environment variables when `JELLYCHORD_USE_CONF_FILE` is `0` (the docker image sets this).

| `config.yml` key | Environment variable | Default |
| --- | --- | --- |
| `discord-token` | `JELLYCHORD_DC_TOKEN` | required |
| `jf-server` | `JELLYCHORD_JF_SERVER` | required |
| `jf-apikey` | `JELLYCHORD_JF_APIKEY` | required |
| `command-group` | `JELLYCHORD_COMMAND_GROUP` | `jellychord` |
| `search-limit` | `JELLYCHORD_SEARCH_LIMIT` | `25` (kept between 1 and 25) |
| `fairplay` | `JELLYCHORD_ENABLE_FAIRPLAY` (`1` to enable) | off |
| `log-level` | `JELLYCHORD_LOG_LEVEL` | `INFO` |
| `enable-debug` | `JELLYCHORD_ENABLE_DEBUG` (`1` to enable) | off |
| `debug-server` | `JELLYCHORD_DEBUG_SERVER` | required when debug mode is on |

In `config.yml`, `fairplay` and `enable-debug` take `true`/`false` (also `yes`/`no`, `on`/`off`, `1`/`0`, quoted or not).

If a required setting is missing or a setting has an invalid value, the bot exits at startup with a message naming the setting(s).

## Commands

This list will assume the default prefix of `jellychord`. This can be changed in the config.

- `/jellychord search <term> <type> <when>`
  Search for a list of items using `<term>`. Options for `<when>` term: `now` stops current track and plays the specified track. `next` places the specified track next in the queue. `last` is the default behavior, places the specified track at the end of the playlist.
- `/jellychord play <term> <type> <when>`
  Parameters work the same as the above command, except it directly uses the first result returned from the server, instead of asking the user to choose from a list of options
- `/jellychord skip`
  Skips the current playing track
- `/jellychord nowplaying`
  Shows the current playing track
- `/jellychord queue`
  Shows the current playlist
- `/jellychord start`
  Starts the player and plays the playlist
- `/jellychord pause`
  Pauses playback
- `/jellychord resume`
  Resumes playback
- `/jellychord stop`
  Stops playback and clears the playlist
- `/jellychord shuffle`
  Shuffles the playlist
- `/jellychord remove <index>`
  Removes the item at the specified index from the playlist. Index starts with 1.
- `/jellychord clear`
  Clears the queue for the current Discord server.
- `/jellychord promote <index>`
  Promotes the item at the specified index from the playlist to the front. Index starts with 1.
- `/jellychord demote <index>`
  Demotes the item at the specified index from the playlist to the back. Index starts with 1.
- `/jellychord playnow <index>`
  Skips the current playing track and play the specified index from the playlist. Index starts with 1. This does NOT discard tracks before the specified index. How this works is promote the specified index then skip the current track. This is disabled in fairplay mode.

## Known limitations / issues / missing features

Intend to fix: All fixed, report issues [here](https://github.com/felix920506/jellychord/issues)

Framework Limitation:

- Stage channels might be broken

I don't need myself but you are welcome to send PRs:

- Playlists (I don't have playlists on my server)
- Login as Jellyfin user instead of using apikey

## Disclosures

> [!NOTE]
> **AI Usage Disclosure**
> AI has been used in CI/CD, tests and coding assistance
