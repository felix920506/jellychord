import types
import unittest

import aiohttp

from jfapi import JellyfinResponseError
from tests.test_main_helpers import import_main_with_stubs


def http_error(status=401):
    return aiohttp.ClientResponseError(request_info=None, history=(), status=status)


class FailingJFAPI:
    """Every Jellyfin request fails with the given exception."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = []

    async def search(self, *args, **kwargs):
        self.calls.append("search")
        raise self.exc

    async def getItemsByIds(self, ids):
        self.calls.append("getItemsByIds")
        raise self.exc

    async def getAlbumTracks(self, albumId):
        self.calls.append("getAlbumTracks")
        raise self.exc


class DeferringContext:
    def __init__(self, guild_id=42, author_id=100):
        self.guild_id = guild_id
        self.guild = types.SimpleNamespace(id=guild_id, voice_client=None)
        self.voice_client = None
        self.author = types.SimpleNamespace(
            id=author_id, voice=types.SimpleNamespace(channel=object())
        )
        self.deferred = False
        self.responses = []

    async def defer(self, **kwargs):
        self.deferred = True

    async def respond(self, content=None, **kwargs):
        self.responses.append(content)


ERRORS = {
    "http 401": lambda: http_error(401),
    "http 500": lambda: http_error(500),
    "connection refused": lambda: aiohttp.ClientConnectionError("refused"),
    "timeout": lambda: TimeoutError(),
    "malformed response": lambda: JellyfinResponseError("no Items"),
}


class JellyfinErrorReplyTests(unittest.IsolatedAsyncioTestCase):
    """A failed Jellyfin request gets a reply instead of leaving the deferred
    command on "thinking..." (#43)."""

    @classmethod
    def setUpClass(cls):
        cls.main = import_main_with_stubs(
            {"JELLYCHORD_ENABLE_DEBUG": "1", "JELLYCHORD_DEBUG_SERVER": "1234"}
        )

    def setUp(self):
        self.main.queues.clear()
        self.main.playing.clear()
        self.main.config["fairplay"] = False

    async def run_with_errors(self, expectedCall, command):
        for name, makeError in ERRORS.items():
            with self.subTest(error=name):
                self.main.JF_APICLIENT = FailingJFAPI(makeError())
                ctx = DeferringContext()

                with self.assertLogs("jellychord", "ERROR"):
                    await command(ctx)

                self.assertEqual(self.main.JF_APICLIENT.calls, [expectedCall])
                self.assertEqual(ctx.responses, [self.main.JELLYFIN_ERROR_MESSAGE])
                self.assertEqual(self.main.queues, {})

    async def test_play(self):
        async def command(ctx):
            await self.main.play(ctx, "term", None, "last")
            self.assertTrue(ctx.deferred)

        await self.run_with_errors("search", command)

    async def test_search(self):
        async def command(ctx):
            await self.main.search(ctx, "term", None, "last")
            self.assertTrue(ctx.deferred)

        await self.run_with_errors("search", command)

    async def test_playbyid(self):
        async def command(ctx):
            await self.main.playbyid(ctx, "item-1", "last")
            self.assertTrue(ctx.deferred)

        await self.run_with_errors("getItemsByIds", command)

    async def test_album_tracks(self):
        # e.g. an album picked from /search, or the top /play result
        async def command(ctx):
            await self.main.playHelperGeneric({"Type": "MusicAlbum", "Id": "album-1"}, ctx, "last")

        await self.run_with_errors("getAlbumTracks", command)


class AlbumThenFailure(FailingJFAPI):
    """The search finds an album, fetching its tracks fails."""

    async def search(self, *args, **kwargs):
        self.calls.append("search")
        return [{"Type": "MusicAlbum", "Id": "album-1", "Name": "Album"}]

    async def getItemsByIds(self, ids):
        self.calls.append("getItemsByIds")
        return [{"Type": "MusicAlbum", "Id": "album-1", "Name": "Album"}]


class AlbumAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    """"Playing X" isn't sent before an album lookup that then fails."""

    @classmethod
    def setUpClass(cls):
        cls.main = import_main_with_stubs(
            {"JELLYCHORD_ENABLE_DEBUG": "1", "JELLYCHORD_DEBUG_SERVER": "1234"}
        )

    def setUp(self):
        self.main.queues.clear()
        self.main.config["fairplay"] = False

    async def test_play_album_failure_replies_only_with_error(self):
        self.main.JF_APICLIENT = AlbumThenFailure(http_error(500))
        ctx = DeferringContext()
        with self.assertLogs("jellychord", "ERROR"):
            await self.main.play(ctx, "term", None, "last")
        self.assertEqual(ctx.responses, [self.main.JELLYFIN_ERROR_MESSAGE])
        self.assertEqual(self.main.queues, {})

    async def test_playbyid_album_failure_replies_only_with_error(self):
        self.main.JF_APICLIENT = AlbumThenFailure(http_error(500))
        ctx = DeferringContext()
        with self.assertLogs("jellychord", "ERROR"):
            await self.main.playbyid(ctx, "album-1", "last")
        self.assertEqual(ctx.responses, [self.main.JELLYFIN_ERROR_MESSAGE])
        self.assertEqual(self.main.queues, {})


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = import_main_with_stubs()

    async def test_bot_close_closes_jellyfin_client(self):
        closed = []

        class ClosingJFAPI:
            async def close(self):
                closed.append(True)

        self.main.JF_APICLIENT = ClosingJFAPI()

        await self.main.bot.close()

        self.assertTrue(self.main.bot.closed)
        self.assertEqual(closed, [True])


if __name__ == "__main__":
    unittest.main()
