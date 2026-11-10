import discord
import discord.ext
import asyncio
import yaml
import random
import datetime
import os
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
        'fairplay': os.getenv('JELLYCHORD_ENABLE_FAIRPLAY') == '1',
        'log-level': os.getenv('JELLYCHORD_LOG_LEVEL'),
    }

    if not config['search-limit']:
        config['search-limit'] = 25
    else:
        config['search-limit'] = int(config['search-limit'])

    if not config['command-group']: 
        config['command-group'] = 'jellychord'

    if config['enable-debug']:
        config['debug-server'] = int(os.getenv('JELLYCHORD_DEBUG_SERVER'))

'''
Logging
'''
# The E2E hook's log level takes precedence: once logging is configured
# here, the hook's own basicConfig() call is a no-op.
LOG_LEVEL = str(os.getenv('JELLYCHORD_E2E_LOG_LEVEL') or config.get('log-level') or 'INFO').upper()
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

queues = {}
playing = {}

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

    if not entries:
        # an empty queue would make the player pop from an empty list
        log.warning('Album %s has no tracks, nothing queued', item['Id'])
        await ctx.respond('That album has no tracks to play')
        return

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

# How many tracks playNextTrack will try to start, one after another, before
# giving up and leaving the voice channel.
MAX_START_ATTEMPTS = 3

def playNextTrack(guild, error=None):
    """Start the next queued track, or leave the voice channel if there is none.

    This runs as the voice player's ``after=`` callback on py-cord's player
    thread, so it must not raise: nothing would schedule another track and the
    bot would sit idle in the channel with no way to resume playback.
    """
    if error is not None:
        log.error('Playback error in guild %s', guild.id, exc_info=error)

    vc = guild.voice_client
    for attempt in range(1, MAX_START_ATTEMPTS + 1):
        if vc is None:
            # Disconnected (kicked, network drop...) before the callback ran.
            log.info('No voice connection in guild %s, stopping playback', guild.id)
            playing.pop(guild.id, None)
            return
        try:
            if not pruneEmptyQueues(guild.id):
                break
            startNextTrack(guild, vc)
            return
        except Exception:
            log.exception('Failed to start the next track in guild %s (attempt %d/%d)',
                          guild.id, attempt, MAX_START_ATTEMPTS)
            if vc.is_playing() or vc.is_paused():
                # Another start won a race (vc.play raised "Already playing
                # audio"). That player's after= callback continues the chain;
                # retrying would only drop more tracks.
                return
        vc = guild.voice_client
    else:
        log.error('Giving up on playback in guild %s after %d failed attempts',
                  guild.id, MAX_START_ATTEMPTS)
    stopPlayback(guild, vc)

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

def startNextTrack(guild, vc):
    """Pop the next track off the guild's (non-empty) queue and start playing it."""
    br = vc.channel.bitrate
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
        # clean up any empty queues for users
        if not queues[guild.id][thisTrackUser]:
            queues[guild.id].pop(thisTrackUser)
            # the following user has shifted into thisIdx; wrap if this user was last
            remaining = len(queues[guild.id])
            nextIdx = thisIdx % remaining if remaining else 0
        else:
            nextIdx = (thisIdx + 1) % len(userIds)

        playing[guild.id]['nextTrackUserIdx'] = nextIdx

    if not queues[guild.id]:
        queues.pop(guild.id)

    if fairplay:
        playing[guild.id]['nextTrackUser'] = None

    log.info('Playing %s (%s) in guild %s', playing[guild.id]['Name'], playing[guild.id]['Id'], guild.id)
    url = JF_APICLIENT.getAudioHls(playing[guild.id]["Id"],br)
    audio = discord.FFmpegOpusAudio(url, codec='copy')
    try:
        audio.read()
        playing[guild.id]['starttime'] = datetime.datetime.now()
        playing[guild.id]['paused'] = False
        vc.play(audio, after=lambda e: playNextTrack(guild, e))
    except Exception:
        # the player never took ownership of the source, so stop ffmpeg here
        audio.cleanup()
        raise

def stopPlayback(guild, vc):
    """Forget the current track and leave the voice channel."""
    playing.pop(guild.id, None)
    if vc is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(vc.disconnect(), vc.loop)
    except Exception:
        log.exception('Failed to disconnect from voice in guild %s', guild.id)

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
        # nothing may be playing, e.g. after playback gave up with tracks still queued
        nextTrackUserIdx = playing.get(guildId, {}).get('nextTrackUserIdx', 0) % len(queues[guildId])
        userIds = list(queues[guildId].keys())
        orderedUserIds = userIds[nextTrackUserIdx:]+userIds[:nextTrackUserIdx]
        orderedTrackLists = [queues[guildId][uid] for uid in orderedUserIds]

        tracks = []
        for i in zip_longest(*orderedTrackLists, fillvalue=None):
            tracks.extend([j for j in i if j is not None])

        return tracks

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
        pages = len(getFlattenedPlayQueue(interaction.guild_id))//PLAYLIST_PAGESIZE+1
        page = int(self._selected_values[0])-1
        tracks = getFlattenedPlayQueue(interaction.guild_id)[page*PLAYLIST_PAGESIZE:page*PLAYLIST_PAGESIZE+PLAYLIST_PAGESIZE]
        strs = [f'{i+page*PLAYLIST_PAGESIZE+1}. {getTrackString(tracks[i])}' for i in range(len(tracks))]
        self.view.page = page
        self.view.updateItems(pages)
        await interaction.response.edit_message(content=f'Tracks in playlist:\n{"\n".join(strs)}\nPage: {page+1}/{pages}', view=self.view)
    
    def update_options(self, pages):
        self.options = [discord.SelectOption(label=str(i+1)) for i in range(min(pages, 25))]

class listPrevButton(discord.ui.Button):
    def __init__(self):
        super(listPrevButton, self).__init__()
        self.label = '◁'

    async def callback(self, interaction: discord.Interaction):
        pages = len(getFlattenedPlayQueue(interaction.guild_id))//PLAYLIST_PAGESIZE+1
        self.view.page -= 1
        page = self.view.page
        tracks = getFlattenedPlayQueue(interaction.guild_id)[page*PLAYLIST_PAGESIZE:page*PLAYLIST_PAGESIZE+PLAYLIST_PAGESIZE]
        strs = [f'{i+page*PLAYLIST_PAGESIZE+1}. {getTrackString(tracks[i])}' for i in range(len(tracks))]
        self.view.updateItems(pages)
        await interaction.response.edit_message(content=f'Tracks in playlist:\n{"\n".join(strs)}\nPage: {page+1}/{pages}', view=self.view)

class listNextButton(discord.ui.Button):
    def __init__(self):
        super(listNextButton, self).__init__()
        self.label = '▷'

    async def callback(self, interaction: discord.Interaction):
        pages = len(getFlattenedPlayQueue(interaction.guild_id))//PLAYLIST_PAGESIZE+1
        self.view.page += 1
        page = self.view.page
        tracks = getFlattenedPlayQueue(interaction.guild_id)[page*PLAYLIST_PAGESIZE:page*PLAYLIST_PAGESIZE+PLAYLIST_PAGESIZE]
        strs = [f'{i+page*PLAYLIST_PAGESIZE+1}. {getTrackString(tracks[i])}' for i in range(len(tracks))]
        self.view.updateItems(pages)
        await interaction.response.edit_message(content=f'Tracks in playlist:\n{"\n".join(strs)}\nPage: {page+1}/{pages}', view=self.view)


class listRefreshButton(discord.ui.Button):
    def __init__(self):
        super(listRefreshButton, self).__init__()
        self.label = '⟳'
    
    async def callback(self, interaction: discord.Interaction):
        pages = len(getFlattenedPlayQueue(interaction.guild_id))//PLAYLIST_PAGESIZE+1
        page = self.view.page
        tracks = getFlattenedPlayQueue(interaction.guild_id)[page*PLAYLIST_PAGESIZE:page*PLAYLIST_PAGESIZE+PLAYLIST_PAGESIZE]
        strs = [f'{i+page*PLAYLIST_PAGESIZE+1}. {getTrackString(tracks[i])}' for i in range(len(tracks))]
        self.view.updateItems(pages)
        await interaction.response.edit_message(content=f'Tracks in playlist:\n{"\n".join(strs)}\nPage: {page+1}/{pages}', view=self.view)


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
        await ctx.respond(f'Currently Playing: {getTrackString(track)} {formatTimeSecs(td.seconds, length >= 3600)}/{formatTimeSecs(length)}')
    else:
        await ctx.respond('Not Currently Playing')

@cmdgrp.command()
async def queue(ctx: discord.ApplicationContext):
    if ctx.guild_id in queues:
        tracks = getFlattenedPlayQueue(ctx.guild_id)
        strs = [f'{i}. {getTrackString(tracks[i])}' for i in range(min(len(tracks), PLAYLIST_PAGESIZE))]
        await ctx.respond(f'Tracks in playlist:\n{"\n".join(strs)}\nPage: 1/{len(tracks)//PLAYLIST_PAGESIZE+1}', view=listView(len(tracks)//PLAYLIST_PAGESIZE+1))
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
    if ctx.voice_client:
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
    if ctx.voice_client:
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

'''
End-to-end test hook (inert unless JELLYCHORD_E2E_DRIVER_ID is set)
'''
if os.getenv('JELLYCHORD_E2E_DRIVER_ID'):
    from jellychord import e2e_control
    e2e_control.install_from_env(bot, cmdgrp)

bot.run(config['discord-token'])
