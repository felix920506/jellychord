import datetime
import importlib
import os
import sys
import types
import unittest
from unittest import mock


def make_discord_stub():
    discord = types.ModuleType("discord")
    discord_ext = types.ModuleType("discord.ext")

    class FakeSelect:
        # Like py-cord's Select, the user's choice is only exposed through the
        # public values property (None until refresh_state has run). The
        # private storage deliberately has a different name from py-cord's.
        def __init__(self, *args, **kwargs):
            self.options = []
            self.view = None
            self._fake_selected = None

        def add_option(self, **kwargs):
            self.options.append(kwargs)

        def refresh_state(self, data):
            self._fake_selected = list(data.get("values", []))

        @property
        def values(self):
            return self._fake_selected

    class FakeButton:
        def __init__(self, *args, **kwargs):
            self.disabled = False
            self.label = None
            self.view = None

    class FakeView:
        def __init__(self, *args, **kwargs):
            self.items = []
            self.stopped = False
            self.message = None

        def stop(self):
            self.stopped = True

        # Defaults match py-cord's View.
        async def interaction_check(self, interaction):
            return True

        async def on_check_failure(self, interaction):
            pass

        def add_item(self, item):
            item.view = self
            self.items.append(item)

        def disable_all_items(self):
            for item in self.items:
                item.disabled = True

    class FakeGroup:
        def command(self, *args, **kwargs):
            def decorator(func):
                return func

            return decorator

    class FakeBot:
        def __init__(self, *args, **kwargs):
            self.groups = []
            self.run_token = None
            self.user = None
            self.listeners = []

        def add_listener(self, func, name):
            self.listeners.append((name, func))

        def create_group(self, *args, **kwargs):
            self.groups.append((args, kwargs))
            return FakeGroup()

        def run(self, token):
            self.run_token = token

        async def close(self):
            self.closed = True

    class FakeFFmpegOpusAudio:
        def __init__(self, url, **kwargs):
            self.url = url
            self.kwargs = kwargs
            self.read_called = False
            self.cleaned_up = False

        def read(self):
            self.read_called = True
            return b""

        def cleanup(self):
            self.cleaned_up = True

    def option(*args, **kwargs):
        return args[0] if args else object

    discord.Bot = FakeBot
    discord.Option = option
    discord.SelectOption = lambda *args, **kwargs: {"args": args, "kwargs": kwargs}
    discord.OptionChoice = lambda name, value: {"name": name, "value": value}
    discord.AutocompleteContext = type("AutocompleteContext", (), {})
    discord.ApplicationContext = type("ApplicationContext", (), {})
    discord.Interaction = type("Interaction", (), {})
    discord.Guild = type("Guild", (), {})
    discord.Member = type("Member", (), {})
    discord.VoiceState = type("VoiceState", (), {})
    discord.FFmpegOpusAudio = FakeFFmpegOpusAudio
    discord.ui = types.SimpleNamespace(Select=FakeSelect, Button=FakeButton, View=FakeView)
    discord.ext = discord_ext
    return discord, discord_ext


def import_main_with_stubs(env_overrides=None):
    """Import a fresh copy of the bot module (jellychord/bot.py) with discord
    stubbed out and settings from the environment."""
    discord, discord_ext = make_discord_stub()
    env = {
        "JELLYCHORD_USE_CONF_FILE": "0",
        "JELLYCHORD_DC_TOKEN": "discord-token",
        "JELLYCHORD_JF_SERVER": "https://jellyfin.example.com/",
        "JELLYCHORD_JF_APIKEY": "api-key",
        "JELLYCHORD_COMMAND_GROUP": "jellychord",
        "JELLYCHORD_SEARCH_LIMIT": "25",
        "JELLYCHORD_ENABLE_DEBUG": "0",
        "JELLYCHORD_ENABLE_FAIRPLAY": "0",
        "JELLYCHORD_LOG_LEVEL": "WARNING",
        **(env_overrides or {}),
    }

    sys.modules.pop("jellychord.bot", None)
    with mock.patch.dict(os.environ, env), mock.patch.dict(
        sys.modules, {"discord": discord, "discord.ext": discord_ext}
    ):
        return importlib.import_module("jellychord.bot")


class MainHelperTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = import_main_with_stubs()

    def setUp(self):
        self.main.queues.clear()
        self.main.playing.clear()
        self.main.config["fairplay"] = False

    def test_format_time_secs(self):
        self.assertEqual(self.main.formatTimeSecs(0), "00:00")
        self.assertEqual(self.main.formatTimeSecs(65), "01:05")
        self.assertEqual(self.main.formatTimeSecs(3661), "1:01:01")
        self.assertEqual(self.main.formatTimeSecs(61, force_hrs=True), "0:01:01")

    def test_get_item_entry_converts_runtime_ticks_to_seconds(self):
        item = {
            "Artists": ["The Artist"],
            "Name": "Track",
            "Id": "track-1",
            "RunTimeTicks": 185_000_0000,
        }

        self.assertEqual(
            self.main.getItemEntry(item),
            {
                "Artists": ["The Artist"],
                "Name": "Track",
                "Id": "track-1",
                "Length": 185,
            },
        )

    def test_get_track_string_formats_track_album_and_artist_cases(self):
        track = {
            "Type": "Audio",
            "Artists": ["The Artist"],
            "Name": "Track",
            "RunTimeTicks": 185_000_0000,
        }
        album = {
            "Type": "MusicAlbum",
            "Artists": ["First", "Second"],
            "Name": "Compilation",
            "RunTimeTicks": 0,
        }
        no_artist = {
            "Type": "Audio",
            "Artists": [],
            "Name": "Untitled",
            "RunTimeTicks": 0,
        }

        self.assertEqual(
            self.main.getTrackString(track, showType=True, showSize=True),
            "Track: The Artist - Track (03:05)",
        )
        self.assertEqual(
            self.main.getTrackString(album, artistLimit=1, showType=True),
            "Album: Various Artists - Compilation",
        )
        self.assertEqual(self.main.getTrackString(no_artist), "Untitled")

    def test_get_item_entry_defaults_missing_artists_and_length(self):
        self.assertEqual(
            self.main.getItemEntry({"Name": "Track", "Id": "track-1"}),
            {"Artists": [], "Name": "Track", "Id": "track-1", "Length": 0},
        )
        self.assertEqual(
            self.main.getItemEntry({"Name": "Track", "Id": "track-1", "Artists": None, "RunTimeTicks": None}),
            {"Artists": [], "Name": "Track", "Id": "track-1", "Length": 0},
        )

    def test_get_track_string_handles_missing_artists_and_length(self):
        self.assertEqual(
            self.main.getTrackString({"Type": "Audio", "Name": "Bare"}, showType=True, showSize=True),
            "Track: Bare",
        )
        self.assertEqual(
            self.main.getTrackString({"Type": "Audio", "Name": "Zero", "Artists": ["A"], "RunTimeTicks": 0},
                                     showSize=True),
            "A - Zero",
        )
        # queue entries carry Length instead of RunTimeTicks and no Type
        self.assertEqual(self.main.getTrackString({"Name": "Entry", "Id": "e", "Artists": [], "Length": 0}), "Entry")

    def playing_now(self, length, elapsed=75, **extra):
        self.main.playing[42] = {
            "Artists": ["Artist"],
            "Name": "Song",
            "Id": "song",
            "paused": True,
            "playtime-offset": datetime.timedelta(seconds=elapsed),
            "starttime": datetime.datetime.now(),
            **({} if length is None else {"Length": length}),
            **extra,
        }

    async def nowplaying_reply(self):
        ctx = types.SimpleNamespace(guild_id=42, respond=mock.AsyncMock())
        await self.main.nowplaying(ctx)
        return ctx.respond.await_args.args[0]

    async def test_nowplaying_shows_elapsed_and_total(self):
        self.playing_now(185)
        self.assertEqual(await self.nowplaying_reply(), "Currently Playing: Artist - Song 01:15/03:05")

    async def test_nowplaying_with_unknown_length(self):
        self.playing_now(0)
        self.assertEqual(await self.nowplaying_reply(), "Currently Playing: Artist - Song 01:15/--:--")

    async def test_nowplaying_with_no_length_or_artists(self):
        self.playing_now(None, Artists=[])
        self.assertEqual(await self.nowplaying_reply(), "Currently Playing: Song 01:15/--:--")

    async def test_search_helper_maps_user_search_types_to_jellyfin_types(self):
        calls = []

        class FakeJFAPI:
            async def search(self, term, limit, types):
                calls.append((term, limit, types))
                return [{"Id": "result"}]

        self.main.JF_APICLIENT = FakeJFAPI()

        self.assertEqual(await self.main.searchHelper("song", searchType="Soundtrack"), [{"Id": "result"}])
        self.assertEqual(await self.main.searchHelper("album", searchType="Album"), [{"Id": "result"}])
        self.assertEqual(await self.main.searchHelper("both"), [{"Id": "result"}])
        self.assertEqual(
            calls,
            [
                ("song", self.main.LIMIT, ["Audio"]),
                ("album", self.main.LIMIT, ["MusicAlbum"]),
                ("both", self.main.LIMIT, ["Audio", "MusicAlbum"]),
            ],
        )

    def test_get_flattened_play_queue_returns_empty_for_missing_guild(self):
        self.assertEqual(self.main.getFlattenedPlayQueue(42), [])

    def test_get_flattened_play_queue_returns_plain_queue_when_fairplay_disabled(self):
        queue = [{"Id": "a"}, {"Id": "b"}]
        self.main.queues[42] = queue

        self.assertIs(self.main.getFlattenedPlayQueue(42), queue)

    def test_get_flattened_play_queue_round_robins_fairplay_queues_from_next_user(self):
        self.main.config["fairplay"] = True
        self.main.playing[42] = {"nextTrackUserIdx": 1}
        self.main.queues[42] = {
            100: [{"Id": "a1"}, {"Id": "a2"}],
            200: [{"Id": "b1"}],
            300: [{"Id": "c1"}, {"Id": "c2"}],
        }

        self.assertEqual(
            self.main.getFlattenedPlayQueue(42),
            [
                {"Id": "b1"},
                {"Id": "c1"},
                {"Id": "a1"},
                {"Id": "c2"},
                {"Id": "a2"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
