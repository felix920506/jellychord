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
# here, the hook's own basicConfig() call is a no-op.
LOG_LEVEL = str(os.getenv('JELLYCHORD_E2E_LOG_LEVEL') or config.get('log-level') or 'INFO').strip().upper()
logging.basicConfig(
    level=logging.getLevelNamesMapping().get(LOG_LEVEL, logging.INFO),
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
log = logging.getLogger('jellychord')
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

def loadAudio(url: str) -> discord.FFmpegOpusAudio:
    """Start ffmpeg and wait for the first packet. Blocking, so run it in a
    thread; it only touches the new audio source, never shared state."""
    audio = discord.FFmpegOpusAudio(
        url,
        codec='copy',
        before_options=STREAM_INPUT_OPTIONS,
    )
    try:
        audio.read()
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
    """Play the next queued track, or leave the voice channel if there is none.
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

        entry = popNextEntry(guild.id)
        if entry is None:
            await stopPlayback(guild, vc)
            return
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
        skipped = skipRequested.is_set()
        if skipped:
            # Skipping a paused track drops its pause, so a later outage
            # recovery doesn't carry it over to the next one.
            entry['paused'] = False

        if loadError is not None:
            if skipped:
                # skipped anyway, so it doesn't count towards giving up
                log.warning('Skipped track %s in guild %s failed to load', entry["Id"], guild.id, exc_info=loadError)
            else:
                failures += 1
                log.error('Failed to start the next track in guild %s (attempt %d/%d)',
                          guild.id, failures, MAX_START_ATTEMPTS, exc_info=loadError)
                # The track never started, so keep it paused (or not) as it is
                # now on the next one. A skip drops it, like skipping a paused track.
                startPaused = entry['paused']
            continue
        if skipped or guild.voice_client is not vc or not vc.is_connected():
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
        if entry['paused']:
            # /pause arrived while the track was loading
            vc.pause()
        return

async def waitForVoiceRecovery(guild: discord.Guild, vc) -> bool:
    """Wait for py-cord to recover vc's dropped connection. Returns True once
    it is connected again or is no longer the guild's voice client (kicked,
    py-cord gave up), so the caller can look again. If it doesn't recover
    within VOICE_OUTAGE_TIMEOUT, or the queue is empty (/stop, /clear, or
    nothing left to play), leaves voice and returns False."""
    log.info('Voice connection in guild %s is down, waiting for it to recover', guild.id)
    deadline = time.monotonic() + VOICE_OUTAGE_TIMEOUT
    while guild.voice_client is vc and not vc.is_connected():
        if not pruneEmptyQueues(guild.id):
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
    """Forget the current track and leave the voice channel. Tracks still
    queued are kept, so /start can pick them up. /play and /start during the
    leave wait for it to finish (waitUntilLeft) and then start a new session."""
    # Before any await, so a command can't see the old voice client without it.
    left = leaving[guild.id] = asyncio.Event()
    playing.pop(guild.id, None)
    currentPlayers.pop(guild.id, None)
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
        await interaction.response.edit_message(content=getQueuePageText(tracks, self.page), view=self)

    async def on_timeout(self):
        self.disable_all_items()
        await self.message.edit(view=self)

'''
Bot Commands

Handlers change queues/playing before awaiting ctx.respond(): a track can end,
and advanceTrack() run, while the reply is being sent to Discord.
'''
if DEBUG:
    cmdgrp = bot.create_group(config['command-group'], guild_ids=[DEBUG_SERVER])
else:
    cmdgrp = bot.create_group(config['command-group'])

@cmdgrp.command()
async def search(ctx: discord.ApplicationContext, 
                 term: discord.Option(str),
                 searchtype: discord.Option(str, name="type", choices=['Soundtrack', 'Album'], required=False),
                 when: discord.Option(str, choices=['now', 'next', 'last'], required=False) = 'last'):
    
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
               when: discord.Option(str, choices=['now', 'next', 'last'], required=False) = 'last'):
    
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
        await ctx.respond(f'Currently Playing: {getTrackString(track)} {formatTimeSecs(td.seconds, length >= 3600)}/{total}')
    else:
        await ctx.respond('Not Currently Playing')

@cmdgrp.command()
async def queue(ctx: discord.ApplicationContext):
    if ctx.guild_id in queues:
        tracks = getFlattenedPlayQueue(ctx.guild_id)
        await ctx.respond(getQueuePageText(tracks, 0), view=listView(ctx.author.id, getPageCount(len(tracks))))
    else:
        await ctx.respond('Empty Queue')

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
                # Another /play or /start may have run while the reply was
                # sent: put the track back in front if it is still waiting,
                # and if that command already started a session, skip
                # whatever it is playing so this track comes next.
                queue = queues.get(ctx.guild_id, [])
                waitingAt = next((i for i, e in enumerate(queue) if e is item), None)
                if waitingAt is not None:
                    queue.insert(0, queue.pop(waitingAt))
                if not ctx.voice_client:
                    await startPlayer(ctx)
                elif waitingAt is not None:
                    skipCurrentTrack(ctx.guild)

@cmdgrp.command()
async def clear(ctx: discord.ApplicationContext):
    global queues
    if not ctx.guild_id in queues:
        await ctx.respond('Playlist is empty')
    else:
        queues.pop(ctx.guild_id)
        await ctx.respond('Playlist cleared')

'''
Debug Commands
'''
if DEBUG:
    dbgcmd = bot.create_group('jfmbdbg', guild_ids=[DEBUG_SERVER])

    @dbgcmd.command()
    async def playbyid(ctx: discord.ApplicationContext,
                    id: discord.Option(str),
                    when: discord.Option(str, choices=['now', 'next', 'last'], required=False) = 'last'):
        
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
End-to-end test hook (inert unless JELLYCHORD_E2E_DRIVER_ID is set)
'''
if os.getenv('JELLYCHORD_E2E_DRIVER_ID'):
    from jellychord import e2e_control
    e2e_control.install_from_env(bot, cmdgrp)

bot.run(config['discord-token'])
