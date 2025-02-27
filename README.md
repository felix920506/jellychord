# JellyChord

Jellyfin music bot for Discord. Name courtesy of [@thornbill](https://github.com/thornbill)

## Requirements

Requirements related to the Jellyfin server:

- A Jellyfin Server accessible by the bot through its HTTP API
- An API key to the Jellyfin server
- Transcoding audio to `opus` codec be properly setup on the Jellyfin server

Requirements related to the bot server:

- A valid install of `ffmpeg` added to PATH
- Python and Poetry
- An internet connection that does not block access to Discord Voice

> [!IMPORTANT]
> Due to how Discord voice works, you NEED a stable internet connection on the bot server, or else music might stutter, play fast/slow or otherwise not work properly. The device hosting the bot SHOULD have a hard wired connection to the internet whenever possible. It SHOULD NOT use Wi-fi or powerline adapters. If you don't have a good internet connection, please find somewhere else to host this bot. Since it isn't actually doing any transcoding, basically anything you can install the environment on will run it without problems. The connection to Jellyfin is buffered, so you don't need to worry about internet quality that much.

## How to setup

You will need to show file extensions if you are on Windows.

1. Create a copy of `config.yml.example` and name it `config.yml`, confirm rename the extension if asked.
2. Create a Discord application and get a bot token and supply it in `config.yml`
3. Supply your Jellyfin server address in `config.yml`
4. Create a Jellyfin API key in the dashboard and supply it in `config.yml`
5. Open a terminal in the bot folder
6. run `poetry install` to install dependencies

## How to run

### Using docker compose

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
```

This can also be found in the repository as `compose.yaml`

### Directly from source

1. Open a terminal in the bot folder
2. run `poetry run python3 main.py` to start the bot. You may need to run `poetry run python main.py` if you are on Windows.
3. press `Ctrl+C` in the terminal window to exit the bot. MacOS uses the same key bind.

It is normal to see these messages in the console. This is caused by a problem in Pycord and will be fixed in a future release of Pycord. Please ignore these warnings.
<img width="1001" alt="Codec AVOption b (set bitrate (in bits/s)) has not been used for any stream. The most likely reason is either wrong type (e.g. a video option with no video streams) or that it is a private option of some encoder which was not actually used for any stream." src="https://github.com/user-attachments/assets/59b9deb5-d250-4012-bb88-20eb2b948e5d">

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
  Skips the current playing track and play the specified index from the playlist. Index starts with 1. This does NOT discard tracks before the specified index. How this works is promote the specified index then skip the current track.

## Known limitations / issues / missing features

Intend to fix: All fixed, report issues [here](https://github.com/felix920506/jellychord/issues)

Framework Limitation:

- Stage channels might be broken

I don't need myself but you are welcome to send PRs:

- Playlists (I don't have playlists on my server)
- Login as Jellyfin user instead of using apikey
