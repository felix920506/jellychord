import aiohttp
import urllib.parse

class JFAPI():
    def __init__(self, server: str, apikey: str) -> None:
        self.__apikey = apikey
        self.__server = server.strip('/')
        self._session = None

    def __getEndpointUrl(self, endpoint: str):
        return f'{self.__server}/{endpoint.strip('/')}'

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    # The session is created lazily on the event loop, so it has to be closed
    # there too: call close() (or use `async with`) before the loop ends.
    async def close(self):
        session, self._session = self._session, None
        if session is not None:
            await session.close()

    async def checkSession(self):
        if not self._session:
            self._session = aiohttp.ClientSession()

    # Raises aiohttp.ClientResponseError if Jellyfin answers with an HTTP error.
    async def __getItems(self, params: dict) -> list:
        await self.checkSession()
        async with self._session.get(self.__getEndpointUrl('/Items'), params=params) as res:
            res.raise_for_status()
            return (await res.json())['Items']

    # https://api.jellyfin.org/#tag/Items/operation/GetItems
    # GET /Items
    async def search(self, term: str, limit: int = None, types: list[str] | None = None) -> list:
        params = {
            'ApiKey': self.__apikey,
            'searchTerm': term,
            'recursive': 'true'
        }
        if limit:
            params['limit'] = limit
        if types:
            params['includeItemTypes'] = ','.join(types)
        return await self.__getItems(params)

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
        params = {
            'ApiKey': self.__apikey,
            'ids': ','.join(ids)
        }
        return await self.__getItems(params)

    # Gets an album's tracks in disc and track order
    # GET /Items
    async def getAlbumTracks(self, albumId: str):
        params = {
            'ApiKey': self.__apikey,
            'parentId': albumId,
            'sortBy': 'ParentIndexNumber,IndexNumber'
        }
        return await self.__getItems(params)

