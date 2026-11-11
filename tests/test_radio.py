import types
import unittest
from unittest import mock

from tests.test_main_helpers import import_main_with_stubs
from tests.test_play_queue import FakeVoiceClient, queue_entry, queue_ids, raw_track


def run_coroutine_inline(coro, loop):
    # the fake API never really awaits, so one step runs it to completion
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("coroutine did not finish in one step")


class FakeRadioJFAPI:
    def __init__(self, songs=None, albums=None, album_tracks=None, libraries=None):
        self.songs = list(songs or [])
        self.albums = list(albums or [])
        self.album_tracks = album_tracks or {}
        self.libraries = libraries or []
        self.random_requests = []
        self.album_requests = []
        self.audio_requests = []
        self.library_requests = 0
        self.fail_random = 0

    async def getRandomItems(self, item_type, library_id=None, limit=1):
        self.random_requests.append((item_type, library_id))
        if self.fail_random:
            self.fail_random -= 1
            raise ConnectionError("jellyfin is down")
        source = self.songs if item_type == "Audio" else self.albums
        if not source:
            return []
        item = source.pop(0)
        source.append(item)
        return [item]

    async def getAlbumTracks(self, album_id):
        self.album_requests.append(album_id)
        return self.album_tracks[album_id]

    async def getMusicLibraries(self):
        self.library_requests += 1
        return self.libraries

    def getAudioHls(self, track_id, bitrate):
        self.audio_requests.append((track_id, bitrate))
        return f"https://stream.example/{track_id}"


class FakeChannel:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)
        return "send-coroutine"


class FakeRadioContext:
    def __init__(self, guild_id=42, author_id=100, voice_client=None, in_voice=True):
        self.guild_id = guild_id
        self.author = types.SimpleNamespace(id=author_id, voice=object() if in_voice else None)
        self.voice_client = voice_client
        self.guild = types.SimpleNamespace(id=guild_id, voice_client=voice_client)
        self.channel = FakeChannel()
        self.responses = []
        self.deferred = False

    async def defer(self, *args, **kwargs):
        self.deferred = True

    async def respond(self, content=None, **kwargs):
        self.responses.append(content)


def radio_state(mode="song", library_id=None, buffer=None, album=None, channel=None):
    return {
        "mode": mode,
        "libraryId": library_id,
        "libraryName": None,
        "buffer": buffer or [],
        "album": album,
        "failures": 0,
        "channel": channel,
    }


def radio_entry(track_id, album=None):
    entry = queue_entry(track_id)
    entry["Radio"] = True
    entry["Album"] = album
    return entry


class RadioTestCase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = import_main_with_stubs()

    def setUp(self):
        self.main.queues.clear()
        self.main.playing.clear()
        self.main.radioStates.clear()
        self.main.musicLibraryCache.update({"time": None, "items": []})
        self.main.config["fairplay"] = False
        self.main.runOnLoop = run_coroutine_inline
        self.main.RADIO_BACKOFF_BASE = 0
        self.voice_client = FakeVoiceClient()
        self.guild = types.SimpleNamespace(id=42, voice_client=self.voice_client)
        self.scheduled = []
        patcher = mock.patch.object(
            self.main.asyncio,
            "run_coroutine_threadsafe",
            lambda coro, loop: self.scheduled.append(coro),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def use_api(self, **kwargs):
        self.main.JF_APICLIENT = FakeRadioJFAPI(**kwargs)
        return self.main.JF_APICLIENT


class RadioPlaybackTests(RadioTestCase):
    def test_song_mode_fetches_a_random_song_when_queue_is_empty(self):
        api = self.use_api(songs=[raw_track("random-1")])
        self.main.radioStates[42] = radio_state(library_id="lib-1")

        self.main.playNextTrack(self.guild)

        self.assertEqual(api.random_requests, [("Audio", "lib-1")])
        self.assertEqual(self.main.playing[42]["Id"], "random-1")
        self.assertTrue(self.main.playing[42]["Radio"])
        self.assertIsNone(self.main.playing[42]["Album"])
        self.assertEqual(len(self.voice_client.play_calls), 1)
        self.assertFalse(self.voice_client.disconnect_requested)

    def test_song_mode_fetches_again_for_every_track(self):
        api = self.use_api(songs=[raw_track("random-1"), raw_track("random-2")])
        self.main.radioStates[42] = radio_state()

        self.main.playNextTrack(self.guild)
        self.main.playNextTrack(self.guild)

        self.assertEqual(len(api.random_requests), 2)
        self.assertEqual([track_id for track_id, _ in api.audio_requests], ["random-1", "random-2"])

    def test_queue_goes_before_radio_and_radio_resumes_after(self):
        api = self.use_api(songs=[raw_track("random-1")])
        self.main.radioStates[42] = radio_state()
        self.main.queues[42] = [queue_entry("queued")]

        self.main.playNextTrack(self.guild)

        self.assertEqual(self.main.playing[42]["Id"], "queued")
        self.assertNotIn("Radio", self.main.playing[42])
        self.assertEqual(api.random_requests, [])

        self.main.playNextTrack(self.guild)

        self.assertEqual(self.main.playing[42]["Id"], "random-1")
        self.assertFalse(self.voice_client.disconnect_requested)

    def test_album_mode_plays_album_tracks_in_order_then_picks_another_album(self):
        api = self.use_api(
            albums=[{"Id": "album-1", "Name": "First Album"}, {"Id": "album-2", "Name": "Second Album"}],
            album_tracks={
                "album-1": [raw_track("a1"), raw_track("a2"), raw_track("a3")],
                "album-2": [raw_track("b1")],
            },
        )
        self.main.radioStates[42] = radio_state(mode="album", library_id="lib-1")

        for _ in range(4):
            self.main.playNextTrack(self.guild)

        self.assertEqual([track_id for track_id, _ in api.audio_requests], ["a1", "a2", "a3", "b1"])
        self.assertEqual(api.random_requests, [("MusicAlbum", "lib-1"), ("MusicAlbum", "lib-1")])
        self.assertEqual(api.album_requests, ["album-1", "album-2"])
        self.assertEqual(self.main.playing[42]["Album"], "Second Album")

    def test_album_mode_continues_same_album_after_a_queued_track(self):
        api = self.use_api(
            albums=[{"Id": "album-1", "Name": "First Album"}],
            album_tracks={"album-1": [raw_track("a1"), raw_track("a2"), raw_track("a3")]},
        )
        self.main.radioStates[42] = radio_state(mode="album")

        self.main.playNextTrack(self.guild)
        self.main.queues[42] = [queue_entry("queued")]
        self.main.playNextTrack(self.guild)
        self.main.playNextTrack(self.guild)

        self.assertEqual([track_id for track_id, _ in api.audio_requests], ["a1", "queued", "a2"])
        self.assertEqual(len(api.random_requests), 1)
        self.assertEqual(queue_ids(self.main.radioStates[42]["buffer"]), ["a3"])

    def test_disconnects_once_radio_is_off_and_queue_is_empty(self):
        self.use_api()
        self.main.playing[42] = radio_entry("finished")

        self.main.playNextTrack(self.guild)

        self.assertNotIn(42, self.main.playing)
        self.assertTrue(self.voice_client.disconnect_requested)

    def test_lost_voice_connection_clears_radio_state(self):
        self.use_api(songs=[raw_track("random-1")])
        self.main.radioStates[42] = radio_state()
        self.main.playing[42] = radio_entry("finished")
        guild = types.SimpleNamespace(id=42, voice_client=None)

        self.main.playNextTrack(guild)

        self.assertNotIn(42, self.main.radioStates)
        self.assertNotIn(42, self.main.playing)

    def test_fairplay_queue_works_while_a_radio_track_is_playing(self):
        self.main.config["fairplay"] = True
        self.use_api(songs=[raw_track("random-1")])
        self.main.radioStates[42] = radio_state()
        self.main.playNextTrack(self.guild)

        self.main.queues[42] = {100: [queue_entry("user-100-a")], 200: [queue_entry("user-200-a")]}

        self.assertEqual(queue_ids(self.main.getFlattenedPlayQueue(42)), ["user-100-a", "user-200-a"])

        self.main.playNextTrack(self.guild)

        self.assertEqual(self.main.playing[42]["Id"], "user-100-a")
        self.assertEqual(queue_ids(self.main.getFlattenedPlayQueue(42)), ["user-200-a"])

    def test_fairplay_flattened_queue_works_when_nothing_is_playing(self):
        self.main.config["fairplay"] = True
        self.main.queues[42] = {100: [queue_entry("user-100-a")]}

        self.assertEqual(queue_ids(self.main.getFlattenedPlayQueue(42)), ["user-100-a"])


class RadioFailureTests(RadioTestCase):
    def test_recovers_after_a_failed_fetch_and_resets_failure_count(self):
        api = self.use_api(songs=[raw_track("random-1")])
        api.fail_random = 2
        self.main.radioStates[42] = radio_state()

        self.main.playNextTrack(self.guild)

        self.assertEqual(len(api.random_requests), 3)
        self.assertEqual(self.main.playing[42]["Id"], "random-1")
        self.assertEqual(self.main.radioStates[42]["failures"], 0)

    def test_turns_radio_off_and_disconnects_after_too_many_failures(self):
        api = self.use_api(songs=[raw_track("random-1")])
        api.fail_random = 100
        channel = FakeChannel()
        self.main.radioStates[42] = radio_state(channel=channel)
        self.main.playing[42] = radio_entry("finished")

        self.main.playNextTrack(self.guild)

        self.assertEqual(len(api.random_requests), self.main.RADIO_MAX_FAILURES)
        self.assertNotIn(42, self.main.radioStates)
        self.assertNotIn(42, self.main.playing)
        self.assertEqual(self.voice_client.play_calls, [])
        self.assertTrue(self.voice_client.disconnect_requested)
        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(self.scheduled, ["send-coroutine", "disconnect-coroutine"])

    def test_empty_library_counts_as_failure_until_radio_turns_off(self):
        api = self.use_api()
        self.main.radioStates[42] = radio_state()

        self.main.playNextTrack(self.guild)

        self.assertEqual(len(api.random_requests), self.main.RADIO_MAX_FAILURES)
        self.assertNotIn(42, self.main.radioStates)
        self.assertTrue(self.voice_client.disconnect_requested)

    def test_album_without_tracks_is_skipped(self):
        api = self.use_api(
            albums=[{"Id": "empty", "Name": "Empty"}, {"Id": "album-1", "Name": "First Album"}],
            album_tracks={"empty": [], "album-1": [raw_track("a1")]},
        )
        self.main.radioStates[42] = radio_state(mode="album")

        self.main.playNextTrack(self.guild)

        self.assertEqual(api.album_requests, ["empty", "album-1"])
        self.assertEqual(self.main.playing[42]["Id"], "a1")

    def test_track_with_no_audio_is_skipped_while_radio_is_on(self):
        api = self.use_api(songs=[raw_track("broken"), raw_track("good")])
        self.main.radioStates[42] = radio_state()
        created = []
        base = self.main.discord.FFmpegOpusAudio

        class SometimesSilentAudio(base):
            def __init__(self, url, **kwargs):
                super().__init__(url, **kwargs)
                created.append(self)

            def read(self):
                super().read()
                return b"" if "broken" in self.url else b"\x00"

        with mock.patch.object(self.main.discord, "FFmpegOpusAudio", SometimesSilentAudio):
            self.main.playNextTrack(self.guild)

        self.assertEqual([track_id for track_id, _ in api.audio_requests], ["broken", "good"])
        self.assertTrue(created[0].cleaned_up)
        self.assertFalse(created[1].cleaned_up)
        self.assertEqual(len(self.voice_client.play_calls), 1)
        self.assertEqual(self.main.playing[42]["Id"], "good")

    def test_backoff_wait_ends_early_when_something_gets_queued(self):
        self.use_api()
        self.main.RADIO_BACKOFF_BASE = 60
        state = radio_state()
        self.main.radioStates[42] = state
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            self.main.queues[42] = [queue_entry("queued")]

        with mock.patch.object(self.main.time, "sleep", fake_sleep):
            self.main.handleRadioFailure(self.guild, self.voice_client, state, RuntimeError("boom"))

        self.assertEqual(len(sleeps), 1)
        self.assertEqual(state["failures"], 1)

    def test_backoff_wait_ends_early_when_radio_is_turned_off(self):
        self.use_api()
        self.main.RADIO_BACKOFF_BASE = 60
        state = radio_state()
        self.main.radioStates[42] = state
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            self.main.radioStates.pop(42)

        with mock.patch.object(self.main.time, "sleep", fake_sleep):
            self.main.handleRadioFailure(self.guild, self.voice_client, state, RuntimeError("boom"))

        self.assertEqual(len(sleeps), 1)

    def test_queue_track_failure_still_raises_when_radio_is_off(self):
        self.use_api()
        self.main.queues[42] = [queue_entry("queued")]

        def broken_play(audio, after):
            raise RuntimeError("not connected")

        self.voice_client.play = broken_play

        with self.assertRaises(RuntimeError):
            self.main.playNextTrack(self.guild)


class RadioCommandTests(RadioTestCase):
    async def test_queueing_a_track_does_not_cut_the_radio_track(self):
        self.use_api()
        self.main.radioStates[42] = radio_state()
        self.main.playing[42] = radio_entry("random-1")
        ctx = FakeRadioContext(voice_client=self.voice_client)

        await self.main.playHelperGeneric(raw_track("queued-last"), ctx, "last")
        await self.main.playHelperGeneric(raw_track("queued-next"), ctx, "next")

        self.assertFalse(self.voice_client.stopped)
        self.assertEqual(queue_ids(self.main.queues[42]), ["queued-next", "queued-last"])
        self.assertEqual(self.main.playing[42]["Id"], "random-1")

    async def test_play_now_cuts_the_radio_track(self):
        self.use_api()
        self.main.radioStates[42] = radio_state()
        self.main.playing[42] = radio_entry("random-1")
        ctx = FakeRadioContext(voice_client=self.voice_client)

        await self.main.playHelperGeneric(raw_track("urgent"), ctx, "now")

        self.assertTrue(self.voice_client.stopped)
        self.assertIn(42, self.main.radioStates)

    async def test_stop_turns_radio_off_and_clears_queue(self):
        self.use_api(songs=[raw_track("random-1")])
        self.main.radioStates[42] = radio_state()
        self.main.queues[42] = [queue_entry("queued")]
        self.main.playing[42] = radio_entry("random-1")
        ctx = FakeRadioContext(voice_client=self.voice_client)

        await self.main.stop(ctx)

        self.assertNotIn(42, self.main.radioStates)
        self.assertNotIn(42, self.main.queues)
        self.assertTrue(self.voice_client.stopped)

        self.main.playNextTrack(self.guild)

        self.assertTrue(self.voice_client.disconnect_requested)
        self.assertEqual(self.voice_client.play_calls, [])

    async def test_radio_on_validates_and_stores_state_without_touching_playback(self):
        api = self.use_api(
            songs=[raw_track("random-1")],
            libraries=[{"Id": "lib-1", "Name": "Music"}],
        )
        ctx = FakeRadioContext(voice_client=self.voice_client)

        await self.main.radio(ctx, "song", "lib-1")

        state = self.main.radioStates[42]
        self.assertEqual((state["mode"], state["libraryId"], state["libraryName"]), ("song", "lib-1", "Music"))
        self.assertIs(state["channel"], ctx.channel)
        self.assertEqual(api.random_requests, [("Audio", "lib-1")])
        self.assertFalse(self.voice_client.stopped)
        self.assertEqual(self.voice_client.play_calls, [])
        self.assertEqual(ctx.responses, ["Radio on: playing random songs from the Music library whenever the queue is empty"])

    async def test_radio_on_accepts_library_name_typed_by_hand(self):
        self.use_api(albums=[{"Id": "album-1", "Name": "First Album"}], libraries=[{"Id": "lib-1", "Name": "Music"}])
        ctx = FakeRadioContext(voice_client=self.voice_client)

        await self.main.radio(ctx, "album", "music")

        self.assertEqual(self.main.radioStates[42]["libraryId"], "lib-1")
        self.assertEqual(self.main.radioStates[42]["mode"], "album")

    async def test_radio_on_starts_player_when_not_connected(self):
        self.use_api(songs=[raw_track("random-1")])
        ctx = FakeRadioContext(voice_client=None)
        started = []

        async def fake_start_player(context):
            started.append(context)

        with mock.patch.object(self.main, "startPlayer", fake_start_player):
            await self.main.radio(ctx, "song")

        self.assertEqual(started, [ctx])
        self.assertIsNone(self.main.radioStates[42]["libraryId"])
        self.assertEqual(ctx.responses, ["Radio on: playing random songs from all libraries whenever the queue is empty"])

    async def test_radio_on_drops_state_when_player_fails_to_start(self):
        self.use_api(songs=[raw_track("random-1")])
        ctx = FakeRadioContext(voice_client=None)

        async def failing_start_player(context):
            raise RuntimeError("cannot connect")

        with mock.patch.object(self.main, "startPlayer", failing_start_player):
            with self.assertRaises(RuntimeError):
                await self.main.radio(ctx, "song")

        self.assertNotIn(42, self.main.radioStates)

    async def test_radio_on_requires_a_voice_channel(self):
        api = self.use_api(songs=[raw_track("random-1")])
        ctx = FakeRadioContext(voice_client=None, in_voice=False)

        await self.main.radio(ctx, "song")

        self.assertNotIn(42, self.main.radioStates)
        self.assertEqual(api.random_requests, [])
        self.assertEqual(ctx.responses, ["You are not in any voice channel"])

    async def test_radio_on_rejects_unknown_library(self):
        self.use_api(songs=[raw_track("random-1")], libraries=[{"Id": "lib-1", "Name": "Music"}])
        ctx = FakeRadioContext(voice_client=self.voice_client)

        await self.main.radio(ctx, "song", "nope")

        self.assertNotIn(42, self.main.radioStates)
        self.assertEqual(ctx.responses, ["Music library not found, pick one from the suggestions"])

    async def test_radio_on_rejects_library_with_nothing_to_play(self):
        self.use_api()
        ctx = FakeRadioContext(voice_client=self.voice_client)

        await self.main.radio(ctx, "album")

        self.assertNotIn(42, self.main.radioStates)
        self.assertEqual(ctx.responses, ["No albums found in all libraries"])

    async def test_radio_on_reports_unreachable_server(self):
        api = self.use_api(songs=[raw_track("random-1")])
        api.fail_random = 1
        ctx = FakeRadioContext(voice_client=self.voice_client)

        await self.main.radio(ctx, "song")

        self.assertNotIn(42, self.main.radioStates)
        self.assertEqual(ctx.responses, ["Could not get items from the Jellyfin server"])

    async def test_radio_on_again_with_same_settings_keeps_album_buffer(self):
        self.use_api(albums=[{"Id": "album-1", "Name": "First Album"}])
        state = radio_state(mode="album", buffer=[radio_entry("a2", "First Album")], album="First Album")
        self.main.radioStates[42] = state
        ctx = FakeRadioContext(voice_client=self.voice_client)

        await self.main.radio(ctx, "album")

        self.assertIs(self.main.radioStates[42], state)
        self.assertEqual(ctx.responses, ["Radio is already playing random albums from all libraries"])

    async def test_switching_mode_drops_the_rest_of_the_album(self):
        api = self.use_api(songs=[raw_track("random-1")])
        self.main.radioStates[42] = radio_state(mode="album", buffer=[radio_entry("a2", "First Album")], album="First Album")
        self.main.playing[42] = radio_entry("a1", "First Album")
        ctx = FakeRadioContext(voice_client=self.voice_client)

        await self.main.radio(ctx, "song")

        self.assertFalse(self.voice_client.stopped)
        self.assertEqual(self.main.radioStates[42]["mode"], "song")
        self.assertEqual(self.main.radioStates[42]["buffer"], [])

        self.main.playNextTrack(self.guild)

        self.assertEqual([track_id for track_id, _ in api.audio_requests], ["random-1"])

    async def test_radio_off_lets_current_track_and_queue_finish(self):
        self.use_api(songs=[raw_track("random-1")])
        self.main.radioStates[42] = radio_state()
        self.main.queues[42] = [queue_entry("queued")]
        ctx = FakeRadioContext(voice_client=self.voice_client)

        await self.main.radio(ctx, "off")

        self.assertNotIn(42, self.main.radioStates)
        self.assertFalse(self.voice_client.stopped)
        self.assertEqual(queue_ids(self.main.queues[42]), ["queued"])

        self.main.playNextTrack(self.guild)
        self.assertEqual(self.main.playing[42]["Id"], "queued")
        self.main.playNextTrack(self.guild)
        self.assertTrue(self.voice_client.disconnect_requested)

    async def test_radio_off_when_not_on(self):
        ctx = FakeRadioContext(voice_client=self.voice_client)

        await self.main.radio(ctx, "off")

        self.assertEqual(ctx.responses, ["Radio is not on"])

    async def test_pause_and_resume_answer_while_radio_waits_between_tracks(self):
        self.main.radioStates[42] = radio_state()
        ctx = FakeRadioContext(voice_client=self.voice_client)

        await self.main.pause(ctx)
        await self.main.resume(ctx)

        self.assertEqual(ctx.responses, ["Not currently playing", "Not currently playing"])

    async def test_library_autocomplete_filters_by_typed_text_and_caches(self):
        api = self.use_api(libraries=[{"Id": "lib-1", "Name": "Music"}, {"Id": "lib-2", "Name": "Soundtracks"}])

        everything = await self.main.libraryAutocomplete(types.SimpleNamespace(value=""))
        filtered = await self.main.libraryAutocomplete(types.SimpleNamespace(value="sound"))

        self.assertEqual(
            everything,
            [{"name": "Music", "value": "lib-1"}, {"name": "Soundtracks", "value": "lib-2"}],
        )
        self.assertEqual(filtered, [{"name": "Soundtracks", "value": "lib-2"}])
        self.assertEqual(api.library_requests, 1)


class RadioDisplayTests(RadioTestCase):
    def test_queue_message_numbers_from_one_and_adds_radio_line(self):
        self.main.queues[42] = [queue_entry("first"), queue_entry("second")]

        content, pages = self.main.getQueueMessage(42)

        self.assertEqual(content, "Tracks in playlist:\n1. Artist - first\n2. Artist - second\nPage: 1/1")
        self.assertEqual(pages, 1)

        self.main.radioStates[42] = radio_state()
        content, _ = self.main.getQueueMessage(42)

        self.assertTrue(content.endswith("\nRadio is on: random songs play when the queue is empty"))

    def test_queue_message_second_page_keeps_absolute_numbers(self):
        self.main.queues[42] = [queue_entry(f"track-{i}") for i in range(1, 23)]

        content, pages = self.main.getQueueMessage(42, 1)

        self.assertEqual(pages, 2)
        self.assertEqual(content, "Tracks in playlist:\n21. Artist - track-21\n22. Artist - track-22\nPage: 2/2")

    def test_radio_status_line_names_album_that_will_continue(self):
        self.assertEqual(self.main.getRadioStatusLine(42), "")

        self.main.radioStates[42] = radio_state(mode="album")
        self.assertEqual(
            self.main.getRadioStatusLine(42),
            "Radio is on: random albums play when the queue is empty",
        )

        self.main.radioStates[42] = radio_state(mode="album", buffer=[radio_entry("a2", "First Album")], album="First Album")
        self.assertEqual(
            self.main.getRadioStatusLine(42),
            "Radio is on: continuing album First Album when the queue is empty",
        )

    async def test_queue_command_shows_radio_line_for_empty_queue(self):
        self.main.radioStates[42] = radio_state()
        ctx = FakeRadioContext(voice_client=self.voice_client)

        await self.main.queue(ctx)

        self.assertEqual(ctx.responses, ["Empty Queue\nRadio is on: random songs play when the queue is empty"])

    async def test_nowplaying_marks_radio_tracks(self):
        self.use_api(
            albums=[{"Id": "album-1", "Name": "First Album"}],
            album_tracks={"album-1": [raw_track("a1")]},
        )
        self.main.radioStates[42] = radio_state(mode="album")
        self.main.playNextTrack(self.guild)
        self.main.playing[42]["paused"] = True
        ctx = FakeRadioContext(voice_client=self.voice_client)

        await self.main.nowplaying(ctx)

        self.assertEqual(ctx.responses, ["Currently Playing: Artist - a1 00:00/02:00 [Radio: First Album]"])


if __name__ == "__main__":
    unittest.main()
