import aiohttp
import urllib.parse

class JellyfinResponseError(Exception):
    """Jellyfin answered with a success status but the body isn't the
    expected JSON (invalid JSON, or no list under 'Items')."""

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

    # Raises aiohttp.ClientResponseError if Jellyfin answers with an HTTP error
    # (or a non-JSON content type), JellyfinResponseError if a success body
    # isn't JSON with an 'Items' list.
    async def __getItems(self, params: dict, endpoint: str = '/Items') -> list:
        await self.checkSession()
        async with self._session.get(self.__getEndpointUrl(endpoint), params=params) as res:
            res.raise_for_status()
            try:
                body = await res.json()
            except ValueError as e:  # json.JSONDecodeError
                raise JellyfinResponseError(f'GET {endpoint} returned invalid JSON: {e}') from e
        items = body.get('Items') if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise JellyfinResponseError(f"GET {endpoint} response has no 'Items' list")
        return items

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

    # Gets random items of one type, optionally from one library only
    # GET /Items
    async def getRandomItems(self, itemType: str, libraryId: str | None = None, limit: int = 1):
        params = {
            'ApiKey': self.__apikey,
            'includeItemTypes': itemType,
            'recursive': 'true',
            'sortBy': 'Random',
            'limit': limit
        }
        if libraryId:
            params['parentId'] = libraryId
        return await self.__getItems(params)

    # Gets the server's music libraries
    # GET /Library/MediaFolders
    async def getMusicLibraries(self):
        params = {
            'ApiKey': self.__apikey
        }
        folders = await self.__getItems(params, '/Library/MediaFolders')
        return [f for f in folders if isinstance(f, dict) and f.get('CollectionType') == 'music']
