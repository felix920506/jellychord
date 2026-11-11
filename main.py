import discord
import discord.ext
import asyncio
import yaml
import random
import datetime
import os
import time
import logging
from itertools import zip_longest

from jfapi import JFAPI

config = {}

'''
Load Settings
'''
if not os.getenv('JELLYCHORD_USE_CONF_FILE') == '0':
    with open('config.yml', 'r', encoding='utf8') as conffile:
        config = yaml.load(conffile, yaml.loader.Loader)

else:
    config = {
        'discord-token': os.getenv('JELLYCHORD_DC_TOKEN'),
        'jf-server': os.getenv('JELLYCHORD_JF_SERVER'),
        'jf-apikey': os.getenv('JELLYCHORD_JF_APIKEY'),
        'command-group': os.getenv('JELLYCHORD_COMMAND_GROUP'),
        'search-limit': os.getenv('JELLYCHORD_SEARCH_LIMIT'),
        'enable-debug': os.getenv('JELLYCHORD_ENABLE_DEBUG') == '1',
        'fairplay': os.getenv('JELLYCHORD_ENABLE_FAIRPLAY') == '1'
    }

    if not config['search-limit']:
        config['search-limit'] = 25
    else:
        config['search-limit'] = int(config['search-limit'])

    if not config['command-group']: 
        config['command-group'] = 'jellychord'

    if config['enable-debug']:
        config['debug-server'] = int(os.getenv('JELLYCHORD_DEBUG_SERVER'))

JF_APICLIENT = JFAPI(config['jf-server'],config['jf-apikey'])
LIMIT = max(1, min(config['search-limit'], 25))
DEBUG = config["enable-debug"]
if DEBUG:
    DEBUG_SERVER = config["debug-server"]
PLAYLIST_PAGESIZE = 20

# radio mode tuning
RADIO_FETCH_TIMEOUT = 30    # seconds to wait for the Jellyfin server when picking a radio item
RADIO_MAX_FAILURES = 5      # consecutive failures before radio turns itself off
RADIO_BACKOFF_BASE = 2      # seconds, doubles after each consecutive failure
RADIO_BACKOFF_MAX = 30      # seconds
LIBRARY_CACHE_SECS = 60

log = logging.getLogger('jellychord')

queues = {}
playing = {}
# radio state per guild, only present while radio is on:
# mode: 'song' or 'album', libraryId: library to pick from (None for all),
# libraryName: display name, buffer: remaining tracks of the current album,
# album: name of the album the buffer came from, failures: consecutive failures,
# channel: text channel to notify if radio has to turn itself off
radioStates = {}
musicLibraryCache = {'time': None, 'items': []}

bot = discord.Bot()

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

def getItemEntry(item: dict) -> dict:
    return {
        "Artists": item["Artists"],
        "Name": item["Name"],
        "Id": item['Id'],
        "Length": item['RunTimeTicks'] // 10000000
    }

async def playHelperGeneric(item: dict, ctx: discord.ApplicationContext, position: str):
    entries = []
    if item["Type"] == "MusicAlbum":
        tracks = await JF_APICLIENT.getAlbumTracks(item['Id'])
        entries = [getItemEntry(i) for i in tracks]
    else:
        entries = [getItemEntry(item)]
    
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

    
    if not ctx.voice_client:
        await startPlayer(ctx)
    elif position == 'now':
        ctx.voice_client.stop()

async def startPlayer(ctx: discord.ApplicationContext):
    vc = ctx.voice_client
    if not vc:
        av = ctx.author.voice
        if av:
            vc = await av.channel.connect()
            await playTrack(ctx.guild)

async def playTrack(guild: discord.Guild):
    vc = guild.voice_client
    if vc.is_paused():
        vc.resume()
    else:
        await asyncio.to_thread(playNextTrack, guild)

def takeQueueTrack(guild):
    # moves the next track of the queue into playing
    fairplay = config.get('fairplay')
    global playing
    global queues
    thisTrackUser = None
    if fairplay:
        thisTrackUserIdx = playing[guild.id].get('nextTrackUserIdx', 0) % len(queues[guild.id]) if guild.id in playing else 0
        thisTrackUser = list(queues[guild.id].keys())[thisTrackUserIdx]
    # grab the next item off the queue to play
    playing[guild.id] = queues[guild.id].pop(0) if not fairplay else queues[guild.id][thisTrackUser].pop(0)

    playing[guild.id]['playtime-offset'] = datetime.timedelta()
    if fairplay:
        # find the next user to play from
        userIds = list(queues[guild.id].keys())
        thisIdx = userIds.index(thisTrackUser) if thisTrackUser in userIds else -1
        nextIdx = (thisIdx + 1) % len(userIds)

        # clean up any empty queues for users
        if not queues[guild.id][thisTrackUser]:
            queues[guild.id].pop(thisTrackUser)
            nextIdx -= 1

        playing[guild.id]['nextTrackUserIdx'] = nextIdx

    if not queues[guild.id]: 
        queues.pop(guild.id)

    if fairplay:
        playing[guild.id]['nextTrackUser'] = None

def runOnLoop(coro, loop):
    # runs a coroutine on the bot event loop from a player thread and waits for its result
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(RADIO_FETCH_TIMEOUT)
    except BaseException:
        future.cancel()
        raise

def fillRadioBuffer(state: dict, loop):
    # picks the next random item(s) for radio mode
    if state['mode'] == 'album':
        albums = runOnLoop(JF_APICLIENT.getRandomItems('MusicAlbum', state['libraryId']), loop)
        if not albums:
            raise RuntimeError('No albums found for radio')
        tracks = runOnLoop(JF_APICLIENT.getAlbumTracks(albums[0]['Id']), loop)
        if not tracks:
            raise RuntimeError(f'Radio album has no tracks: {albums[0]["Name"]}')
        album = albums[0]['Name']
    else:
        tracks = runOnLoop(JF_APICLIENT.getRandomItems('Audio', state['libraryId']), loop)
        if not tracks:
            raise RuntimeError('No songs found for radio')
        album = None

    entries = []
    for track in tracks:
        entry = getItemEntry(track)
        entry['Radio'] = True
        entry['Album'] = album
        entries.append(entry)
    state['album'] = album
    state['buffer'] = entries

def takeRadioTrack(guild, vc, state: dict) -> bool:
    # moves the next radio track into playing
    # returns False if radio changed or something got queued while picking
    global playing
    if not state['buffer']:
        fillRadioBuffer(state, vc.loop)
    if radioStates.get(guild.id) is not state or guild.id in queues:
        return False
    entry = state['buffer'].pop(0)
    entry['playtime-offset'] = datetime.timedelta()
    # radio tracks belong to no user, fairplay rotation starts over after them
    entry['nextTrackUserIdx'] = 0
    playing[guild.id] = entry
    return True

def startPlayback(guild, vc, state: dict = None) -> bool:
    # starts playing the track in playing
    # returns False if the track was dropped because radio changed while it was loading
    br = vc.channel.bitrate
    url = JF_APICLIENT.getAudioHls(playing[guild.id]["Id"],br)
    audio = discord.FFmpegOpusAudio(url, codec='copy')
    try:
        if not audio.read() and state is not None:
            # nothing came out of the stream, without this check
            # radio would spin through tracks while the server is failing
            raise RuntimeError(f'No audio received for track {playing[guild.id]["Id"]}')
        if playing[guild.id].get('Radio') and radioStates.get(guild.id) is not state:
            audio.cleanup()
            return False
        playing[guild.id]['starttime'] = datetime.datetime.now()
        playing[guild.id]['paused'] = False
        vc.play(audio, after=lambda e: playNextTrack(guild, e))
    except Exception:
        audio.cleanup()
        raise
    return True

def handleRadioFailure(guild, vc, state: dict, error: Exception):
    # called when picking or starting a track failed while radio is on
    # waits before the next attempt, turns radio off after too many failures in a row
    state['failures'] += 1
    log.warning('Radio failure %s/%s in guild %s: %r', state['failures'], RADIO_MAX_FAILURES, guild.id, error)
    if state['failures'] >= RADIO_MAX_FAILURES:
        if radioStates.get(guild.id) is state:
            radioStates.pop(guild.id)
            channel = state.get('channel')
            if channel:
                try:
                    asyncio.run_coroutine_threadsafe(channel.send('Radio turned off: too many tracks failed to play in a row'), vc.loop)
                except Exception as e:
                    log.warning('Could not send radio notice: %r', e)
        return

    delay = min(RADIO_BACKOFF_BASE * 2 ** (state['failures'] - 1), RADIO_BACKOFF_MAX)
    hadQueue = guild.id in queues
    deadline = time.monotonic() + delay
    # stop waiting early if radio gets turned off or changed, or if something gets queued
    while time.monotonic() < deadline:
        if radioStates.get(guild.id) is not state or (not hadQueue and guild.id in queues):
            break
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))

def playNextTrack(guild, error=None):
    global playing
    global queues
    while True:
        vc = guild.voice_client
        if not vc:
            # voice connection is gone, nothing left to play to
            playing.pop(guild.id, None)
            radioStates.pop(guild.id, None)
            return

        state = radioStates.get(guild.id)
        try:
            if guild.id in queues:
                # queued tracks always go before radio
                takeQueueTrack(guild)
            elif state is not None:
                if not takeRadioTrack(guild, vc, state):
                    continue
            else:
                playing.pop(guild.id, None)
                asyncio.run_coroutine_threadsafe(vc.disconnect(), vc.loop)
                return

            if not startPlayback(guild, vc, state):
                continue
        except Exception as e:
            if state is None:
                raise
            # nothing is playing while radio waits to try again
            playing.pop(guild.id, None)
            handleRadioFailure(guild, vc, state, e)
            continue

        if state is not None:
            state['failures'] = 0
        return

def getTrackString(item: dict, artistLimit: int = 1, showType: bool = False, showSize: bool = False):

    if not showType:
        res = ''
    elif item["Type"] == "MusicAlbum":
        res = 'Album: '
    else:
        res = 'Track: '

    if len(item["Artists"]) > artistLimit:
        res += 'Various Artists'
    elif item["Artists"]:
        res += ','.join(item["Artists"])
    
    if item["Artists"]:
        res += ' - '
    
    res += item["Name"]

    if showSize and item["Type"] == "Audio":
        secs = item['RunTimeTicks'] // 10000000
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

def getFlattenedPlayQueue(guildId: int) -> list[dict]:
    if not guildId in queues:
        return []
    if not config.get('fairplay'):
        return queues[guildId]
    else:
        nextTrackUserIdx = playing.get(guildId, {}).get('nextTrackUserIdx', 0) % len(queues[guildId])
        userIds = list(queues[guildId].keys())
        orderedUserIds = userIds[nextTrackUserIdx:]+userIds[:nextTrackUserIdx]
        orderedTrackLists = [queues[guildId][uid] for uid in orderedUserIds]

        tracks = []
        for i in zip_longest(*orderedTrackLists, fillvalue=None):
            tracks.extend([j for j in i if j is not None])

        return tracks

def getRadioStatusLine(guildId: int) -> str:
    state = radioStates.get(guildId)
    if not state:
        return ''
    if state['mode'] == 'album':
        if state['buffer']:
            album = state['album'] if len(state['album']) <= 100 else state['album'][:97]+'...'
            return f'Radio is on: continuing album {album} when the queue is empty'
        return 'Radio is on: random albums play when the queue is empty'
    return 'Radio is on: random songs play when the queue is empty'

def getQueueMessage(guildId: int, page: int = 0) -> tuple[str, int]:
    # returns the text for one page of the queue and the total number of pages
    tracks = getFlattenedPlayQueue(guildId)
    pages = len(tracks)//PLAYLIST_PAGESIZE+1
    shown = tracks[page*PLAYLIST_PAGESIZE:page*PLAYLIST_PAGESIZE+PLAYLIST_PAGESIZE]
    strs = [f'{i+page*PLAYLIST_PAGESIZE+1}. {getTrackString(shown[i])}' for i in range(len(shown))]
    content = f'Tracks in playlist:\n{"\n".join(strs)}\nPage: {page+1}/{pages}'
    radioLine = getRadioStatusLine(guildId)
    if radioLine:
        content += '\n' + radioLine
    return content, pages

async def getMusicLibraries() -> list[dict]:
    # list of music libraries on the server, cached for a short while since autocomplete asks often
    now = time.monotonic()
    if musicLibraryCache['time'] is None or now - musicLibraryCache['time'] > LIBRARY_CACHE_SECS:
        musicLibraryCache['items'] = await JF_APICLIENT.getMusicLibraries()
        musicLibraryCache['time'] = now
    return musicLibraryCache['items']

async def libraryAutocomplete(ctx: discord.AutocompleteContext):
    try:
        libraries = await getMusicLibraries()
    except Exception as e:
        log.warning('Could not list music libraries: %r', e)
        return []
    typed = (ctx.value or '').lower()
    return [discord.OptionChoice(name=lib['Name'][:100], value=lib['Id']) for lib in libraries if typed in lib['Name'].lower()][:25]

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
        await interaction.response.edit_message(content=f'Playing {getTrackString(self.items[int(self._selected_values[0])], showType=True)}',view=None)
        await playHelperGeneric(self.items[int(self._selected_values[0])], self.ctx, self.when)

async def onSearchViewTimeout(self: discord.ui.View):
    self.disable_all_items()
    await self.message.edit("Selection timed out.", view=None)

class listDropdown(discord.ui.Select):
    def __init__(self, pages: int):
        super(listDropdown, self).__init__()
        self.max_values = 1
        self.min_values = 1
        self.update_options(pages)
    
    async def callback(self, interaction: discord.Interaction):
        page = int(self._selected_values[0])-1
        content, pages = getQueueMessage(interaction.guild_id, page)
        self.view.page = page
        self.view.updateItems(pages)
        await interaction.response.edit_message(content=content, view=self.view)
    
    def update_options(self, pages):
        self.options = [discord.SelectOption(label=str(i+1)) for i in range(min(pages, 25))]

class listPrevButton(discord.ui.Button):
    def __init__(self):
        super(listPrevButton, self).__init__()
        self.label = '◁'

    async def callback(self, interaction: discord.Interaction):
        self.view.page -= 1
        content, pages = getQueueMessage(interaction.guild_id, self.view.page)
        self.view.updateItems(pages)
        await interaction.response.edit_message(content=content, view=self.view)

class listNextButton(discord.ui.Button):
    def __init__(self):
        super(listNextButton, self).__init__()
        self.label = '▷'

    async def callback(self, interaction: discord.Interaction):
        self.view.page += 1
        content, pages = getQueueMessage(interaction.guild_id, self.view.page)
        self.view.updateItems(pages)
        await interaction.response.edit_message(content=content, view=self.view)


class listRefreshButton(discord.ui.Button):
    def __init__(self):
        super(listRefreshButton, self).__init__()
        self.label = '⟳'
    
    async def callback(self, interaction: discord.Interaction):
        content, pages = getQueueMessage(interaction.guild_id, self.view.page)
        self.view.updateItems(pages)
        await interaction.response.edit_message(content=content, view=self.view)


class listView(discord.ui.View):
    def __init__(self, pages: int):
        super(listView, self).__init__()
        self.page = 0
        self.selection = listDropdown(pages)
        self.prevButton = listPrevButton()
        self.nextButton = listNextButton()
        self.prevButton.disabled = True
        self.nextButton.disabled = self.page == pages - 1
        self.add_item(self.prevButton)
        self.add_item(listRefreshButton())
        self.add_item(self.nextButton)
        self.add_item(self.selection)
    
    def updateItems(self, pages):
        self.prevButton.disabled = self.page == 0
        self.nextButton.disabled = self.page == pages - 1
        self.selection.update_options(pages)
    
    async def on_timeout(self):
        self.disable_all_items()

'''
Bot Commands
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
    res = await searchHelper(term, searchType=searchtype)
    if not res:
        await ctx.respond("No items match your query.")
    elif not ctx.author.voice and not ctx.voice_client:
        await ctx.respond('You are not in any voice channel')
    else:
        view = discord.ui.View()
        view.on_timeout = onSearchViewTimeout
        view.add_item(searchDropdown(res, ctx, when, not searchtype))

        await ctx.respond('Select an item to play:', view=view)

@cmdgrp.command()
async def play(ctx: discord.ApplicationContext,
               term: discord.Option(str),
               searchtype: discord.Option(str, name='type', choices=['Soundtrack', 'Album'], required=False),
               when: discord.Option(str, choices=['now', 'next', 'last'], required=False) = 'last'):
    
    await ctx.defer(invisible=True)
    res = await searchHelper(term, limit=1, searchType=searchtype)

    if not res:
        await ctx.respond('No items match your query')
    elif not ctx.author.voice and not ctx.voice_client:
        await ctx.respond('You are not in any voice channel')
    else:
        await ctx.respond(f'Playing {getTrackString(res[0], showType=True)}')
        await playHelperGeneric(res[0], ctx, when)

@cmdgrp.command()
async def skip(ctx: discord.ApplicationContext):
    if not ctx.voice_client:
        await ctx.respond('Not currently playing')
    else:
        await ctx.respond('Skipping current track')
        ctx.voice_client.stop()

@cmdgrp.command()
async def nowplaying(ctx: discord.ApplicationContext):
    if ctx.guild_id in playing:
        track = playing[ctx.guild_id]
        td = track['playtime-offset']
        if not playing[ctx.guild_id]['paused']:
            td += datetime.datetime.now() - track['starttime']
        length = track["Length"]
        radioTag = ''
        if track.get('Radio'):
            radioTag = f' [Radio: {track["Album"]}]' if track.get('Album') else ' [Radio]'
        await ctx.respond(f'Currently Playing: {getTrackString(track)} {formatTimeSecs(td.seconds, length >= 3600)}/{formatTimeSecs(length)}{radioTag}')
    else:
        await ctx.respond('Not Currently Playing')

@cmdgrp.command()
async def queue(ctx: discord.ApplicationContext):
    if ctx.guild_id in queues:
        content, pages = getQueueMessage(ctx.guild_id)
        await ctx.respond(content, view=listView(pages))
    elif ctx.guild_id in radioStates:
        await ctx.respond(f'Empty Queue\n{getRadioStatusLine(ctx.guild_id)}')
    else:
        await ctx.respond('Empty Queue')

@cmdgrp.command()
async def start(ctx: discord.ApplicationContext):
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
    global playing
    if ctx.voice_client and ctx.guild_id not in playing:
        # radio is between tracks, waiting to retry after a failure
        await ctx.respond('Not currently playing')
    elif ctx.voice_client:
        if playing[ctx.guild_id]['paused']:
            await ctx.respond('Already paused')
        else:
            await ctx.respond('Pausing playback')
            td = datetime.datetime.now() - playing[ctx.guild_id]['starttime']
            playing[ctx.guild_id]['paused'] = True
            playing[ctx.guild_id]['playtime-offset'] += td
            ctx.voice_client.pause()
    else:
        await ctx.respond('Not connect to any voice channel')

@cmdgrp.command()
async def resume(ctx: discord.ApplicationContext):
    global playing
    if ctx.voice_client and ctx.guild_id not in playing:
        # radio is between tracks, waiting to retry after a failure
        await ctx.respond('Not currently playing')
    elif ctx.voice_client:
        if playing[ctx.guild_id]['paused']:
            await ctx.respond('Resuming playback')
            playing[ctx.guild_id]['starttime'] = datetime.datetime.now()
            playing[ctx.guild_id]['paused'] = False
            ctx.voice_client.resume()
        else:
            await ctx.respond('Already playing')
    else:
        await ctx.respond('Not connect to any voice channel')

@cmdgrp.command()
async def stop(ctx: discord.ApplicationContext):
    if ctx.voice_client:
        await ctx.respond('Stopping playback')
        global queues
        if ctx.guild_id in queues:
            queues.pop(ctx.guild_id)
        # radio has to go too, otherwise it would just pick another track
        radioStates.pop(ctx.guild_id, None)
        ctx.voice_client.stop()
    else:
        await ctx.respond('Not connected to any voice channel')

@cmdgrp.command()
async def shuffle(ctx: discord.ApplicationContext):
    global queues
    if not ctx.guild_id in queues:
        await ctx.respond('Playlist is empty')
    else:
        await ctx.respond('Shuffling playlist')
        if config.get('fairplay'):
            for userId in queues[ctx.guild_id]:
                random.shuffle(queues[ctx.guild_id][userId])

            userIds = list(queues[ctx.guild_id].keys())
            random.shuffle(userIds)
            shuffledQueues = {uid: queues[ctx.guild_id][uid] for uid in userIds}
            queues[ctx.guild_id] = shuffledQueues
        else:
            random.shuffle(queues[ctx.guild_id])

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
            target = guildqueue[index-1]
            for userId in queues[ctx.guild_id]:
                for item in queues[ctx.guild_id][userId]:
                    if item['Id'] == target['Id']:
                        queues[ctx.guild_id][userId].remove(item)
                        if not queues[ctx.guild_id][userId]:
                            queues[ctx.guild_id].pop(userId)
                        if not queues[ctx.guild_id]:
                            queues.pop(ctx.guild_id)
                        await ctx.respond(f'Deleted track: {getTrackString(item)}')
                        return

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
            target = guildqueue[index-1]
            for item in queues[ctx.guild_id][ctx.author.id]:
                if item['Id'] == target['Id']:
                    queues[ctx.guild_id][ctx.author.id].remove(item)
                    queues[ctx.guild_id][ctx.author.id].insert(0, item)
                    await ctx.respond(f'Promoted track to the front of your tracks: {getTrackString(item)}')
                    return
            else:
                await ctx.respond('You can only promote your own tracks in fairplay mode', ephemeral=True)

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
            target = guildqueue[index-1]
            for item in queues[ctx.guild_id][ctx.author.id]:
                if item['Id'] == target['Id']:
                    queues[ctx.guild_id][ctx.author.id].remove(item)
                    queues[ctx.guild_id][ctx.author.id].insert(-1, item)
                    await ctx.respond(f'Demoted track to the back of your tracks: {getTrackString(item)}')
                    return
            else:
                await ctx.respond('You can only demote your own tracks in fairplay mode', ephemeral=True)

if not config.get('fairplay'):
    # playnow command is disabled in fairplay mode since it would defeat the whole point
    @cmdgrp.command()
    async def playnow(ctx: discord.ApplicationContext,
                    index: discord.Option(int, min_value=1)):
        global queues
        if ctx.guild_id not in queues:
            await ctx.respond('Playlist is empty')
        elif len(queues[ctx.guild_id]) < index:
            await ctx.respond('Specified index does not exist')
        else:
            item = queues[ctx.guild_id].pop(index-1)
            queues[ctx.guild_id].insert(0, item)
            await ctx.respond(f'Now playing track: {getTrackString(item)}')
            ctx.voice_client.stop()

@cmdgrp.command()
async def clear(ctx: discord.ApplicationContext):
    global queues
    if not ctx.guild_id in queues:
        await ctx.respond('Playlist is empty')
    else:
        await ctx.respond('Playlist cleared')
        queues.pop(ctx.guild_id)

@cmdgrp.command()
async def radio(ctx: discord.ApplicationContext,
                mode: discord.Option(str, choices=['song', 'album', 'off']),
                library: discord.Option(str, autocomplete=libraryAutocomplete, required=False) = None):
    if mode == 'off':
        if radioStates.pop(ctx.guild_id, None) is None:
            await ctx.respond('Radio is not on')
        else:
            await ctx.respond('Radio turned off, playback ends after the current track and the queue')
        return

    if not ctx.author.voice and not ctx.voice_client:
        await ctx.respond('You are not in any voice channel')
        return

    await ctx.defer()
    itemType = 'Audio' if mode == 'song' else 'MusicAlbum'
    libraryId = None
    libraryName = None
    try:
        if library:
            libraries = await getMusicLibraries()
            for lib in libraries:
                if lib['Id'] == library or lib['Name'].lower() == library.lower():
                    libraryId = lib['Id']
                    libraryName = lib['Name']
                    break
            else:
                await ctx.respond('Music library not found, pick one from the suggestions')
                return
        # make sure there is something to play before turning radio on
        sample = await JF_APICLIENT.getRandomItems(itemType, libraryId)
    except Exception as e:
        log.warning('Could not start radio: %r', e)
        await ctx.respond('Could not get items from the Jellyfin server')
        return

    source = f'the {libraryName} library' if libraryName else 'all libraries'
    if not sample:
        await ctx.respond(f'No {mode}s found in {source}')
        return

    current = radioStates.get(ctx.guild_id)
    if current and current['mode'] == mode and current['libraryId'] == libraryId:
        await ctx.respond(f'Radio is already playing random {mode}s from {source}')
        return

    # replacing the state drops what is left of the current radio album
    # the track that is playing right now is left alone
    radioStates[ctx.guild_id] = {
        'mode': mode,
        'libraryId': libraryId,
        'libraryName': libraryName,
        'buffer': [],
        'album': None,
        'failures': 0,
        'channel': ctx.channel
    }
    await ctx.respond(f'Radio on: playing random {mode}s from {source} whenever the queue is empty')

    if not ctx.voice_client:
        try:
            await startPlayer(ctx)
        except Exception:
            radioStates.pop(ctx.guild_id, None)
            raise

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
        res = await JF_APICLIENT.getItemsByIds([id])

        if not res:
            await ctx.respond('No items match your query')
        elif not ctx.author.voice:
            await ctx.respond('You are not in any voice channel')
        else:
            await ctx.respond(f'Playing {getTrackString(res[0], showType=True)}')
            await playHelperGeneric(res[0], ctx, when)
    
    @dbgcmd.command()
    async def printplaylist(ctx: discord.ApplicationContext):
        await ctx.respond(queues.get(ctx.guild_id, 'Playlist is empty'), ephemeral=True)

bot.run(config['discord-token'])
