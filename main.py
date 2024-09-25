import discord
import discord.ext
import asyncio
import yaml
import random
import datetime

from jfapi import JFAPI

with open('config.yml', 'r', encoding='utf8') as conffile:
    config = yaml.load(conffile, yaml.loader.Loader)

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
async def searchHelper(term: str, limit: int = LIMIT, type:str = None):
    if type == 'Soundtrack':
        type = ['Audio']
    elif type == 'Album':
        type = ['MusicAlbum']
    else:
        type = ['Audio', 'MusicAlbum']
    
    res = await JF_APICLIENT.search(term, limit, type)
    return res

async def playHelperTrack(item: dict, ctx: discord.ApplicationContext, position: str):
    entry = {
        "Artists": item["Artists"],
        "Name": item["Name"],
        "Id": item['Id'],
        "Length": item['RunTimeTicks'] // 10000000
    }
    global queues
    if not ctx.guild_id in queues:
        queues[ctx.guild_id] = []
    if position == 'last':
        queues[ctx.guild_id].append(entry)
    else:
        queues[ctx.guild_id].insert(0, entry)

    if not ctx.voice_client:
        await startPlayer(ctx)
    elif position == 'now':
        ctx.voice_client.stop()

async def playHelperAlbum(item: dict, ctx: discord.ApplicationContext, position: str):
    tracks = await JF_APICLIENT.getAlbumTracks(item['Id'])
    entries = [{
        "Artists": item["Artists"],
        "Name": item["Name"],
        "Id": item['Id'],
        "Length": item['RunTimeTicks'] // 10000000
    } for item in tracks]

    global queues
    if not ctx.guild_id in queues:
        queues[ctx.guild_id] = []
    if position == 'last':
        queues[ctx.guild_id].extend(entries)
    else:
        queues[ctx.guild_id][0:0] = entries
    
    if not ctx.voice_client:
        await startPlayer(ctx)
    elif position == 'now':
        ctx.voice_client.stop()

async def playHelperGeneric(item: dict, ctx: discord.ApplicationContext, position: str):
    if item["Type"] == "MusicAlbum":
        await playHelperAlbum(item, ctx, position)
    else:
        await playHelperTrack(item, ctx, position)

async def startPlayer(ctx: discord.ApplicationContext):
    vc = ctx.voice_client
    if not vc:
        av = ctx.author.voice
        if av:
            vc = await av.channel.connect()
            await playTrack(ctx.guild)

async def playTrack(guild: discord.Guild):
    vc = guild.voice_client
    if vc.paused:
        vc.resume()
    else:
        await asyncio.to_thread(playNextTrack, guild)

def playNextTrack(guild, error=None):
    vc = guild.voice_client
    br = vc.channel.bitrate
    global playing
    global queues
    if guild.id in queues:
        playing[guild.id] = queues[guild.id].pop(0)
        playing[guild.id]['playtime-offset'] = datetime.timedelta()
        if not queues[guild.id]: 
            queues.pop(guild.id)
        url = JF_APICLIENT.getAudioHls(playing[guild.id]["Id"],br)
        # use libopus until py-cord 2.7
        # change to 'copy' after py-cord 2.7 is out
        audio = discord.FFmpegOpusAudio(url, codec='libopus')
        audio.read() # remove this line when py-cord 2.7 is out
        playing[guild.id]['starttime'] = datetime.datetime.now()
        playing[guild.id]['paused'] = False
        vc.play(audio, after=lambda e: playNextTrack(guild, e))
    else:
        playing.pop(guild.id)
        asyncio.run_coroutine_threadsafe(vc.disconnect(), vc.loop)

def getTrackString(item: dict, artistLimit: int = 1, type: bool = False):

    if not type:
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
    return res

def formatTimeSecs(secs: int, force_hrs: bool = False) -> str:
    s = secs % 60
    m = secs // 60 % 60
    h = secs // 3600
    
    if h or force_hrs:
        return f'{h}:{m:02d}:{s:02d}'
    else:
        return f'{m:02d}:{s:02d}'

'''
Discord View Related
'''
class searchDropdown(discord.ui.Select):
    def __init__(self, items: list[dict], ctx: discord.ApplicationContext, when: str):
        super(searchDropdown, self).__init__()
        self.ctx = ctx
        self.when = when
        self.items = items
        self.max_values = 1
        self.min_values = 1
        for i in range(min(len(items), 25)):
            label = getTrackString(items[i], type=not bool(type))
            label = label[:97]+'...' if len(label) > 100 else label
            self.add_option(label = label, value = str(i))
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content=f'Playing {getTrackString(self.items[int(self._selected_values[0])], type=True)}',view=None)
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
        pages = len(queues[interaction.guild_id])//PLAYLIST_PAGESIZE+1
        page = int(self._selected_values[0])-1
        tracks = queues[interaction.guild_id][page*PLAYLIST_PAGESIZE:page*PLAYLIST_PAGESIZE+PLAYLIST_PAGESIZE]
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
        pages = len(queues[interaction.guild_id])//PLAYLIST_PAGESIZE+1
        self.view.page -= 1
        page = self.view.page
        tracks = queues[interaction.guild_id][page*PLAYLIST_PAGESIZE:page*PLAYLIST_PAGESIZE+PLAYLIST_PAGESIZE]
        strs = [f'{i+page*PLAYLIST_PAGESIZE+1}. {getTrackString(tracks[i])}' for i in range(len(tracks))]
        self.view.updateItems(pages)
        await interaction.response.edit_message(content=f'Tracks in playlist:\n{"\n".join(strs)}\nPage: {page+1}/{pages}', view=self.view)

class listNextButton(discord.ui.Button):
    def __init__(self):
        super(listNextButton, self).__init__()
        self.label = '▷'

    async def callback(self, interaction: discord.Interaction):
        pages = len(queues[interaction.guild_id])//PLAYLIST_PAGESIZE+1
        self.view.page += 1
        page = self.view.page
        tracks = queues[interaction.guild_id][page*PLAYLIST_PAGESIZE:page*PLAYLIST_PAGESIZE+PLAYLIST_PAGESIZE]
        strs = [f'{i+page*PLAYLIST_PAGESIZE+1}. {getTrackString(tracks[i])}' for i in range(len(tracks))]
        self.view.updateItems(pages)
        await interaction.response.edit_message(content=f'Tracks in playlist:\n{"\n".join(strs)}\nPage: {page+1}/{pages}', view=self.view)


class listRefreshButton(discord.ui.Button):
    def __init__(self):
        super(listRefreshButton, self).__init__()
        self.label = '⟳'
    
    async def callback(self, interaction: discord.Interaction):
        pages = len(queues[interaction.guild_id])//PLAYLIST_PAGESIZE+1
        page = self.view.page
        tracks = queues[interaction.guild_id][page*PLAYLIST_PAGESIZE:page*PLAYLIST_PAGESIZE+PLAYLIST_PAGESIZE]
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
                 type: discord.Option(str, choices=['Soundtrack', 'Album'], required=False),
                 when: discord.Option(str, choices=['now', 'next', 'last'], required=False) = 'last'):
    
    await ctx.defer(invisible=True)
    res = await searchHelper(term, type=type)
    if not res:
        await ctx.respond("No items match your query.")
    elif not ctx.author.voice and not ctx.voice_client:
        await ctx.respond('You are not in any voice channel')
    else:
        view = discord.ui.View()
        view.on_timeout = onSearchViewTimeout
        view.add_item(searchDropdown(res, ctx, when))

        await ctx.respond('Select an item to play:', view=view)

@cmdgrp.command()
async def play(ctx: discord.ApplicationContext,
               term: discord.Option(str),
               type: discord.Option(str, choices=['Soundtrack', 'Album'], required=False),
               when: discord.Option(str, choices=['now', 'next', 'last'], required=False) = 'last'):
    
    await ctx.defer(invisible=True)
    res = await searchHelper(term, limit=1, type=type)

    if not res:
        await ctx.respond('No items match your query')
    elif not ctx.author.voice and not ctx.voice_client:
        await ctx.respond('You are not in any voice channel')
    else:
        await ctx.respond(f'Playing {getTrackString(res[0], type=True)}')
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
        tracks = queues[ctx.guild_id]
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
        random.shuffle(queues[ctx.guild_id])

@cmdgrp.command()
async def remove(ctx: discord.ApplicationContext,
                 index: discord.Option(int, min_value = 1)):
    global queues
    if not ctx.guild_id in queues:
        await ctx.respond('Queue is empty')
    elif len(queues[ctx.guild_id]) < index:
        await ctx.respond('Specified index does not exist')
    else:
        item = queues[ctx.guild_id].pop(index-1)
        if not queues[ctx.guild_id]:
            queues.pop(ctx.guild_id)
        await ctx.respond(f'Deleted track: {getTrackString(item)}')

@cmdgrp.command()
async def promote(ctx: discord.ApplicationContext,
                  index: discord.Option(int, min_value = 1)):
    global queues
    if not ctx.guild_id in queues:
        await ctx.respond('Playlist is empty')
    elif len(queues[ctx.guild_id]) < index:
        await ctx.respond('Specified index does not exist')
    else:
        item = queues[ctx.guild_id].pop(index-1)
        queues[ctx.guild_id].insert(0, item)
        await ctx.respond(f'Promoted track to the front: {getTrackString(item)}')

@cmdgrp.command()
async def demote(ctx: discord.ApplicationContext,
                 index: discord.Option(int, min_value = 1)):
    global queues
    if ctx.guild_id not in queues:
        await ctx.respond('Playlist is empty')
    elif len(queues[ctx.guild_id]) < index:
        await ctx.respond('Specified index does not exist')
    else:
        item = queues[ctx.guild_id].pop(index-1)
        queues[ctx.guild_id].append(item)
        await ctx.respond(f'Promoted track to the front: {getTrackString(item)}')

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
            await ctx.respond(f'Playing {getTrackString(res[0], type=True)}')
            await playHelperGeneric(res[0], ctx, when)


bot.run(config['discord-token'])