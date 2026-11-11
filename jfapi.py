import aiohttp
import asyncio
import json
import yaml
import urllib.parse

class JFAPI():
    def __init__(self, server: str, apikey: str) -> None:
        self.__apikey = apikey
        self.__server = server.strip('/')
        self._session = None

    def __getEndpointUrl(self, endpoint: str):
        return f'{self.__server}/{endpoint.strip('/')}'

    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
    
    async def __aexit__(self):
        await self._session.close()
    
    def __del__(self):
        try:
            asyncio.run(self._session.delete())
        except:
            pass

    async def checkSession(self):
        if not self._session:
            self._session = aiohttp.ClientSession()

    # https://api.jellyfin.org/#tag/Search/operation/GetSearchHints
    # GET /Search/Hints
    async def search(self, term: str, limit: int = None, types:list[str] = []) -> list:
        await self.checkSession()
        params = {
            'ApiKey': self.__apikey,
            'searchTerm': term,
            'recursive': 'true'
        }
        if limit:
            params['limit'] = limit
        if types:
            params['includeItemTypes'] = ','.join(types)

        async with self._session.get(
            self.__getEndpointUrl('/Items'),
            params=params
        ) as res:
            return (await res.json())["Items"]

    # Gets HLS stream for audio soundtrack with format Opus 16bit 48khz in fMP4 containers
    # Returns URL for use with external player (ffmpeg)
    # GET /Audio/{itemId}/main.m3u8
    def getAudioHls(self, id, bitrate):
        endpoint = self.__getEndpointUrl(f'/Audio/{id}/main.m3u8')
        params = {
            'ApiKey': self.__apikey,
            'segmentContainer': 'mp4',
            'audioCodec': 'opus',
            'allowAudioStreamCopy': True,
            'maxAudioBitDepth': 16,
            'audioSampleRate': 48000,
            'audioChannels': 2,
            'audioBitRate': bitrate
        }
        q = urllib.parse.urlencode(params)
        return endpoint + '?' + q

    # Gets items by IDs
    # GET /Items
    async def getItemsByIds(self, ids: list[str]):
        await self.checkSession()
        endpoint = self.__getEndpointUrl('/Items')
        params = {
            'ApiKey': self.__apikey,
            'ids': ','.join(ids)
        }
        async with self._session.get(endpoint, params=params) as res:
            return (await res.json())['Items']
    
    async def getAlbumTracks(self, albumId: str):
        await self.checkSession()
        endpoint = self.__getEndpointUrl('/Items')
        params = {
            'ApiKey': self.__apikey,
            'parentId': albumId,
            'sortBy': 'ParentIndexNumber,IndexNumber'
        }
        async with self._session.get(endpoint, params=params) as res:
            return (await res.json())['Items']

    # Gets random items of one type, optionally limited to one library
    # GET /Items
    async def getRandomItems(self, itemType: str, libraryId: str = None, limit: int = 1):
        await self.checkSession()
        endpoint = self.__getEndpointUrl('/Items')
        params = {
            'ApiKey': self.__apikey,
            'includeItemTypes': itemType,
            'recursive': 'true',
            'sortBy': 'Random',
            'limit': limit
        }
        if libraryId:
            params['parentId'] = libraryId
        async with self._session.get(endpoint, params=params) as res:
            return (await res.json())['Items']

    # Gets the music libraries on the server
    # GET /Library/MediaFolders
    async def getMusicLibraries(self):
        await self.checkSession()
        endpoint = self.__getEndpointUrl('/Library/MediaFolders')
        params = {
            'ApiKey': self.__apikey
        }
        async with self._session.get(endpoint, params=params) as res:
            items = (await res.json())['Items']
            return [i for i in items if i.get('CollectionType') == 'music']
