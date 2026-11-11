import discord
import discord.ext
import aiohttp
import asyncio
import random
import datetime
import os
import math
import time
import logging
import sys

from jfapi import JFAPI, JellyfinResponseError
from jellychord.config import ConfigError, load_config

'''
Load Settings

The settings decide which commands exist (the command group's name, debug
commands, /playnow only without fairplay), so they are read when the module is
imported. A bad config exits here, before anything else runs.
'''
try:
    config = load_config()
except ConfigError as e:
    # Logging isn't set up yet (the log level is a setting); exit non-zero
    # with the message on stderr.
    sys.exit(f'jellychord: {e}')

'''
Logging
'''
# The E2E hook's log level takes precedence: once logging is configured
# (setupLogging), the hook's own basicConfig() call is a no-op.
LOG_LEVEL = str(os.getenv('JELLYCHORD_E2E_LOG_LEVEL') or config.get('log-level') or 'INFO').strip().upper()
log = logging.getLogger('jellychord')

def setupLogging():
    """Configure logging for the process. Called by main(), so importing this
    module leaves the importer's logging alone."""
    logging.basicConfig(
        level=logging.getLevelNamesMapping().get(LOG_LEVEL, logging.INFO),
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )
    if LOG_LEVEL not in logging.getLevelNamesMapping():
        log.warning('Unknown log level %r, using INFO', LOG_LEVEL)

JF_APICLIENT = JFAPI(config['jf-server'],config['jf-apikey'])
LIMIT = max(1, min(config['search-limit'], 25))
DEBUG = config["enable-debug"]
if DEBUG:
    DEBUG_SERVER = config["debug-server"]
PLAYLIST_PAGESIZE = 20
SELECT_MAX_OPTIONS = 25 # Discord's limit on options in a select menu

# queues and playing are only read or changed on the event loop. The voice
# player thread never touches them: when a track ends, its after= callback
# hands off to advanceTrack() on the loop (see onTrackEnd).
queues = {}
playing = {}
# guild id -> asyncio.Event, set if a skip was requested while the next track
# was loading
loading = {}
playerLocks = {}
# guild id -> time.monotonic() deadline until which a bot leave event counts as
# the bot's own disconnect (stopPlayback) rather than a kick. py-cord doesn't
# guarantee a leave event for every disconnect, so the flag expires shortly
# after the disconnect finishes instead of waiting for one forever.
expectedDisconnects = {}
# How long after stopPlayback's disconnect returns its leave event may still
# arrive. If py-cord delivers one, it is already queued by then.
EXPECTED_LEAVE_GRACE = 5
# Per-guild counter bumped on every bot leave event and every new voice session,
# so a listener still waiting on an older leave knows it has been superseded.
voiceEpochs = {}
# How long onVoiceStateUpdate waits to see whether py-cord recovers a dropped
# voice connection, and how often it checks.
VOICE_RECOVERY_TIMEOUT = 60
VOICE_POLL_INTERVAL = 0.25
# How long advanceTrack waits for py-cord to recover a dropped voice connection
# before giving up and leaving voice (queued tracks are kept for /start).
VOICE_OUTAGE_TIMEOUT = 120
# guild id -> token of the last player advanceTrack started. Its after=
# callback passes the token back, so advanceTrack can tell that this player's
# thread has exited even if py-cord still reports it as playing.
currentPlayers = {}
# guild id -> asyncio.Event, present while stopPlayback is leaving voice and set
# once it has left. guild.voice_client stays set until the disconnect finishes,
# so commands that would start a player wait on this first (waitUntilLeft).
leaving = {}
# guild id -> radio state, present while radio is on. Like queues, only read or
# changed on the event loop. Radio belongs to the voice session: it is dropped
# when the bot leaves voice (stopPlayback, clearGuildState).
#   mode: 'song' or 'album'
#   libraryId, libraryName: the music library to pick from, None for all
#   buffer: radio tracks waiting to play (the rest of the current album)
#   album: name of the album the buffer came from, None in song mode
#   failures: radio picks or radio tracks that failed in a row
#   channel: where to say so if radio turns itself off
# Changing mode or library stores a new dict, so a pick that was in flight for
# the old one is recognised (by identity) and thrown away.
radioStates = {}
# How long a radio pick may wait for Jellyfin.
RADIO_FETCH_TIMEOUT = 30
# How many radio picks or radio tracks may fail in a row before radio turns
# itself off. Radio is meant to run unattended, so unlike a queued track
# (MAX_START_ATTEMPTS) it waits between attempts and rides out a short outage.
RADIO_MAX_FAILURES = 5
# Wait after the first failure in seconds, doubled after each further one.
RADIO_BACKOFF_BASE = 2
RADIO_BACKOFF_MAX = 30
# How often that wait checks whether it should end early.
RADIO_POLL_INTERVAL = 0.25
# The library option's autocomplete asks on every keystroke.
LIBRARY_CACHE_SECS = 60
musicLibraryCache = {'time': None, 'items': []}

class JellychordBot(discord.Bot):
    async def close(self):
        # bot.run() calls close() on shutdown, also after Ctrl+C or SIGTERM.
        try:
            await super().close()
        finally:
            await JF_APICLIENT.close()

bot = JellychordBot()

'''
Helper Functions
'''
async def searchHelper(term: str, limit: int = LIMIT, searchType:str = None):
    if searchType == 'Soundtrack':
        searchType = ['Audio']
    elif searchType == 'Album':
        searchType = ['MusicAlbum']
    else:
        searchType = ['Audio', 'MusicAlbum']
    
    res = await JF_APICLIENT.search(term, limit, searchType)
    return res

# What a Jellyfin request raises when the server can't be reached, times out,
# answers with an HTTP error (JFAPI calls raise_for_status) or with a body
# that isn't the expected JSON.
JELLYFIN_ERRORS = (aiohttp.ClientError, TimeoutError, JellyfinResponseError)
JELLYFIN_ERROR_MESSAGE = "Couldn't reach the Jellyfin server. Please try again later."

async def reportJellyfinError(ctx: discord.ApplicationContext):
    # Call from an except block. Commands defer first, so without a reply the
    # user would be left looking at "thinking..." until Discord gives up.
    log.exception('Jellyfin request failed')
    await ctx.respond(JELLYFIN_ERROR_MESSAGE)

def getItemLength(item: dict) -> int:
    """Length in seconds, 0 if Jellyfin doesn't know it."""
    ticks = item.get('RunTimeTicks')
    return max(0, ticks // 10000000) if isinstance(ticks, int) else 0

def getItemEntry(item: dict) -> dict:
    return {
        "Artists": item.get("Artists") or [],
        "Name": item["Name"],
        "Id": item['Id'],
        "Length": getItemLength(item)
    }

async def playHelperGeneric(item: dict, ctx: discord.ApplicationContext, position: str,
                            announcement: str | None = None):
    # announcement ("Playing X") is sent once the tracks are known, so a failed
    # or empty album lookup doesn't follow it with an error.
    entries = []
    if item["Type"] == "MusicAlbum":
        try:
            tracks = await JF_APICLIENT.getAlbumTracks(item['Id'])
        except JELLYFIN_ERRORS:
            await reportJellyfinError(ctx)
            return
        entries = [getItemEntry(i) for i in tracks]
    else:
        entries = [getItemEntry(item)]

    if not entries:
        # an empty queue would make the player pop from an empty list
        log.warning('Album %s has no tracks, nothing queued', item['Id'])
        await ctx.respond('That album has no tracks to play')
        return

    if announcement:
        await ctx.respond(announcement)

    fairplay = config.get('fairplay')

    # insert the item(s) into the queue
    global queues
    # ensure the queue for the guild (server) exists
    if not ctx.guild_id in queues:
        # fairplay requires per user queues, otherwise just one queue per guild
        if not fairplay:
            queues[ctx.guild_id] = []
        else:
            queues[ctx.guild_id] = {}

    if fairplay:
        if not ctx.author.id in queues[ctx.guild_id]:
            queues[ctx.guild_id][ctx.author.id] = []

    # insert into appropriate position in the queue
    if not fairplay:
        if position == 'last':
            queues[ctx.guild_id].extend(entries)
        else:
            queues[ctx.guild_id][0:0] = entries
    else:
        if position == 'last':
            queues[ctx.guild_id][ctx.author.id].extend(entries)
        else:
            queues[ctx.guild_id][ctx.author.id][0:0] = entries


    # If the bot is leaving voice right now, the voice client it still has is
    # about to go away: wait, then start a new session like after the leave.
    await waitUntilLeft(ctx.guild_id)
    if not ctx.voice_client:
        if pruneEmptyQueues(ctx.guild_id):  # not emptied (/stop) while waiting
            await startPlayer(ctx)
    elif position == 'now':
        # Skip whatever is playing (or loading) now, unless it is one of this
        # request's own tracks: after waiting for a leave, a new session may
        # already have started from the front of the queue, where they are.
        # Queue entries are distinct dicts and the popped one becomes the
        # playing entry, so identity tells them apart even for duplicates.
        # (While a new session is still connecting nothing is selected yet,
        # so the skip has nothing to stop; it then starts from the front.)
        current = playing.get(ctx.guild_id)
        if not any(current is e for e in entries):
            skipCurrentTrack(ctx.guild)

async def startPlayer(ctx: discord.ApplicationContext):
    vc = ctx.voice_client
    if not vc:
        av = ctx.author.voice
        if av:
            expectedDisconnects.pop(ctx.guild.id, None)
            bumpVoiceEpoch(ctx.guild.id)
            vc = await av.channel.connect()
            await playTrack(ctx.guild)

async def playTrack(guild: discord.Guild):
    vc = guild.voice_client
    if vc.is_paused():
        vc.resume()
    else:
        await advanceTrack(guild)

def skipCurrentTrack(guild: discord.Guild):
    """Skip the playing track, or the next one if it is still loading."""
    if guild.id in loading:
        loading[guild.id].set()
    if guild.voice_client:
        guild.voice_client.stop()

def pruneEmptyQueues(guildId: int) -> bool:
    """Drop empty queues for the guild. Returns True if any tracks are left."""
    queue = queues.get(guildId)
    if queue is None:
        return False
    if config.get('fairplay'):
        for userId in [uid for uid, tracks in queue.items() if not tracks]:
            queue.pop(userId)
    if not queue:
        queues.pop(guildId)
        return False
    return True

def popNextEntry(guildId: int) -> dict | None:
    """Take the next track off the guild's queue and make it the playing entry.
    Returns None when the queue is empty. Must run on the event loop."""
    if not pruneEmptyQueues(guildId):
        return None

    fairplay = config.get('fairplay')
    thisTrackUser = None
    if fairplay:
        thisTrackUserIdx = playing[guildId].get('nextTrackUserIdx', 0) % len(queues[guildId]) if guildId in playing else 0
        thisTrackUser = list(queues[guildId].keys())[thisTrackUserIdx]
    # grab the next item off the queue to play
    entry = queues[guildId].pop(0) if not fairplay else queues[guildId][thisTrackUser].pop(0)

    if fairplay:
        # find the next user to play from
        userIds = list(queues[guildId].keys())
        thisIdx = userIds.index(thisTrackUser) if thisTrackUser in userIds else -1
        # clean up any empty queues for users
        if not queues[guildId][thisTrackUser]:
            queues[guildId].pop(thisTrackUser)
            # the following user has shifted into thisIdx; wrap if this user was last
            remaining = len(queues[guildId])
            nextIdx = thisIdx % remaining if remaining else 0
        else:
            nextIdx = (thisIdx + 1) % len(userIds)

        entry['nextTrackUserIdx'] = nextIdx

    if not queues[guildId]:
        queues.pop(guildId)

    entry['playtime-offset'] = datetime.timedelta()
    entry['starttime'] = datetime.datetime.now()
    entry['paused'] = False
    playing[guildId] = entry
    return entry

def popRadioEntry(guildId: int, state: dict) -> dict:
    """Take the next track off the radio buffer and make it the playing entry.
    The buffer must not be empty. Must run on the event loop."""
    entry = state['buffer'].pop(0)
    # Radio tracks belong to no user. They only play when every user's queue
    # is empty, so the fairplay rotation starts over after them.
    entry['nextTrackUserIdx'] = 0
    entry['playtime-offset'] = datetime.timedelta()
    entry['starttime'] = datetime.datetime.now()
    entry['paused'] = False
    playing[guildId] = entry
    return entry

async def fetchRadioTracks(state: dict) -> tuple[list[dict], str | None]:
    """Pick what radio plays next: one random song, or the tracks of a random
    album in album order. Returns the entries and the album's name (None in
    song mode). Raises if Jellyfin fails or has nothing to play."""
    album = None
    if state['mode'] == 'album':
        albums = await JF_APICLIENT.getRandomItems('MusicAlbum', state['libraryId'])
        if not albums:
            raise LookupError('No albums found for radio')
        album = albums[0]['Name']
        tracks = await JF_APICLIENT.getAlbumTracks(albums[0]['Id'])
        # an album folder can hold more than its songs
        tracks = [t for t in tracks if t.get('Type', 'Audio') == 'Audio']
        if not tracks:
            raise LookupError(f'Radio album {albums[0]["Id"]} has no tracks')
    else:
        tracks = await JF_APICLIENT.getRandomItems('Audio', state['libraryId'])
        if not tracks:
            raise LookupError('No songs found for radio')

    entries = []
    for track in tracks:
        entry = getItemEntry(track)
        entry['Radio'] = True
        entry['Album'] = album
        entries.append(entry)
    return entries, album

async def fillRadioBuffer(guild: discord.Guild, state: dict):
    """Refill an empty radio buffer. Commands keep running meanwhile, so the
    caller has to look at the queue, the voice client and the radio state
    again afterwards. A failed pick is handled here (see radioFailed)."""
    try:
        entries, album = await asyncio.wait_for(fetchRadioTracks(state), RADIO_FETCH_TIMEOUT)
    except Exception as exc:
        # also a TimeoutError, or an item missing the fields an entry needs
        await radioFailed(guild, state, exc)
        return
    # Written even if radio was changed meanwhile: nothing reads the old state.
    state['buffer'] = entries
    state['album'] = album

async def radioFailed(guild: discord.Guild, state: dict, error: Exception):
    """A radio pick or a radio track failed. Waits before the next attempt,
    or turns radio off after RADIO_MAX_FAILURES in a row. Either way the
    caller looks again afterwards."""
    state['failures'] += 1
    log.error('Radio failed in guild %s (attempt %d/%d)', guild.id, state['failures'], RADIO_MAX_FAILURES,
              exc_info=error)
    if radioStates.get(guild.id) is not state:
        return  # turned off or changed meanwhile
    # Nothing is playing while radio waits. (After a failed load the caller
    # has already noted whether the next track should start paused.)
    playing.pop(guild.id, None)
    if state['failures'] >= RADIO_MAX_FAILURES:
        log.error('Turning radio off in guild %s after %d failures in a row', guild.id, state['failures'])
        radioStates.pop(guild.id)
        channel = state.get('channel')
        if channel is not None:
            try:
                await channel.send('Radio turned off: too many tracks failed to play in a row')
            except Exception:
                log.warning('Could not announce that radio was turned off in guild %s', guild.id, exc_info=True)
        return

    delay = min(RADIO_BACKOFF_BASE * 2 ** (state['failures'] - 1), RADIO_BACKOFF_MAX)
    deadline = time.monotonic() + delay
    vc = guild.voice_client
    # Cut the wait short if there is something else to do: a track was queued,
    # radio was turned off or changed, or the voice connection needs a look.
    while time.monotonic() < deadline:
        if (radioStates.get(guild.id) is not state or guild.id in queues
                or guild.voice_client is not vc or vc is None or not vc.is_connected()):
            return
        await asyncio.sleep(min(RADIO_POLL_INTERVAL, max(0, deadline - time.monotonic())))

# How long ffmpeg waits on a stalled stream read (or connect) before giving up
# with an error, in seconds. Jellyfin can take a few seconds to transcode the
# first segment, so leave plenty of room. Without it a stalled stream blocks
# ffmpeg forever, both while loading and mid-track in py-cord's player thread.
STREAM_IO_TIMEOUT = 30
# How long advanceTrack waits for a track to load before dropping it. A
# backstop for loads ffmpeg's own timeout doesn't end; longer than it, so a
# load that ffmpeg would still finish isn't cut short.
TRACK_LOAD_TIMEOUT = 45
# ffmpeg input options for a track's stream; -rw_timeout is in microseconds
STREAM_INPUT_OPTIONS = f'-rw_timeout {STREAM_IO_TIMEOUT * 1_000_000}'

class EmptyStreamError(Exception):
    """ffmpeg produced no audio for a radio track."""

def loadAudio(url: str) -> discord.FFmpegOpusAudio:
    """Start ffmpeg and wait for the first packet. Blocking, so run it in a
    thread; it only touches the new audio source, never shared state.
    The source's startedEmpty says whether the stream ended without a packet."""
    audio = discord.FFmpegOpusAudio(
        url,
        codec='copy',
        before_options=STREAM_INPUT_OPTIONS,
    )
    try:
        audio.startedEmpty = not audio.read()
    except BaseException:
        audio.cleanup()
        raise
    return audio

def cleanupAbandonedLoad(load: asyncio.Future):
    """Done-callback for a load nobody waits for any more: stop the ffmpeg
    process it started. Runs on the event loop."""
    if load.cancelled() or load.exception() is not None:
        return
    try:
        load.result().cleanup()
    except Exception:
        log.exception('Failed to clean up an abandoned track load')

async def waitForLoad(url: str, skipRequested: asyncio.Event) -> discord.FFmpegOpusAudio | None:
    """Run loadAudio in a worker thread. Returns its audio source, or None if a
    skip was requested first. Raises TimeoutError after TRACK_LOAD_TIMEOUT.
    The thread can't be interrupted, so when this stops waiting for it (skip,
    timeout or cancellation), the source it eventually returns is cleaned up."""
    load = asyncio.get_running_loop().run_in_executor(None, loadAudio, url)
    skipWait = asyncio.ensure_future(skipRequested.wait())
    handedOver = False
    try:
        await asyncio.wait({load, skipWait}, timeout=TRACK_LOAD_TIMEOUT,
                           return_when=asyncio.FIRST_COMPLETED)
        if load.done():
            # if a skip came in too, the caller drops (and cleans up) the source
            audio = load.result()
            handedOver = True
            return audio
        if skipRequested.is_set():
            return None
        raise TimeoutError(f'Track did not start within {TRACK_LOAD_TIMEOUT} seconds')
    finally:
        skipWait.cancel()
        if not handedOver:
            # Also covers a load that already finished, e.g. when this was
            # cancelled before it could resume: done-callbacks still run then.
            load.add_done_callback(cleanupAbandonedLoad)

def onTrackEnd(loop: asyncio.AbstractEventLoop, guild: discord.Guild, error: Exception | None, player: object = None):
    # Runs on the voice player thread: hand off to the event loop and return.
    asyncio.run_coroutine_threadsafe(advanceTrack(guild, error, player), loop)

# How many tracks advanceTrack will try to start, one after another, before
# giving up and leaving the voice channel.
MAX_START_ATTEMPTS = 3

async def advanceTrack(guild: discord.Guild, error: Exception | None = None, endedPlayer: object = None):
    """Play the next queued track, or the next radio track if nothing is queued
    and radio is on, or leave the voice channel if there is neither.
    endedPlayer is the token of the player whose after= callback triggered this."""
    if error is not None:
        log.error('Playback error in guild %s', guild.id, exc_info=error)
    lock = playerLocks.setdefault(guild.id, asyncio.Lock())
    try:
        async with lock:
            await _advanceTrack(guild, endedPlayer)
    except Exception:
        # Nothing awaits this when it comes from onTrackEnd, so log here.
        log.exception('Could not start the next track in guild %s', guild.id)

async def _advanceTrack(guild: discord.Guild, endedPlayer: object = None):
    failures = 0
    startPaused = False
    while True:
        vc = guild.voice_client
        if vc is not None and not vc.is_connected():
            # py-cord keeps the voice client and reconnects in the background.
            # Nothing else would restart playback once it's back, so wait here.
            if await waitForVoiceRecovery(guild, vc):
                # The now-playing entry holds the latest /pause or /resume
                # (made during the wait, or carried over from a failed start),
                # so it decides whether the next track starts paused.
                startPaused = playing.get(guild.id, {}).get('paused', False)
                continue  # recovered, or the voice client changed: look again
            return  # gave up and left voice
        if vc is None:
            # Kicked, or py-cord gave up on the connection, before we got here.
            log.info('No voice connection in guild %s, stopping playback', guild.id)
            playing.pop(guild.id, None)
            radioStates.pop(guild.id, None)
            return
        if endedPlayer is not None and currentPlayers.get(guild.id) is endedPlayer:
            # The player that called after= has exited, and no player was
            # started since (only this coroutine starts them, under the lock).
            currentPlayers.pop(guild.id)
            if vc.is_playing() or vc.is_paused():
                # py-cord's player gave up waiting for a dropped connection
                # and returned without stop(), so it still reports itself as
                # playing. Clear it, or nothing could ever play again.
                log.warning('Voice player in guild %s stopped after losing its connection', guild.id)
                vc.stop()
        if vc.is_playing() or vc.is_paused():
            # a track is already playing; only its end may advance the queue
            return
        if failures >= MAX_START_ATTEMPTS:
            log.error('Giving up on playback in guild %s after %d failed attempts', guild.id, failures)
            await stopPlayback(guild, vc)
            return

        # Queued tracks always go before radio.
        entry = popNextEntry(guild.id)
        radio = None
        if entry is None:
            radio = radioStates.get(guild.id)
            if radio is None:
                await stopPlayback(guild, vc)
                return
            if not radio['buffer']:
                await fillRadioBuffer(guild, radio)
                # A track may have been queued, radio changed or the voice
                # connection lost while Jellyfin was asked: look again.
                continue
            entry = popRadioEntry(guild.id, radio)
        if startPaused:
            entry['paused'] = True
            # Carried over now; from here the entry's own state is what counts,
            # so a /resume while it loads isn't undone if it fails.
            startPaused = False

        log.info('Playing %s (%s) in guild %s', entry['Name'], entry['Id'], guild.id)
        # Commands keep running while ffmpeg starts. A skip, /stop or
        # when=now in that window sets loading[guild.id], so this track is
        # dropped right away, even if its load is stuck.
        skipRequested = loading[guild.id] = asyncio.Event()
        loadError = None
        audio = None
        try:
            url = JF_APICLIENT.getAudioHls(entry["Id"], vc.channel.bitrate)
            audio = await waitForLoad(url, skipRequested)
        except Exception as exc:
            # includes TimeoutError: a stalled load counts as a failure
            loadError = exc
        finally:
            loading.pop(guild.id)
        if radio is not None and audio is not None and getattr(audio, 'startedEmpty', False):
            # Jellyfin failing to transcode shows up as a stream without
            # audio. A queued track like that is over at once and the next
            # one plays; radio would never run out of next ones, so for a
            # radio track it counts as a failure.
            audio.cleanup()
            audio = None
            loadError = EmptyStreamError(f'No audio received for radio track {entry["Id"]}')
        skipped = skipRequested.is_set()
        if skipped:
            # Skipping a paused track drops its pause, so a later outage
            # recovery doesn't carry it over to the next one.
            entry['paused'] = False

        if loadError is not None:
            if skipped:
                # skipped anyway, so it doesn't count towards giving up
                log.warning('Skipped track %s in guild %s failed to load', entry["Id"], guild.id, exc_info=loadError)
            elif radio is not None:
                # Radio waits and tries another track, with its own limit.
                startPaused = entry['paused']
                await radioFailed(guild, radio, loadError)
            else:
                failures += 1
                log.error('Failed to start the next track in guild %s (attempt %d/%d)',
                          guild.id, failures, MAX_START_ATTEMPTS, exc_info=loadError)
                # The track never started, so keep it paused (or not) as it is
                # now on the next one. A skip drops it, like skipping a paused track.
                startPaused = entry['paused']
            continue
        if (skipped or guild.voice_client is not vc or not vc.is_connected()
                or (radio is not None and radioStates.get(guild.id) is not radio)):
            # Also a radio track that loaded while radio was turned off or
            # changed: that takes effect after the current track, and this
            # one hasn't started.
            if audio is not None:
                audio.cleanup()
            continue

        # don't count loading time as played time
        entry['playtime-offset'] = datetime.timedelta()
        entry['starttime'] = datetime.datetime.now()
        loop = asyncio.get_running_loop()
        player = object()
        try:
            vc.play(audio, after=lambda e: onTrackEnd(loop, guild, e, player))
        except Exception:
            # The player never took ownership of the source, so stop ffmpeg here.
            audio.cleanup()
            if vc.is_playing() or vc.is_paused():
                # py-cord stores the player before starting its thread; if
                # starting it failed, that player never runs or calls after=.
                # Only this coroutine starts players (under the lock), so it is
                # ours: clear it so the next attempt isn't mistaken for playing.
                vc.stop()
            failures += 1
            log.exception('Failed to start the next track in guild %s (attempt %d/%d)',
                          guild.id, failures, MAX_START_ATTEMPTS)
            startPaused = entry['paused']
            continue
        currentPlayers[guild.id] = player
        if radio is not None:
            radio['failures'] = 0
        if entry['paused']:
            # /pause arrived while the track was loading
            vc.pause()
        return

async def waitForVoiceRecovery(guild: discord.Guild, vc) -> bool:
    """Wait for py-cord to recover vc's dropped connection. Returns True once
    it is connected again or is no longer the guild's voice client (kicked,
    py-cord gave up), so the caller can look again. If it doesn't recover
    within VOICE_OUTAGE_TIMEOUT, or the queue is empty and radio is off
    (/stop, /clear, or nothing left to play), leaves voice and returns False."""
    log.info('Voice connection in guild %s is down, waiting for it to recover', guild.id)
    deadline = time.monotonic() + VOICE_OUTAGE_TIMEOUT
    while guild.voice_client is vc and not vc.is_connected():
        if not pruneEmptyQueues(guild.id) and guild.id not in radioStates:
            # Nothing would play after recovery, so don't wait for it.
            log.info('Nothing queued in guild %s while its voice connection is down, leaving voice', guild.id)
            await stopPlayback(guild, vc)
            return False
        if time.monotonic() >= deadline:
            log.warning('Voice connection in guild %s did not recover within %s seconds, leaving voice',
                        guild.id, VOICE_OUTAGE_TIMEOUT)
            await stopPlayback(guild, vc)
            return False
        await asyncio.sleep(VOICE_POLL_INTERVAL)
    if guild.voice_client is vc:
        log.info('Voice connection in guild %s recovered', guild.id)
    return True

def cancelVoiceReconnect(vc):
    """Stop py-cord from reconnecting vc after we disconnect it.

    In py-cord 2.8.1, VoiceClient.disconnect() doesn't cancel the connection's
    background task (VoiceConnectionState._runner); only connect() and
    soft_disconnect() do. If that task is asleep in its reconnect backoff, it
    later rejoins voice with a voice client that has already been removed.
    There is no public API for this, so this reaches into private attributes.
    If py-cord renames them, it does nothing."""
    try:
        runner = getattr(getattr(vc, '_connection', None), '_runner', None)
        if isinstance(runner, asyncio.Task) and runner is not asyncio.current_task():
            runner.cancel()
    except Exception:
        log.debug('Could not cancel the voice reconnect task', exc_info=True)

async def waitUntilLeft(guildId: int):
    """If stopPlayback is leaving voice in this guild, wait until it is done.
    Doesn't yield otherwise, so callers can check the queue and voice client
    in the same step."""
    while (left := leaving.get(guildId)) is not None:
        await left.wait()

async def stopPlayback(guild: discord.Guild, vc):
    """Forget the current track and leave the voice channel, turning radio
    off. Tracks still queued are kept, so /start can pick them up. /play and /start during the
    leave wait for it to finish (waitUntilLeft) and then start a new session."""
    # Before any await, so a command can't see the old voice client without it.
    left = leaving[guild.id] = asyncio.Event()
    playing.pop(guild.id, None)
    currentPlayers.pop(guild.id, None)
    # Radio ends with the voice session. Unlike queued tracks nobody picked
    # these, so there is nothing to keep for /start.
    radioStates.pop(guild.id, None)
    # tell onVoiceStateUpdate this leave is ours, so it keeps the queue
    expectedDisconnects[guild.id] = math.inf
    # A listener still waiting on an earlier leave (e.g. py-cord's reconnect)
    # must not take this leave for a kick: py-cord may not deliver a new leave
    # event if the bot is already out of the channel.
    bumpVoiceEpoch(guild.id)
    cancelVoiceReconnect(vc)
    try:
        await vc.disconnect(force=True)
    except Exception:
        log.exception('Failed to disconnect from voice in guild %s', guild.id)
        # still connected, so a later leave is not ours
        expectedDisconnects.pop(guild.id, None)
    else:
        if guild.id in expectedDisconnects:
            expectedDisconnects[guild.id] = time.monotonic() + EXPECTED_LEAVE_GRACE
    finally:
        if leaving.get(guild.id) is left:
            leaving.pop(guild.id)
        left.set()

def clearGuildState(guildId: int):
    queues.pop(guildId, None)
    playing.pop(guildId, None)
    currentPlayers.pop(guildId, None)
    radioStates.pop(guildId, None)

def bumpVoiceEpoch(guildId: int) -> int:
    voiceEpochs[guildId] = voiceEpochs.get(guildId, 0) + 1
    return voiceEpochs[guildId]

async def onVoiceStateUpdate(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Drop the guild's queue and now-playing state when the bot is kicked or
    disconnected from voice by someone else, so a later /play starts fresh."""
    if bot.user is None or member.id != bot.user.id:
        return
    if before.channel is None or after.channel is not None:
        return

    guild = member.guild
    epoch = bumpVoiceEpoch(guild.id)
    ownLeaveDeadline = expectedDisconnects.pop(guild.id, None)
    if ownLeaveDeadline is not None and time.monotonic() <= ownLeaveDeadline:
        # The bot left on its own (stopPlayback); queued tracks are kept for /start.
        return

    # py-cord also leaves and rejoins by itself while recovering a dropped voice
    # connection, keeping the same voice client. Only clear state once the voice
    # client is actually gone.
    vc = guild.voice_client
    deadline = time.monotonic() + VOICE_RECOVERY_TIMEOUT
    while vc is not None and guild.voice_client is vc and not vc.is_connected():
        if time.monotonic() >= deadline:
            return
        await asyncio.sleep(VOICE_POLL_INTERVAL)
    # Skip if a later leave or a new voice session happened while waiting.
    if guild.voice_client is None and voiceEpochs.get(guild.id) == epoch:
        log.info('Removed from voice in guild %s, clearing its queue', guild.id)
        clearGuildState(guild.id)

bot.add_listener(onVoiceStateUpdate, 'on_voice_state_update')


def getTrackString(item: dict, artistLimit: int = 1, showType: bool = False, showSize: bool = False):

    if not showType:
        res = ''
    elif item["Type"] == "MusicAlbum":
        res = 'Album: '
    else:
        res = 'Track: '

    artists = item.get("Artists") or []
    if len(artists) > artistLimit:
        res += 'Various Artists'
    elif artists:
        res += ','.join(artists)
    
    if artists:
        res += ' - '
    
    res += item["Name"]

    # An unknown length (0) is left out rather than shown as 00:00.
    secs = getItemLength(item)
    if showSize and item["Type"] == "Audio" and secs:
        res += f' ({formatTimeSecs(secs, secs >= 3600)})'
    return res

def formatTimeSecs(secs: int, force_hrs: bool = False) -> str:
    s = secs % 60
    m = secs // 60 % 60
    h = secs // 3600
    
    if h or force_hrs:
        return f'{h}:{m:02d}:{s:02d}'
    else:
        return f'{m:02d}:{s:02d}'

def getFairplayQueueOrder(guildId: int) -> list[tuple[int, int]]:
    """(userId, position in that user's queue) for each fairplay queue entry,
    in the order they will play."""
    if not guildId in queues:
        return []
    # nothing may be playing, e.g. after playback gave up with tracks still queued
    nextTrackUserIdx = playing.get(guildId, {}).get('nextTrackUserIdx', 0) % len(queues[guildId])
    userIds = list(queues[guildId].keys())
    orderedUserIds = userIds[nextTrackUserIdx:]+userIds[:nextTrackUserIdx]

    order = []
    longest = max(len(queues[guildId][uid]) for uid in orderedUserIds)
    for pos in range(longest):
        order.extend((uid, pos) for uid in orderedUserIds if pos < len(queues[guildId][uid]))
    return order

def getFlattenedPlayQueue(guildId: int) -> list[dict]:
    if not guildId in queues:
        return []
    if not config.get('fairplay'):
        return queues[guildId]
    else:
        return [queues[guildId][uid][pos] for uid, pos in getFairplayQueueOrder(guildId)]

def removeFairplayUser(guildId: int, userId: int):
    """Drop a user's queue, keeping the user who is due next due next."""
    userIds = list(queues[guildId].keys())
    removedIdx = userIds.index(userId)
    queues[guildId].pop(userId)
    remaining = len(queues[guildId])
    if guildId in playing and 'nextTrackUserIdx' in playing[guildId]:
        nextIdx = playing[guildId]['nextTrackUserIdx'] % len(userIds)
        if removedIdx < nextIdx:
            nextIdx -= 1
        # if removedIdx == nextIdx the following user shifts into nextIdx;
        # wrap if the removed user was last
        playing[guildId]['nextTrackUserIdx'] = nextIdx % remaining if remaining else 0
    if not remaining:
        queues.pop(guildId)

def shuffleFairplayUsers(guildId: int):
    """Shuffle the user rotation. The user due next stays next, so the user
    who just played can't play again right away; the others are shuffled after them."""
    userIds = list(queues[guildId].keys())
    # nothing may be playing; popNextEntry then starts from the first user
    nextIdx = playing.get(guildId, {}).get('nextTrackUserIdx', 0) % len(userIds)
    others = userIds[nextIdx+1:]+userIds[:nextIdx]
    random.shuffle(others)
    queues[guildId] = {uid: queues[guildId][uid] for uid in [userIds[nextIdx]]+others}
    if guildId in playing:
        playing[guildId]['nextTrackUserIdx'] = 0

'''
Discord View Related
'''
class searchDropdown(discord.ui.Select):
    def __init__(self, items: list[dict], ctx: discord.ApplicationContext, when: str, showType: bool = True):
        super(searchDropdown, self).__init__()
        self.ctx = ctx
        self.when = when
        self.items = items
        self.max_values = 1
        self.min_values = 1
        for i in range(min(len(items), 25)):
            label = getTrackString(items[i], showType=showType, showSize=True)
            label = label[:97]+'...' if len(label) > 100 else label
            self.add_option(label = label, value = str(i))
    
    async def callback(self, interaction: discord.Interaction):
        # Stop the view before awaiting anything: removing the view from the
        # message doesn't cancel its timeout, so on_timeout would otherwise
        # replace "Playing X" with "Selection timed out." later.
        self.view.stop()
        await interaction.response.edit_message(content=f'Playing {getTrackString(self.items[int(self.values[0])], showType=True)}',view=None)
        await playHelperGeneric(self.items[int(self.values[0])], self.ctx, self.when)

class ownerOnlyView(discord.ui.View):
    # For views on public replies: only the user who ran the command may use
    # them. Others get checkFailureMessage as an ephemeral reply.
    checkFailureMessage = 'Only the user who ran this command can use this.'

    def __init__(self, ownerId: int):
        super(ownerOnlyView, self).__init__()
        self.ownerId = ownerId

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.ownerId

    async def on_check_failure(self, interaction: discord.Interaction):
        await interaction.response.send_message(self.checkFailureMessage, ephemeral=True)

class searchView(ownerOnlyView):
    # The track is queued (and the bot joins voice) on the invoker's behalf.
    checkFailureMessage = 'Only the user who ran this search can pick an item.'

    async def on_timeout(self):
        self.disable_all_items()
        await self.message.edit("Selection timed out.", view=None)

def getPageCount(trackCount: int) -> int:
    # Always at least one page, so an empty queue still shows "Page: 1/1".
    return max(1, math.ceil(trackCount / PLAYLIST_PAGESIZE))

def getQueuePageText(tracks: list, page: int) -> str:
    # Numbering is 1-based, matching /remove, /promote, /demote and /playnow.
    first = page*PLAYLIST_PAGESIZE
    strs = [f'{first+i+1}. {getTrackString(track)}' for i, track in enumerate(tracks[first:first+PLAYLIST_PAGESIZE])]
    return f'Tracks in playlist:\n{"\n".join(strs)}\nPage: {page+1}/{getPageCount(len(tracks))}'

def getRadioStatusLine(guildId: int) -> str:
    """One line for /queue saying what radio will do, '' if radio is off. The
    rest of a radio album isn't in the queue, so /queue shows this instead."""
    state = radioStates.get(guildId)
    if not state:
        return ''
    if state['mode'] == 'album':
        if state['buffer']:
            album = state['album'] if len(state['album']) <= 100 else state['album'][:97]+'...'
            return f'Radio is on: continuing album {album} when the queue is empty'
        return 'Radio is on: random albums play when the queue is empty'
    return 'Radio is on: random songs play when the queue is empty'

def withRadioStatus(guildId: int, text: str) -> str:
    radioLine = getRadioStatusLine(guildId)
    return f'{text}\n{radioLine}' if radioLine else text

async def getMusicLibraries() -> list[dict]:
    """The server's music libraries, cached for LIBRARY_CACHE_SECS."""
    now = time.monotonic()
    if musicLibraryCache['time'] is None or now - musicLibraryCache['time'] > LIBRARY_CACHE_SECS:
        musicLibraryCache['items'] = await JF_APICLIENT.getMusicLibraries()
        musicLibraryCache['time'] = now
    return musicLibraryCache['items']

async def libraryAutocomplete(ctx: discord.AutocompleteContext):
    try:
        libraries = await getMusicLibraries()
    except JELLYFIN_ERRORS:
        log.warning('Could not list the music libraries', exc_info=True)
        return []
    typed = (ctx.value or '').lower()
    matches = [lib for lib in libraries if typed in lib['Name'].lower()]
    return [discord.OptionChoice(name=lib['Name'][:100], value=lib['Id']) for lib in matches[:SELECT_MAX_OPTIONS]]

def getPageSelectorRange(pages: int, page: int) -> range:
    # Discord allows at most 25 select options: show a window of 25 pages
    # around the current one.
    start = max(0, min(page - SELECT_MAX_OPTIONS//2, pages - SELECT_MAX_OPTIONS))
    return range(start, min(pages, start + SELECT_MAX_OPTIONS))

class listDropdown(discord.ui.Select):
    def __init__(self):
        super(listDropdown, self).__init__()
        self.max_values = 1
        self.min_values = 1

    async def callback(self, interaction: discord.Interaction):
        await self.view.showPage(interaction, int(self.values[0])-1)

    def update_options(self, pages: int, page: int):
        self.options = [discord.SelectOption(label=str(i+1), value=str(i+1), default=i == page)
                        for i in getPageSelectorRange(pages, page)]

class listPrevButton(discord.ui.Button):
    def __init__(self):
        super(listPrevButton, self).__init__()
        self.label = '◁'

    async def callback(self, interaction: discord.Interaction):
        await self.view.showPage(interaction, self.view.page - 1)

class listNextButton(discord.ui.Button):
    def __init__(self):
        super(listNextButton, self).__init__()
        self.label = '▷'

    async def callback(self, interaction: discord.Interaction):
        await self.view.showPage(interaction, self.view.page + 1)


class listRefreshButton(discord.ui.Button):
    def __init__(self):
        super(listRefreshButton, self).__init__()
        self.label = '⟳'

    async def callback(self, interaction: discord.Interaction):
        await self.view.showPage(interaction, self.view.page)


class listView(ownerOnlyView):
    # Paging edits the shared message, so only the invoker may page.
    checkFailureMessage = 'Only the user who ran /queue can change its page. Run /queue to get your own view.'

    def __init__(self, ownerId: int, pages: int):
        super(listView, self).__init__(ownerId)
        self.page = 0
        self.selection = listDropdown()
        self.prevButton = listPrevButton()
        self.nextButton = listNextButton()
        self.updateItems(pages)
        self.add_item(self.prevButton)
        self.add_item(listRefreshButton())
        self.add_item(self.nextButton)
        self.add_item(self.selection)

    def updateItems(self, pages):
        self.prevButton.disabled = self.page == 0
        self.nextButton.disabled = self.page == pages - 1
        self.selection.update_options(pages, self.page)

    async def showPage(self, interaction: discord.Interaction, page: int):
        tracks = getFlattenedPlayQueue(interaction.guild_id)
        pages = getPageCount(len(tracks))
        # The queue may have shrunk since the view was last rendered.
        self.page = max(0, min(page, pages - 1))
        self.updateItems(pages)
        content = withRadioStatus(interaction.guild_id, getQueuePageText(tracks, self.page))
        await interaction.response.edit_message(content=content, view=self)

    async def on_timeout(self):
        self.disable_all_items()
        await self.message.edit(view=self)

'''
Bot Commands

Handlers change queues/playing before awaiting ctx.respond(): a track can end,
and advanceTrack() run, while the reply is being sent to Discord.
'''
# when=now skips the playing track, which in fairplay mode may be another
# user's, and the rotation, not the requester, still decides what plays next.
# Like /playnow it isn't offered there (#83).
WHEN_CHOICES = ['next', 'last'] if config.get('fairplay') else ['now', 'next', 'last']
WHEN_NOW_FAIRPLAY_MESSAGE = ("`when: now` isn't available in fairplay mode, since it would skip other users' tracks. "
                             "Use `when: next` to put the track at the front of your own queue.")

async def refuseWhenNowInFairplay(ctx: discord.ApplicationContext, when: str) -> bool:
    """Refuse when=now in fairplay mode. The option isn't offered there, but a
    client with stale command data or the E2E hook can still send it."""
    if when == 'now' and config.get('fairplay'):
        await ctx.respond(WHEN_NOW_FAIRPLAY_MESSAGE, ephemeral=True)
        return True
    return False

if DEBUG:
    cmdgrp = bot.create_group(config['command-group'], guild_ids=[DEBUG_SERVER])
else:
    cmdgrp = bot.create_group(config['command-group'])

@cmdgrp.command()
async def search(ctx: discord.ApplicationContext, 
                 term: discord.Option(str),
                 searchtype: discord.Option(str, name="type", choices=['Soundtrack', 'Album'], required=False),
                 when: discord.Option(str, choices=WHEN_CHOICES, required=False) = 'last'):
    if await refuseWhenNowInFairplay(ctx, when):
        return
    await ctx.defer(invisible=True)
    try:
        res = await searchHelper(term, searchType=searchtype)
    except JELLYFIN_ERRORS:
        await reportJellyfinError(ctx)
        return
    if not res:
        await ctx.respond("No items match your query.")
    elif not ctx.author.voice and not ctx.voice_client:
        await ctx.respond('You are not in any voice channel')
    else:
        view = searchView(ctx.author.id)
        view.add_item(searchDropdown(res, ctx, when, not searchtype))

        await ctx.respond('Select an item to play:', view=view)

@cmdgrp.command()
async def play(ctx: discord.ApplicationContext,
               term: discord.Option(str),
               searchtype: discord.Option(str, name='type', choices=['Soundtrack', 'Album'], required=False),
               when: discord.Option(str, choices=WHEN_CHOICES, required=False) = 'last'):
    if await refuseWhenNowInFairplay(ctx, when):
        return
    await ctx.defer(invisible=True)
    try:
        res = await searchHelper(term, limit=1, searchType=searchtype)
    except JELLYFIN_ERRORS:
        await reportJellyfinError(ctx)
        return
    # A voice client that is about to go away doesn't count (see stopPlayback).
    await waitUntilLeft(ctx.guild_id)

    if not res:
        await ctx.respond('No items match your query')
    elif not ctx.author.voice and not ctx.voice_client:
        await ctx.respond('You are not in any voice channel')
    else:
        await playHelperGeneric(res[0], ctx, when,
                                announcement=f'Playing {getTrackString(res[0], showType=True)}')

@cmdgrp.command()
async def skip(ctx: discord.ApplicationContext):
    if not ctx.voice_client:
        await ctx.respond('Not currently playing')
    else:
        skipCurrentTrack(ctx.guild)
        await ctx.respond('Skipping current track')

@cmdgrp.command()
async def nowplaying(ctx: discord.ApplicationContext):
    if ctx.guild_id in playing:
        track = playing[ctx.guild_id]
        td = track['playtime-offset']
        if not playing[ctx.guild_id]['paused']:
            td += datetime.datetime.now() - track['starttime']
        length = track.get("Length") or 0
        # Length 0 means Jellyfin didn't report one: show the elapsed time only.
        total = formatTimeSecs(length) if length else '--:--'
        radioTag = ''
        if track.get('Radio'):
            radioTag = f' [Radio: {track["Album"]}]' if track.get('Album') else ' [Radio]'
        await ctx.respond(f'Currently Playing: {getTrackString(track)} {formatTimeSecs(td.seconds, length >= 3600)}/{total}{radioTag}')
    else:
        await ctx.respond('Not Currently Playing')

@cmdgrp.command()
async def queue(ctx: discord.ApplicationContext):
    if ctx.guild_id in queues:
        tracks = getFlattenedPlayQueue(ctx.guild_id)
        await ctx.respond(withRadioStatus(ctx.guild_id, getQueuePageText(tracks, 0)),
                          view=listView(ctx.author.id, getPageCount(len(tracks))))
    else:
        await ctx.respond(withRadioStatus(ctx.guild_id, 'Empty Queue'))

@cmdgrp.command()
async def start(ctx: discord.ApplicationContext):
    if ctx.guild_id in leaving:
        # The voice client it still has is about to go away (see stopPlayback).
        # The leave can take up to py-cord's disconnect timeout, so defer.
        await ctx.defer()
        await waitUntilLeft(ctx.guild_id)
    if ctx.guild_id in queues and ctx.author.voice and not ctx.voice_client:
        await ctx.respond('Starting Playback')
        await startPlayer(ctx)
    elif ctx.guild_id not in queues:
        await ctx.respond('No tracks to play')
    elif ctx.guild_id in playing or ctx.voice_client:
        await ctx.respond('Already Playing')
    else:
        await ctx.respond('You are not in any voice channel')

@cmdgrp.command()
async def pause(ctx: discord.ApplicationContext):
    if not ctx.voice_client:
        await ctx.respond('Not connect to any voice channel')
    elif ctx.guild_id not in playing:
        await ctx.respond('Not Currently Playing')
    elif playing[ctx.guild_id]['paused']:
        await ctx.respond('Already paused')
    else:
        track = playing[ctx.guild_id]
        track['playtime-offset'] += datetime.datetime.now() - track['starttime']
        track['paused'] = True
        ctx.voice_client.pause()
        await ctx.respond('Pausing playback')

@cmdgrp.command()
async def resume(ctx: discord.ApplicationContext):
    if not ctx.voice_client:
        await ctx.respond('Not connect to any voice channel')
    elif ctx.guild_id not in playing:
        await ctx.respond('Not Currently Playing')
    elif not playing[ctx.guild_id]['paused']:
        await ctx.respond('Already playing')
    else:
        track = playing[ctx.guild_id]
        track['starttime'] = datetime.datetime.now()
        track['paused'] = False
        ctx.voice_client.resume()
        await ctx.respond('Resuming playback')

@cmdgrp.command()
async def stop(ctx: discord.ApplicationContext):
    if ctx.voice_client:
        queues.pop(ctx.guild_id, None)
        # Radio has to go too, or it would just pick another track.
        radioStates.pop(ctx.guild_id, None)
        skipCurrentTrack(ctx.guild)
        await ctx.respond('Stopping playback')
    else:
        await ctx.respond('Not connected to any voice channel')

@cmdgrp.command()
async def shuffle(ctx: discord.ApplicationContext):
    global queues
    if not ctx.guild_id in queues:
        await ctx.respond('Playlist is empty')
    else:
        if config.get('fairplay'):
            for userId in queues[ctx.guild_id]:
                random.shuffle(queues[ctx.guild_id][userId])

            shuffleFairplayUsers(ctx.guild_id)
        else:
            random.shuffle(queues[ctx.guild_id])
        await ctx.respond('Shuffling playlist')

@cmdgrp.command()
async def remove(ctx: discord.ApplicationContext,
                 index: discord.Option(int, min_value = 1)):
    global queues
    guildqueue = getFlattenedPlayQueue(ctx.guild_id)
    if not ctx.guild_id in queues:
        await ctx.respond('Queue is empty', ephemeral=True)
    elif len(guildqueue) < index:
        await ctx.respond('Specified index does not exist')
    else:
        if not config.get('fairplay'):
            item = queues[ctx.guild_id].pop(index-1)
            if not queues[ctx.guild_id]:
                queues.pop(ctx.guild_id)
            await ctx.respond(f'Deleted track: {getTrackString(item)}')
        else:
            userId, pos = getFairplayQueueOrder(ctx.guild_id)[index-1]
            item = queues[ctx.guild_id][userId].pop(pos)
            if not queues[ctx.guild_id][userId]:
                removeFairplayUser(ctx.guild_id, userId)
            await ctx.respond(f'Deleted track: {getTrackString(item)}')

@cmdgrp.command()
async def promote(ctx: discord.ApplicationContext,
                  index: discord.Option(int, min_value = 1)):
    global queues
    guildqueue = getFlattenedPlayQueue(ctx.guild_id)
    if not ctx.guild_id in queues:
        await ctx.respond('Queue is empty', ephemeral=True)
    elif len(guildqueue) < index:
        await ctx.respond('Specified index does not exist', ephemeral=True)
    else:
        if not config.get('fairplay'):
            item = queues[ctx.guild_id].pop(index-1)
            queues[ctx.guild_id].insert(0, item)
            await ctx.respond(f'Promoted track to the front: {getTrackString(item)}')
        else:
            userId, pos = getFairplayQueueOrder(ctx.guild_id)[index-1]
            if userId != ctx.author.id:
                await ctx.respond('You can only promote your own tracks in fairplay mode', ephemeral=True)
            else:
                userQueue = queues[ctx.guild_id][userId]
                item = userQueue.pop(pos)
                userQueue.insert(0, item)
                await ctx.respond(f'Promoted track to the front of your tracks: {getTrackString(item)}')

@cmdgrp.command()
async def demote(ctx: discord.ApplicationContext,
                  index: discord.Option(int, min_value = 1)):
    global queues
    guildqueue = getFlattenedPlayQueue(ctx.guild_id)
    if not ctx.guild_id in queues:
        await ctx.respond('Queue is empty')
    elif len(guildqueue) < index:
        await ctx.respond('Specified index does not exist', ephemeral=True)
    else:
        if not config.get('fairplay'):
            item = queues[ctx.guild_id].pop(index-1)
            queues[ctx.guild_id].append(item)
            await ctx.respond(f'Demoted track to the back: {getTrackString(item)}')
        else:
            userId, pos = getFairplayQueueOrder(ctx.guild_id)[index-1]
            if userId != ctx.author.id:
                await ctx.respond('You can only demote your own tracks in fairplay mode', ephemeral=True)
            else:
                userQueue = queues[ctx.guild_id][userId]
                item = userQueue.pop(pos)
                userQueue.append(item)
                await ctx.respond(f'Demoted track to the back of your tracks: {getTrackString(item)}')

def playNextIfStillQueued(guild: discord.Guild, entry: dict):
    """If entry is still waiting in the (non-fairplay) queue, move it to the
    front and skip whatever is playing or loading, so it plays next. Does
    nothing if it was already taken off the queue (playing, or removed)."""
    queue = queues.get(guild.id, [])
    waitingAt = next((i for i, e in enumerate(queue) if e is entry), None)
    if waitingAt is None:
        return
    queue.insert(0, queue.pop(waitingAt))
    if guild.voice_client:
        skipCurrentTrack(guild)

if not config.get('fairplay'):
    # playnow command is disabled in fairplay mode since it would defeat the whole point
    @cmdgrp.command()
    async def playnow(ctx: discord.ApplicationContext,
                    index: discord.Option(int, min_value=1)):
        global queues
        if ctx.guild_id in leaving:
            # Like /start: the voice client it still has is about to go away
            # (see stopPlayback), and the leave can take a while, so defer.
            await ctx.defer()
            await waitUntilLeft(ctx.guild_id)
        if ctx.guild_id not in queues:
            await ctx.respond('Playlist is empty')
        elif len(queues[ctx.guild_id]) < index:
            await ctx.respond('Specified index does not exist')
        elif not ctx.voice_client and not ctx.author.voice:
            # Nothing would play it, so leave the queue as it is, like /start
            # and /play refuse without changing anything.
            await ctx.respond('You are not in any voice channel')
        else:
            item = queues[ctx.guild_id].pop(index-1)
            queues[ctx.guild_id].insert(0, item)
            if ctx.voice_client:
                # Also while the connection is down (#64): advanceTrack is
                # waiting for it to recover and then plays from the front.
                skipCurrentTrack(ctx.guild)
                await ctx.respond(f'Now playing track: {getTrackString(item)}')
            else:
                # Not in voice (kept queue after a leave): start a session,
                # which plays from the front, where the track now is.
                await ctx.respond(f'Now playing track: {getTrackString(item)}')
                if not ctx.voice_client:
                    await startPlayer(ctx)
                # Another /play or /start may have run while the reply was
                # sent or while connecting, and queued or started something
                # ahead of this track.
                playNextIfStillQueued(ctx.guild, item)

@cmdgrp.command()
async def clear(ctx: discord.ApplicationContext):
    global queues
    if not ctx.guild_id in queues:
        await ctx.respond('Playlist is empty')
    else:
        queues.pop(ctx.guild_id)
        await ctx.respond('Playlist cleared')

@cmdgrp.command()
async def radio(ctx: discord.ApplicationContext,
                mode: discord.Option(str, choices=['song', 'album', 'off']),
                library: discord.Option(str, autocomplete=libraryAutocomplete, required=False) = None):
    if mode == 'off':
        # Only the fallback goes: the current track and the queue still play,
        # then the bot leaves as it does without radio.
        if radioStates.pop(ctx.guild_id, None) is None:
            await ctx.respond('Radio is not on')
        else:
            await ctx.respond('Radio turned off, playback ends after the current track and the queue')
        return

    await ctx.defer()
    libraryId = None
    libraryName = None
    try:
        if library:
            # The option holds a library id when picked from the suggestions,
            # or whatever was typed otherwise: accept a library's name too.
            for lib in await getMusicLibraries():
                if lib['Id'] == library or lib['Name'].lower() == library.lower():
                    libraryId = lib['Id']
                    libraryName = lib['Name']
                    break
            else:
                await ctx.respond('Music library not found, pick one from the suggestions')
                return
        # Make sure there is something to play before turning radio on.
        sample = await JF_APICLIENT.getRandomItems('Audio' if mode == 'song' else 'MusicAlbum', libraryId)
    except JELLYFIN_ERRORS:
        await reportJellyfinError(ctx)
        return
    # A voice client that is about to go away doesn't count (see stopPlayback).
    await waitUntilLeft(ctx.guild_id)

    source = f'the {libraryName} library' if libraryName else 'all libraries'
    if not sample:
        await ctx.respond(f'No {mode}s found in {source}')
        return
    if not ctx.author.voice and not ctx.voice_client:
        await ctx.respond('You are not in any voice channel')
        return

    state = radioStates.get(ctx.guild_id)
    if state is not None and state['mode'] == mode and state['libraryId'] == libraryId:
        await ctx.respond(f'Radio is already playing random {mode}s from {source}')
    else:
        # A new state drops what is left of the current radio album. The
        # track playing now is left alone: it takes effect after it.
        state = radioStates[ctx.guild_id] = {
            'mode': mode,
            'libraryId': libraryId,
            'libraryName': libraryName,
            'buffer': [],
            'album': None,
            'failures': 0,
            'channel': getattr(ctx, 'channel', None),
        }
        await ctx.respond(f'Radio on: playing random {mode}s from {source} whenever the queue is empty')

    # If the bot is in voice, the session's player picks radio up when the
    # current track ends. Otherwise start a session, like /play does. The
    # reply above took a moment: only if radio is still on as it was set.
    await waitUntilLeft(ctx.guild_id)
    if not ctx.voice_client and radioStates.get(ctx.guild_id) is state:
        try:
            await startPlayer(ctx)
        finally:
            # Not joined (the user left voice meanwhile, or connecting failed):
            # radio must not stay on without a voice session.
            if not ctx.voice_client and radioStates.get(ctx.guild_id) is state:
                radioStates.pop(ctx.guild_id)

'''
Debug Commands
'''
if DEBUG:
    dbgcmd = bot.create_group('jfmbdbg', guild_ids=[DEBUG_SERVER])

    @dbgcmd.command()
    async def playbyid(ctx: discord.ApplicationContext,
                    id: discord.Option(str),
                    when: discord.Option(str, choices=WHEN_CHOICES, required=False) = 'last'):
        if await refuseWhenNowInFairplay(ctx, when):
            return
        await ctx.defer(invisible=True)
        try:
            res = await JF_APICLIENT.getItemsByIds([id])
        except JELLYFIN_ERRORS:
            await reportJellyfinError(ctx)
            return

        if not res:
            await ctx.respond('No items match your query')
        elif not ctx.author.voice:
            await ctx.respond('You are not in any voice channel')
        else:
            await playHelperGeneric(res[0], ctx, when,
                                    announcement=f'Playing {getTrackString(res[0], showType=True)}')
    
    @dbgcmd.command()
    async def printplaylist(ctx: discord.ApplicationContext):
        await ctx.respond(queues.get(ctx.guild_id, 'Playlist is empty'), ephemeral=True)

'''
Entry point
'''
def main():
    """Start the bot and block until it shuts down. main.py calls this;
    importing the module only builds the bot and its commands."""
    setupLogging()
    # End-to-end test hook (inert unless JELLYCHORD_E2E_DRIVER_ID is set)
    if os.getenv('JELLYCHORD_E2E_DRIVER_ID'):
        from jellychord import e2e_control
        e2e_control.install_from_env(bot, cmdgrp)
    bot.run(config['discord-token'])
