import unittest
import urllib.parse

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from jfapi import JFAPI


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def raise_for_status(self):
        return None

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def get(self, endpoint, params):
        self.requests.append((endpoint, params))
        return FakeResponse(self.payload)


class JFAPITests(unittest.IsolatedAsyncioTestCase):
    def test_get_audio_hls_builds_expected_stream_url(self):
        api = JFAPI("https://jellyfin.example.com/base/", "secret-key")

        url = api.getAudioHls("item-1", 128000)
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)

        self.assertEqual(
            parsed.scheme + "://" + parsed.netloc + parsed.path,
            "https://jellyfin.example.com/base/Audio/item-1/main.m3u8",
        )
        self.assertEqual(query["ApiKey"], ["secret-key"])
        self.assertEqual(query["segmentContainer"], ["mp4"])
        self.assertEqual(query["audioCodec"], ["opus"])
        self.assertEqual(query["allowAudioStreamCopy"], ["True"])
        self.assertEqual(query["maxAudioBitDepth"], ["16"])
        self.assertEqual(query["audioSampleRate"], ["48000"])
        self.assertEqual(query["audioChannels"], ["2"])
        self.assertEqual(query["audioBitRate"], ["128000"])

    async def test_search_returns_items_and_sends_filters(self):
        api = JFAPI("https://jellyfin.example.com/", "secret-key")
        session = FakeSession({"Items": [{"Id": "track-1"}]})
        api._session = session

        result = await api.search("mint", limit=5, types=["Audio", "MusicAlbum"])

        self.assertEqual(result, [{"Id": "track-1"}])
        self.assertEqual(len(session.requests), 1)
        endpoint, params = session.requests[0]
        self.assertEqual(endpoint, "https://jellyfin.example.com/Items")
        self.assertEqual(
            params,
            {
                "ApiKey": "secret-key",
                "searchTerm": "mint",
                "recursive": "true",
                "limit": 5,
                "includeItemTypes": "Audio,MusicAlbum",
            },
        )

    async def test_get_items_by_ids_joins_ids_for_query(self):
        api = JFAPI("https://jellyfin.example.com/", "secret-key")
        session = FakeSession({"Items": [{"Id": "a"}, {"Id": "b"}]})
        api._session = session

        result = await api.getItemsByIds(["a", "b"])

        self.assertEqual(result, [{"Id": "a"}, {"Id": "b"}])
        endpoint, params = session.requests[0]
        self.assertEqual(endpoint, "https://jellyfin.example.com/Items")
        self.assertEqual(params, {"ApiKey": "secret-key", "ids": "a,b"})

    async def test_get_album_tracks_requests_parent_sorted_by_disc_and_index(self):
        api = JFAPI("https://jellyfin.example.com/", "secret-key")
        session = FakeSession({"Items": [{"Id": "track-1"}]})
        api._session = session

        result = await api.getAlbumTracks("album-1")

        self.assertEqual(result, [{"Id": "track-1"}])
        endpoint, params = session.requests[0]
        self.assertEqual(endpoint, "https://jellyfin.example.com/Items")
        self.assertEqual(
            params,
            {
                "ApiKey": "secret-key",
                "parentId": "album-1",
                "sortBy": "ParentIndexNumber,IndexNumber",
            },
        )


class JFAPIServerTests(unittest.IsolatedAsyncioTestCase):
    """Talks to a local HTTP server that answers GET /Items like Jellyfin."""

    async def asyncSetUp(self):
        self.requests = []
        # (status, body, content type) for every GET /Items
        self.reply = (200, '{"Items": [{"Id": "track-1"}]}', "application/json")

        async def handler(request):
            self.requests.append((request.method, request.path))
            status, body, contentType = self.reply
            return web.Response(status=status, text=body, content_type=contentType)

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        self.server = TestServer(app)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.serverUrl = str(self.server.make_url("/"))

    def calls(self, api):
        return {
            "search": lambda: api.search("mint"),
            "getItemsByIds": lambda: api.getItemsByIds(["a"]),
            "getAlbumTracks": lambda: api.getAlbumTracks("album-1"),
        }

    async def test_http_errors_raise_client_response_error(self):
        replies = [
            (401, "Unauthorized", "text/plain"),  # not JSON
            (500, '{"error": "boom"}', "application/json"),  # JSON without Items
        ]
        for reply in replies:
            async with JFAPI(self.serverUrl, "secret-key") as api:
                for name, call in self.calls(api).items():
                    with self.subTest(status=reply[0], call=name):
                        self.reply = reply
                        with self.assertRaises(aiohttp.ClientResponseError) as cm:
                            await call()
                        self.assertEqual(cm.exception.status, reply[0])

    async def test_success_returns_items(self):
        async with JFAPI(self.serverUrl, "secret-key") as api:
            for name, call in self.calls(api).items():
                with self.subTest(call=name):
                    self.assertEqual(await call(), [{"Id": "track-1"}])
        self.assertEqual(self.requests, [("GET", "/Items")] * 3)

    async def test_context_manager_returns_client_and_closes_session(self):
        client = JFAPI(self.serverUrl, "secret-key")
        async with client as api:
            self.assertIs(api, client)
            await api.search("mint")
            session = api._session
            self.assertFalse(session.closed)
        self.assertTrue(session.closed)
        self.assertIsNone(client._session)

    async def test_context_manager_exit_on_exception_closes_session(self):
        client = JFAPI(self.serverUrl, "secret-key")
        with self.assertRaises(ValueError):
            async with client:
                await client.search("mint")
                session = client._session
                raise ValueError("inside the block")
        self.assertTrue(session.closed)

    async def test_close_closes_session_and_can_be_repeated(self):
        api = JFAPI(self.serverUrl, "secret-key")
        await api.search("mint")
        session = api._session

        await api.close()
        await api.close()

        self.assertTrue(session.closed)
        self.assertIsNone(api._session)
        # only the search reached the server, nothing on close
        self.assertEqual(self.requests, [("GET", "/Items")])

    async def test_unused_client_makes_no_request(self):
        api = JFAPI(self.serverUrl, "secret-key")
        api.getAudioHls("item-1", 128000)  # only builds a URL
        async with api:
            pass
        await api.close()

        self.assertIsNone(api._session)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
