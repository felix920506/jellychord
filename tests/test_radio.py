"""Radio mode: random songs or albums whenever the queue is empty (#24)."""

import asyncio
import logging
import threading
import types
import unittest
from unittest import mock

import aiohttp

from tests.test_main_helpers import import_main_with_stubs
from tests.test_play_queue import FakeChannel, FakeVoiceClient, LiveContext, queue_entry, queue_ids, raw_track
from tests.test_queue_view import make_interaction


class FakeRadioJFAPI:
    """Hands out songs and albums in a fixed rotation instead of at random.
    The first fail_random calls to getRandomItems raise."""

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
        self.fail_libraries = False

    async def getRandomItems(self, item_type, library_id=None, limit=1):
        self.random_requests.append((item_type, library_id))
        if self.fail_random:
            self.fail_random -= 1
            raise aiohttp.ClientError("jellyfin is down")
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
        if self.fail_libraries:
            raise aiohttp.ClientError("jellyfin is down")
        return self.libraries

    def getAudioHls(self, track_id, bitrate):
        self.audio_requests.append((track_id, bitrate))
        return f"https://stream.example/{track_id}"

    def played_ids(self):
        return [track_id for track_id, _ in self.audio_requests]


class FakeTextChannel:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)


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
        for state in (self.main.queues, self.main.playing, self.main.loading, self.main.playerLocks,
                      self.main.leaving, self.main.radioStates, self.main.currentPlayers,
                      self.main.expectedDisconnects, self.main.voiceEpochs):
            state.clear()
        self.main.musicLibraryCache.update({"time": None, "items": []})
        self.main.config["fairplay"] = False

        # Failures are logged with tracebacks; keep them out of the test output.
        # (assertLogs still sees them: it installs its own handler.)
        quiet = logging.NullHandler()
        self.main.log.addHandler(quiet)
        self.main.log.propagate = False
        self.addCleanup(setattr, self.main.log, "propagate", True)
        self.addCleanup(self.main.log.removeHandler, quiet)

        # The stub's source produces no audio; radio needs one that does.
        test = self
        self.sources = []

        class AudibleAudio(self.main.discord.FFmpegOpusAudio):
            def __init__(inner_self, url, **kwargs):
                super().__init__(url, **kwargs)
                test.sources.append(inner_self)

            def read(inner_self):
                super().read()
                return b"" if "silent" in inner_self.url else b"\x00"

        for target, name, value in (
            (self.main.discord, "FFmpegOpusAudio", AudibleAudio),
            # no waiting between attempts unless a test asks for it
            (self.main, "RADIO_BACKOFF_BASE", 0),
            (self.main, "RADIO_POLL_INTERVAL", 0.001),
            (self.main, "VOICE_POLL_INTERVAL", 0.001),
            (self.main, "EXPECTED_LEAVE_GRACE", 0),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.guild = types.SimpleNamespace(id=42, voice_client=None)
        self.voice_client = FakeVoiceClient()
        self.voice_client.guild = self.guild
        self.guild.voice_client = self.voice_client

    def tearDown(self):
        self.main.config["fairplay"] = False

    def use_api(self, **kwargs):
        self.main.JF_APICLIENT = FakeRadioJFAPI(**kwargs)
        return self.main.JF_APICLIENT

    def turn_radio_on(self, **kwargs):
        state = self.main.radioStates[42] = radio_state(**kwargs)
        return state

    def ctx(self, author_id=100, channel=None):
        ctx = LiveContext(self.guild, author_id=author_id, channel=channel)
        ctx.channel = FakeTextChannel()
        return ctx

    async def wait_for(self, predicate, timeout=5):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.001)

    async def wait_for_plays(self, count):
        await self.wait_for(lambda: len(self.voice_client.play_calls) == count)

    async def wait_until_idle(self):
        """Until the player has finished whatever it was doing."""
        await self.wait_for(lambda: 42 not in self.main.playerLocks or not self.main.playerLocks[42].locked())

    async def next_track(self, count):
        """End the current track and wait for the count-th one to start."""
        self.voice_client.finish_track()
        await self.wait_for_plays(count)


class RadioPlaybackTests(RadioTestCase):
    async def test_song_mode_plays_a_random_song_when_the_queue_is_empty(self):
        api = self.use_api(songs=[raw_track("random-1")])
        self.turn_radio_on(library_id="lib-1")

        await self.main.advanceTrack(self.guild)

        self.assertEqual(api.random_requests, [("Audio", "lib-1")])
        self.assertEqual(self.main.playing[42]["Id"], "random-1")
        self.assertTrue(self.main.playing[42]["Radio"])
        self.assertIsNone(self.main.playing[42]["Album"])
        self.assertFalse(self.main.playing[42]["paused"])
        self.assertEqual(len(self.voice_client.play_calls), 1)
        self.assertFalse(self.voice_client.disconnect_requested)

    async def test_song_mode_picks_again_for_every_track(self):
        api = self.use_api(songs=[raw_track("random-1"), raw_track("random-2")])
        self.turn_radio_on()

        await self.main.advanceTrack(self.guild)
        await self.next_track(2)
        await self.next_track(3)

        self.assertEqual(len(api.random_requests), 3)
        self.assertEqual(api.played_ids(), ["random-1", "random-2", "random-1"])
        self.assertFalse(self.voice_client.disconnect_requested)

    async def test_queue_goes_before_radio_and_radio_continues_after(self):
        api = self.use_api(songs=[raw_track("random-1")])
        self.turn_radio_on()
        self.main.queues[42] = [queue_entry("queued")]

        await self.main.advanceTrack(self.guild)

        self.assertEqual(self.main.playing[42]["Id"], "queued")
        self.assertNotIn("Radio", self.main.playing[42])
        self.assertEqual(api.random_requests, [])

        await self.next_track(2)

        self.assertEqual(self.main.playing[42]["Id"], "random-1")
        self.assertFalse(self.voice_client.disconnect_requested)

    async def test_album_mode_plays_an_album_in_order_then_picks_another(self):
        api = self.use_api(
            albums=[{"Id": "album-1", "Name": "First Album"}, {"Id": "album-2", "Name": "Second Album"}],
            album_tracks={
                "album-1": [raw_track("a1"), raw_track("a2"), raw_track("a3")],
                "album-2": [raw_track("b1")],
            },
        )
        self.turn_radio_on(mode="album", library_id="lib-1")

        await self.main.advanceTrack(self.guild)
        for count in (2, 3, 4):
            await self.next_track(count)

        self.assertEqual(api.played_ids(), ["a1", "a2", "a3", "b1"])
        self.assertEqual(api.random_requests, [("MusicAlbum", "lib-1"), ("MusicAlbum", "lib-1")])
        self.assertEqual(api.album_requests, ["album-1", "album-2"])
        self.assertEqual(self.main.playing[42]["Album"], "Second Album")

    async def test_album_mode_continues_the_same_album_after_a_queued_track(self):
        api = self.use_api(
            albums=[{"Id": "album-1", "Name": "First Album"}],
            album_tracks={"album-1": [raw_track("a1"), raw_track("a2"), raw_track("a3")]},
        )
        state = self.turn_radio_on(mode="album")

        await self.main.advanceTrack(self.guild)
        self.main.queues[42] = [queue_entry("queued")]
        await self.next_track(2)
        await self.next_track(3)

        self.assertEqual(api.played_ids(), ["a1", "queued", "a2"])
        self.assertEqual(len(api.random_requests), 1)
        self.assertEqual(queue_ids(state["buffer"]), ["a3"])

    async def test_album_mode_only_plays_the_songs_of_an_album(self):
        api = self.use_api(
            albums=[{"Id": "album-1", "Name": "First Album"}],
            album_tracks={
                "album-1": [raw_track("a1"), {"Type": "Folder", "Id": "extras", "Name": "Extras"}, raw_track("a2")]
            },
        )
        state = self.turn_radio_on(mode="album")

        await self.main.advanceTrack(self.guild)
        await self.next_track(2)

        self.assertEqual(api.played_ids(), ["a1", "a2"])
        self.assertEqual(state["buffer"], [])

    async def test_album_picked_for_old_settings_is_not_played_after_a_switch(self):
        api = self.use_api(
            songs=[raw_track("random-1")],
            albums=[{"Id": "album-1", "Name": "First Album"}],
            album_tracks={"album-1": [raw_track("a1"), raw_track("a2")]},
        )
        self.turn_radio_on(mode="album")
        new_state = radio_state(mode="song")
        real_get_album_tracks = api.getAlbumTracks

        async def switch_during_pick(album_id):
            # /radio song arrives while the album is still being fetched
            self.main.radioStates[42] = new_state
            return await real_get_album_tracks(album_id)

        api.getAlbumTracks = switch_during_pick

        await self.main.advanceTrack(self.guild)

        self.assertEqual(api.played_ids(), ["random-1"])
        self.assertEqual(new_state["buffer"], [])
        self.assertIsNone(self.main.playing[42]["Album"])

    async def test_track_queued_during_a_pick_plays_before_the_picked_song(self):
        api = self.use_api(songs=[raw_track("random-1")])
        state = self.turn_radio_on()
        real_get_random_items = api.getRandomItems

        async def queue_during_pick(*args, **kwargs):
            self.main.queues[42] = [queue_entry("queued")]
            return await real_get_random_items(*args, **kwargs)

        api.getRandomItems = queue_during_pick

        await self.main.advanceTrack(self.guild)

        self.assertEqual(self.main.playing[42]["Id"], "queued")
        # the picked song is kept for when the queue is empty again
        self.assertEqual(queue_ids(state["buffer"]), ["random-1"])

    async def test_leaves_voice_once_radio_is_off_and_the_queue_is_empty(self):
        self.use_api()
        self.main.playing[42] = radio_entry("finished")

        await self.main.advanceTrack(self.guild)

        self.assertNotIn(42, self.main.playing)
        self.assertTrue(self.voice_client.disconnect_requested)

    async def test_lost_voice_client_turns_radio_off(self):
        self.use_api(songs=[raw_track("random-1")])
        self.turn_radio_on()
        self.guild.voice_client = None

        await self.main.advanceTrack(self.guild)

        self.assertNotIn(42, self.main.radioStates)
        self.assertEqual(self.voice_client.play_calls, [])

    async def test_being_removed_from_voice_turns_radio_off(self):
        self.turn_radio_on()
        self.main.queues[42] = [queue_entry("queued")]

        self.main.clearGuildState(42)

        self.assertNotIn(42, self.main.radioStates)
        self.assertNotIn(42, self.main.queues)

    async def test_giving_up_on_playback_turns_radio_off_and_keeps_the_queue(self):
        self.use_api(songs=[raw_track("random-1")])
        self.turn_radio_on()
        self.main.queues[42] = [queue_entry(f"queued-{i}") for i in range(5)]
        self.voice_client.play_failures = self.main.MAX_START_ATTEMPTS

        await self.main.advanceTrack(self.guild)

        self.assertTrue(self.voice_client.disconnect_requested)
        self.assertNotIn(42, self.main.radioStates)
        self.assertEqual(queue_ids(self.main.queues[42]), ["queued-3", "queued-4"])

    async def test_voice_outage_is_waited_out_while_radio_is_on(self):
        api = self.use_api(songs=[raw_track("random-1"), raw_track("random-2")])
        self.turn_radio_on()
        await self.main.advanceTrack(self.guild)

        self.voice_client.drop_connection()
        self.voice_client.finish_track()
        await asyncio.sleep(0.05)

        # nothing is queued, but radio would play once the connection is back
        self.assertFalse(self.voice_client.disconnect_requested)
        self.assertIn(42, self.main.radioStates)

        self.voice_client.recover_connection()
        await self.wait_for_plays(2)

        self.assertEqual(api.played_ids(), ["random-1", "random-2"])

    async def test_voice_outage_ends_the_session_once_radio_is_turned_off(self):
        self.use_api(songs=[raw_track("random-1")])
        self.turn_radio_on()
        await self.main.advanceTrack(self.guild)

        self.voice_client.drop_connection()
        self.voice_client.finish_track()
        await asyncio.sleep(0.02)
        self.main.radioStates.pop(42)

        await self.wait_for(lambda: self.voice_client.disconnect_requested)

    async def test_fairplay_queue_works_while_a_radio_track_is_playing(self):
        self.main.config["fairplay"] = True
        self.use_api(songs=[raw_track("random-1")])
        self.turn_radio_on()
        await self.main.advanceTrack(self.guild)

        self.main.queues[42] = {100: [queue_entry("user-100-a")], 200: [queue_entry("user-200-a")]}

        self.assertEqual(queue_ids(self.main.getFlattenedPlayQueue(42)), ["user-100-a", "user-200-a"])

        await self.next_track(2)

        self.assertEqual(self.main.playing[42]["Id"], "user-100-a")
        self.assertEqual(queue_ids(self.main.getFlattenedPlayQueue(42)), ["user-200-a"])

        await self.next_track(3)
        await self.next_track(4)

        self.assertEqual(self.main.playing[42]["Id"], "random-1")


class RadioFailureTests(RadioTestCase):
    async def test_recovers_after_failed_picks_and_forgets_the_failures(self):
        api = self.use_api(songs=[raw_track("random-1")])
        api.fail_random = 2
        state = self.turn_radio_on()

        await self.main.advanceTrack(self.guild)

        self.assertEqual(len(api.random_requests), 3)
        self.assertEqual(self.main.playing[42]["Id"], "random-1")
        self.assertEqual(state["failures"], 0)

    async def test_turns_itself_off_and_leaves_after_too_many_failed_picks(self):
        api = self.use_api(songs=[raw_track("random-1")])
        api.fail_random = 100
        channel = FakeTextChannel()
        self.turn_radio_on(channel=channel)
        self.main.playing[42] = radio_entry("finished")

        await self.main.advanceTrack(self.guild)

        self.assertEqual(len(api.random_requests), self.main.RADIO_MAX_FAILURES)
        self.assertNotIn(42, self.main.radioStates)
        self.assertNotIn(42, self.main.playing)
        self.assertEqual(self.voice_client.play_calls, [])
        self.assertTrue(self.voice_client.disconnect_requested)
        self.assertEqual(channel.sent, ["Radio turned off: too many tracks failed to play in a row"])

    async def test_failed_notice_does_not_keep_the_bot_in_voice(self):
        api = self.use_api()
        api.fail_random = 100
        channel = FakeTextChannel()
        channel.send = mock.AsyncMock(side_effect=RuntimeError("missing permissions"))
        self.turn_radio_on(channel=channel)

        await self.main.advanceTrack(self.guild)

        self.assertNotIn(42, self.main.radioStates)
        self.assertTrue(self.voice_client.disconnect_requested)

    async def test_library_that_became_empty_counts_as_failure(self):
        api = self.use_api()
        self.turn_radio_on()

        await self.main.advanceTrack(self.guild)

        self.assertEqual(len(api.random_requests), self.main.RADIO_MAX_FAILURES)
        self.assertNotIn(42, self.main.radioStates)
        self.assertTrue(self.voice_client.disconnect_requested)

    async def test_album_without_tracks_is_passed_over(self):
        api = self.use_api(
            albums=[{"Id": "empty", "Name": "Empty"}, {"Id": "album-1", "Name": "First Album"}],
            album_tracks={"empty": [], "album-1": [raw_track("a1")]},
        )
        self.turn_radio_on(mode="album")

        await self.main.advanceTrack(self.guild)

        self.assertEqual(api.album_requests, ["empty", "album-1"])
        self.assertEqual(self.main.playing[42]["Id"], "a1")

    async def test_pick_that_hangs_is_given_up_on(self):
        api = self.use_api(songs=[raw_track("random-1")])
        self.turn_radio_on()
        real_get_random_items = api.getRandomItems
        hung = []

        async def hang_once(*args, **kwargs):
            if not hung:
                hung.append(True)
                await asyncio.sleep(3600)
            return await real_get_random_items(*args, **kwargs)

        api.getRandomItems = hang_once

        with mock.patch.object(self.main, "RADIO_FETCH_TIMEOUT", 0.01):
            await self.main.advanceTrack(self.guild)

        self.assertEqual(self.main.playing[42]["Id"], "random-1")

    async def test_radio_track_without_audio_is_dropped_and_another_is_picked(self):
        api = self.use_api(songs=[raw_track("silent-1"), raw_track("good")])
        state = self.turn_radio_on()

        await self.main.advanceTrack(self.guild)

        self.assertEqual(api.played_ids(), ["silent-1", "good"])
        self.assertTrue(self.sources[0].cleaned_up)
        self.assertFalse(self.sources[1].cleaned_up)
        self.assertEqual(len(self.voice_client.play_calls), 1)
        self.assertEqual(self.main.playing[42]["Id"], "good")
        self.assertEqual(state["failures"], 0)

    async def test_radio_stops_when_every_track_comes_without_audio(self):
        api = self.use_api(songs=[raw_track("silent-1")])
        channel = FakeTextChannel()
        self.turn_radio_on(channel=channel)

        await self.main.advanceTrack(self.guild)

        self.assertEqual(len(api.audio_requests), self.main.RADIO_MAX_FAILURES)
        self.assertEqual(self.voice_client.play_calls, [])
        self.assertTrue(all(source.cleaned_up for source in self.sources))
        self.assertNotIn(42, self.main.radioStates)
        self.assertTrue(self.voice_client.disconnect_requested)
        self.assertEqual(len(channel.sent), 1)

    async def test_queued_track_without_audio_still_plays_like_without_radio(self):
        self.use_api()
        state = self.turn_radio_on()
        self.main.queues[42] = [queue_entry("silent-queued")]

        await self.main.advanceTrack(self.guild)

        self.assertEqual(len(self.voice_client.play_calls), 1)
        self.assertEqual(self.main.playing[42]["Id"], "silent-queued")
        self.assertEqual(state["failures"], 0)

    async def test_queued_track_that_fails_to_start_does_not_count_against_radio(self):
        self.use_api(songs=[raw_track("random-1")])
        state = self.turn_radio_on()
        self.main.queues[42] = [queue_entry("queued-broken"), queue_entry("queued-good")]
        self.voice_client.play_failures = 1

        await self.main.advanceTrack(self.guild)

        self.assertEqual(self.main.playing[42]["Id"], "queued-good")
        self.assertEqual(state["failures"], 0)
        self.assertIn(42, self.main.radioStates)

    async def test_waits_longer_after_each_failure(self):
        self.use_api()
        state = self.turn_radio_on()
        clock = [0.0]
        waits = []

        async def fake_sleep(seconds):
            waits.append(seconds)
            clock[0] += seconds

        with mock.patch.object(self.main, "RADIO_BACKOFF_BASE", 2), mock.patch.object(
            self.main, "RADIO_POLL_INTERVAL", 1000
        ), mock.patch.object(self.main.time, "monotonic", lambda: clock[0]), mock.patch.object(
            self.main.asyncio, "sleep", fake_sleep
        ):
            for _ in range(4):
                await self.main.radioFailed(self.guild, state, RuntimeError("boom"))

        self.assertEqual(waits, [2, 4, 8, 16])
        self.assertIn(42, self.main.radioStates)

    async def test_wait_is_capped(self):
        self.use_api()
        state = self.turn_radio_on()
        clock = [0.0]
        waits = []

        async def fake_sleep(seconds):
            waits.append(seconds)
            clock[0] += seconds

        with mock.patch.object(self.main, "RADIO_BACKOFF_BASE", 20), mock.patch.object(
            self.main, "RADIO_POLL_INTERVAL", 1000
        ), mock.patch.object(self.main.time, "monotonic", lambda: clock[0]), mock.patch.object(
            self.main.asyncio, "sleep", fake_sleep
        ):
            await self.main.radioFailed(self.guild, state, RuntimeError("boom"))
            await self.main.radioFailed(self.guild, state, RuntimeError("boom"))

        self.assertEqual(waits, [20, self.main.RADIO_BACKOFF_MAX])

    async def test_wait_ends_early_when_a_track_is_queued(self):
        api = self.use_api(songs=[raw_track("random-1")])
        api.fail_random = 1
        state = self.turn_radio_on()

        with mock.patch.object(self.main, "RADIO_BACKOFF_BASE", 3600):
            advance = asyncio.create_task(self.main.advanceTrack(self.guild))
            await self.wait_for(lambda: state["failures"] == 1)
            self.assertNotIn(42, self.main.playing)
            self.main.queues[42] = [queue_entry("queued")]
            await asyncio.wait_for(advance, timeout=5)

        self.assertEqual(self.main.playing[42]["Id"], "queued")

    async def test_wait_ends_early_when_radio_is_turned_off(self):
        api = self.use_api(songs=[raw_track("random-1")])
        api.fail_random = 1
        state = self.turn_radio_on()

        with mock.patch.object(self.main, "RADIO_BACKOFF_BASE", 3600):
            advance = asyncio.create_task(self.main.advanceTrack(self.guild))
            await self.wait_for(lambda: state["failures"] == 1)
            self.main.radioStates.pop(42)
            await asyncio.wait_for(advance, timeout=5)

        self.assertEqual(self.voice_client.play_calls, [])
        self.assertTrue(self.voice_client.disconnect_requested)

    async def test_pause_and_resume_answer_while_radio_waits_to_try_again(self):
        api = self.use_api(songs=[raw_track("random-1")])
        api.fail_random = 1
        state = self.turn_radio_on()
        ctx = self.ctx()

        with mock.patch.object(self.main, "RADIO_BACKOFF_BASE", 3600):
            advance = asyncio.create_task(self.main.advanceTrack(self.guild))
            await self.wait_for(lambda: state["failures"] == 1)
            await self.main.pause(ctx)
            await self.main.resume(ctx)
            await self.main.nowplaying(ctx)
            self.main.radioStates.pop(42)
            await asyncio.wait_for(advance, timeout=5)

        self.assertEqual(ctx.responses, ["Not Currently Playing", "Not Currently Playing", "Not Currently Playing"])


class RadioWithOtherCommandsTests(RadioTestCase):
    """Existing commands act on a radio track like on any other."""

    async def start_radio(self, **kwargs):
        api = self.use_api(songs=[raw_track("random-1"), raw_track("random-2")], **kwargs)
        self.turn_radio_on()
        await self.main.advanceTrack(self.guild)
        return api

    async def test_queueing_a_track_does_not_cut_the_radio_track(self):
        await self.start_radio()
        ctx = self.ctx()

        await self.main.playHelperGeneric(raw_track("queued-last"), ctx, "last")
        await self.main.playHelperGeneric(raw_track("queued-next"), ctx, "next")
        await self.wait_until_idle()

        self.assertFalse(self.voice_client.stopped)
        self.assertEqual(len(self.voice_client.play_calls), 1)
        self.assertEqual(queue_ids(self.main.queues[42]), ["queued-next", "queued-last"])
        self.assertEqual(self.main.playing[42]["Id"], "random-1")

    async def test_play_now_cuts_the_radio_track_and_radio_stays_on(self):
        api = await self.start_radio()
        ctx = self.ctx()

        await self.main.playHelperGeneric(raw_track("urgent"), ctx, "now")
        await self.wait_for_plays(2)

        self.assertEqual(self.main.playing[42]["Id"], "urgent")
        self.assertIn(42, self.main.radioStates)

        await self.next_track(3)

        self.assertEqual(api.played_ids(), ["random-1", "urgent", "random-2"])

    async def test_skip_moves_on_to_the_next_radio_track(self):
        api = await self.start_radio()
        ctx = self.ctx()

        await self.main.skip(ctx)
        await self.wait_for_plays(2)

        self.assertEqual(api.played_ids(), ["random-1", "random-2"])
        self.assertEqual(self.main.playing[42]["Id"], "random-2")

    async def test_skip_in_album_mode_skips_one_track_not_the_album(self):
        api = self.use_api(
            albums=[{"Id": "album-1", "Name": "First Album"}],
            album_tracks={"album-1": [raw_track("a1"), raw_track("a2"), raw_track("a3")]},
        )
        self.turn_radio_on(mode="album")
        await self.main.advanceTrack(self.guild)

        await self.main.skip(self.ctx())
        await self.wait_for_plays(2)

        self.assertEqual(api.played_ids(), ["a1", "a2"])
        self.assertEqual(len(api.random_requests), 1)

    async def test_pause_and_resume_work_on_a_radio_track(self):
        await self.start_radio()
        ctx = self.ctx()

        await self.main.pause(ctx)
        self.assertTrue(self.voice_client.is_paused())
        await self.main.resume(ctx)

        self.assertTrue(self.voice_client.is_playing())
        self.assertEqual(ctx.responses, ["Pausing playback", "Resuming playback"])

    async def test_stop_turns_radio_off_and_leaves(self):
        api = await self.start_radio()
        self.main.queues[42] = [queue_entry("queued")]
        ctx = self.ctx()

        await self.main.stop(ctx)
        await self.wait_for(lambda: self.voice_client.disconnect_requested)

        self.assertNotIn(42, self.main.radioStates)
        self.assertNotIn(42, self.main.queues)
        self.assertNotIn(42, self.main.playing)
        self.assertEqual(api.played_ids(), ["random-1"])
        self.assertEqual(ctx.responses, ["Stopping playback"])

    async def held_load(self):
        """Make the next track's load wait until the returned event is set."""
        release = threading.Event()
        real_load_audio = self.main.loadAudio

        def load_when_released(url):
            release.wait(5)
            return real_load_audio(url)

        patcher = mock.patch.object(self.main, "loadAudio", load_when_released)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(release.set)
        return release

    async def test_stop_while_a_radio_track_is_loading_drops_that_track(self):
        self.use_api(songs=[raw_track("random-1")])
        self.turn_radio_on()
        release = await self.held_load()
        advance = asyncio.create_task(self.main.advanceTrack(self.guild))
        await self.wait_for(lambda: 42 in self.main.loading)

        await self.main.stop(self.ctx())
        release.set()
        await asyncio.wait_for(advance, timeout=5)

        self.assertEqual(self.voice_client.play_calls, [])
        self.assertNotIn(42, self.main.radioStates)
        self.assertTrue(self.voice_client.disconnect_requested)

    async def test_radio_track_that_loaded_after_radio_was_turned_off_is_not_played(self):
        self.use_api(songs=[raw_track("random-1")])
        self.turn_radio_on()
        release = await self.held_load()
        advance = asyncio.create_task(self.main.advanceTrack(self.guild))
        await self.wait_for(lambda: 42 in self.main.loading)

        await self.main.radio(self.ctx(), "off")
        release.set()
        await asyncio.wait_for(advance, timeout=5)

        self.assertEqual(self.voice_client.play_calls, [])
        self.assertTrue(self.sources[0].cleaned_up)
        self.assertTrue(self.voice_client.disconnect_requested)

    async def test_clear_only_empties_the_queue(self):
        await self.start_radio()
        self.main.queues[42] = [queue_entry("queued")]
        ctx = self.ctx()

        await self.main.clear(ctx)

        self.assertNotIn(42, self.main.queues)
        self.assertIn(42, self.main.radioStates)
        self.assertFalse(self.voice_client.stopped)

    async def test_queue_commands_do_not_see_the_rest_of_a_radio_album(self):
        self.use_api(
            albums=[{"Id": "album-1", "Name": "First Album"}],
            album_tracks={"album-1": [raw_track("a1"), raw_track("a2"), raw_track("a3")]},
        )
        state = self.turn_radio_on(mode="album")
        await self.main.advanceTrack(self.guild)
        ctx = self.ctx()

        await self.main.shuffle(ctx)
        await self.main.remove(ctx, 1)

        self.assertEqual(ctx.responses, ["Playlist is empty", "Queue is empty"])
        self.assertEqual(queue_ids(state["buffer"]), ["a2", "a3"])


class RadioCommandTests(RadioTestCase):
    def playing_something(self):
        """A session with a track playing, so /radio has nothing to start."""
        self.voice_client._playing = True

    async def test_radio_on_stores_the_settings_and_leaves_playback_alone(self):
        api = self.use_api(songs=[raw_track("random-1")], libraries=[{"Id": "lib-1", "Name": "Music"}])
        self.playing_something()
        ctx = self.ctx()

        await self.main.radio(ctx, "song", "lib-1")

        state = self.main.radioStates[42]
        self.assertEqual((state["mode"], state["libraryId"], state["libraryName"]), ("song", "lib-1", "Music"))
        self.assertEqual((state["buffer"], state["album"], state["failures"]), ([], None, 0))
        self.assertIs(state["channel"], ctx.channel)
        self.assertEqual(api.random_requests, [("Audio", "lib-1")])
        self.assertTrue(ctx.deferred)
        self.assertFalse(self.voice_client.stopped)
        self.assertEqual(self.voice_client.play_calls, [])
        self.assertEqual(
            ctx.responses, ["Radio on: playing random songs from the Music library whenever the queue is empty"]
        )

    async def test_radio_on_accepts_a_library_name_typed_by_hand(self):
        self.use_api(albums=[{"Id": "album-1", "Name": "First Album"}], libraries=[{"Id": "lib-1", "Name": "Music"}])
        self.playing_something()

        await self.main.radio(self.ctx(), "album", "music")

        self.assertEqual(self.main.radioStates[42]["libraryId"], "lib-1")
        self.assertEqual(self.main.radioStates[42]["mode"], "album")

    async def test_radio_on_joins_voice_and_starts_playing_when_not_connected(self):
        api = self.use_api(songs=[raw_track("random-1")])
        self.guild.voice_client = None
        channel = FakeChannel(self.guild)
        ctx = self.ctx(channel=channel)

        await self.main.radio(ctx, "song")

        self.assertEqual(len(channel.clients), 1)
        self.assertIs(self.guild.voice_client, channel.clients[0])
        self.assertEqual(len(channel.clients[0].play_calls), 1)
        self.assertEqual(self.main.playing[42]["Id"], "random-1")
        self.assertIsNone(self.main.radioStates[42]["libraryId"])
        # one request to check there is something to play, one for the track
        self.assertEqual(len(api.random_requests), 2)
        self.assertEqual(
            ctx.responses, ["Radio on: playing random songs from all libraries whenever the queue is empty"]
        )

    async def test_radio_on_plays_leftover_queue_first_when_joining(self):
        api = self.use_api(songs=[raw_track("random-1")])
        self.guild.voice_client = None
        self.main.queues[42] = [queue_entry("left-over")]
        channel = FakeChannel(self.guild)

        await self.main.radio(self.ctx(channel=channel), "song")

        self.assertEqual(self.main.playing[42]["Id"], "left-over")
        self.assertIn(42, self.main.radioStates)

    async def test_radio_on_waits_for_the_bot_to_finish_leaving(self):
        self.use_api(songs=[raw_track("random-1")])
        left = self.main.leaving[42] = asyncio.Event()
        channel = FakeChannel(self.guild)
        ctx = self.ctx(channel=channel)

        command = asyncio.create_task(self.main.radio(ctx, "song"))
        await asyncio.sleep(0.02)

        # the old voice client is still there, but it is about to go away
        self.assertNotIn(42, self.main.radioStates)
        self.assertEqual(ctx.responses, [])

        self.guild.voice_client = None
        self.main.leaving.pop(42)
        left.set()
        await asyncio.wait_for(command, timeout=5)

        self.assertEqual(len(channel.clients), 1)
        self.assertEqual(len(channel.clients[0].play_calls), 1)
        self.assertIn(42, self.main.radioStates)

    async def test_radio_is_dropped_when_joining_voice_fails(self):
        self.use_api(songs=[raw_track("random-1")])
        self.guild.voice_client = None
        channel = FakeChannel(self.guild)
        channel.connect = mock.AsyncMock(side_effect=RuntimeError("cannot connect"))

        with self.assertRaises(RuntimeError):
            await self.main.radio(self.ctx(channel=channel), "song")

        self.assertNotIn(42, self.main.radioStates)

    async def test_radio_is_dropped_when_the_user_left_voice_before_it_could_join(self):
        self.use_api(songs=[raw_track("random-1")])
        self.guild.voice_client = None
        channel = FakeChannel(self.guild)
        ctx = self.ctx(channel=channel)
        real_respond = ctx.respond

        async def leave_voice_during_reply(content=None, **kwargs):
            ctx.author.voice = None
            await real_respond(content, **kwargs)

        ctx.respond = leave_voice_during_reply

        await self.main.radio(ctx, "song")

        self.assertEqual(channel.clients, [])
        self.assertNotIn(42, self.main.radioStates)

    async def test_radio_on_requires_a_voice_channel(self):
        self.use_api(songs=[raw_track("random-1")])
        self.guild.voice_client = None
        ctx = self.ctx()

        await self.main.radio(ctx, "song")

        self.assertNotIn(42, self.main.radioStates)
        self.assertEqual(ctx.responses, ["You are not in any voice channel"])

    async def test_radio_on_rejects_an_unknown_library(self):
        api = self.use_api(songs=[raw_track("random-1")], libraries=[{"Id": "lib-1", "Name": "Music"}])
        self.playing_something()
        ctx = self.ctx()

        await self.main.radio(ctx, "song", "nope")

        self.assertNotIn(42, self.main.radioStates)
        self.assertEqual(api.random_requests, [])
        self.assertEqual(ctx.responses, ["Music library not found, pick one from the suggestions"])

    async def test_radio_on_rejects_a_library_with_nothing_to_play(self):
        self.use_api()
        self.playing_something()
        ctx = self.ctx()

        await self.main.radio(ctx, "album")

        self.assertNotIn(42, self.main.radioStates)
        self.assertEqual(ctx.responses, ["No albums found in all libraries"])

    async def test_radio_on_reports_an_unreachable_server(self):
        api = self.use_api(songs=[raw_track("random-1")])
        api.fail_random = 1
        self.playing_something()
        ctx = self.ctx()

        with self.assertLogs("jellychord", level="ERROR"):
            await self.main.radio(ctx, "song")

        self.assertNotIn(42, self.main.radioStates)
        self.assertEqual(ctx.responses, [self.main.JELLYFIN_ERROR_MESSAGE])

    async def test_radio_on_reports_an_unreachable_server_when_looking_up_the_library(self):
        api = self.use_api(songs=[raw_track("random-1")])
        api.fail_libraries = True
        self.playing_something()
        ctx = self.ctx()

        with self.assertLogs("jellychord", level="ERROR"):
            await self.main.radio(ctx, "song", "Music")

        self.assertNotIn(42, self.main.radioStates)
        self.assertEqual(ctx.responses, [self.main.JELLYFIN_ERROR_MESSAGE])

    async def test_radio_on_again_with_the_same_settings_keeps_the_album(self):
        self.use_api(albums=[{"Id": "album-1", "Name": "First Album"}])
        self.playing_something()
        state = self.turn_radio_on(mode="album", buffer=[radio_entry("a2", "First Album")], album="First Album")
        ctx = self.ctx()

        await self.main.radio(ctx, "album")

        self.assertIs(self.main.radioStates[42], state)
        self.assertEqual(queue_ids(state["buffer"]), ["a2"])
        self.assertEqual(ctx.responses, ["Radio is already playing random albums from all libraries"])

    async def test_switching_mode_takes_effect_after_the_current_track_and_drops_the_album(self):
        api = self.use_api(
            songs=[raw_track("random-1")],
            albums=[{"Id": "album-1", "Name": "First Album"}],
            album_tracks={"album-1": [raw_track("a1"), raw_track("a2")]},
        )
        self.turn_radio_on(mode="album")
        await self.main.advanceTrack(self.guild)

        await self.main.radio(self.ctx(), "song")

        self.assertFalse(self.voice_client.stopped)
        self.assertEqual(self.main.playing[42]["Id"], "a1")
        self.assertEqual(self.main.radioStates[42]["mode"], "song")
        self.assertEqual(self.main.radioStates[42]["buffer"], [])

        await self.next_track(2)

        self.assertEqual(api.played_ids(), ["a1", "random-1"])

    async def test_switching_library_drops_the_album_too(self):
        self.use_api(
            albums=[{"Id": "album-1", "Name": "First Album"}],
            libraries=[{"Id": "lib-1", "Name": "Music"}, {"Id": "lib-2", "Name": "Soundtracks"}],
        )
        self.playing_something()
        old = self.turn_radio_on(mode="album", library_id="lib-1", buffer=[radio_entry("a2", "First Album")],
                                 album="First Album")

        await self.main.radio(self.ctx(), "album", "lib-2")

        self.assertIsNot(self.main.radioStates[42], old)
        self.assertEqual(self.main.radioStates[42]["libraryId"], "lib-2")
        self.assertEqual(self.main.radioStates[42]["buffer"], [])

    async def test_radio_off_lets_the_current_track_and_the_queue_finish(self):
        api = self.use_api(songs=[raw_track("random-1")])
        self.turn_radio_on()
        await self.main.advanceTrack(self.guild)
        self.main.queues[42] = [queue_entry("queued")]
        ctx = self.ctx()

        await self.main.radio(ctx, "off")

        self.assertNotIn(42, self.main.radioStates)
        self.assertFalse(self.voice_client.stopped)
        self.assertFalse(ctx.deferred)
        self.assertEqual(self.main.playing[42]["Id"], "random-1")
        self.assertEqual(
            ctx.responses, ["Radio turned off, playback ends after the current track and the queue"]
        )

        await self.next_track(2)
        self.assertEqual(self.main.playing[42]["Id"], "queued")

        self.voice_client.finish_track()
        await self.wait_for(lambda: self.voice_client.disconnect_requested)
        self.assertEqual(api.played_ids(), ["random-1", "queued"])

    async def test_radio_off_when_it_is_not_on(self):
        ctx = self.ctx()

        await self.main.radio(ctx, "off")

        self.assertEqual(ctx.responses, ["Radio is not on"])

    async def test_library_suggestions_follow_the_typed_text_and_are_cached(self):
        api = self.use_api(libraries=[{"Id": "lib-1", "Name": "Music"}, {"Id": "lib-2", "Name": "Soundtracks"}])

        everything = await self.main.libraryAutocomplete(types.SimpleNamespace(value=""))
        filtered = await self.main.libraryAutocomplete(types.SimpleNamespace(value="SOUND"))

        self.assertEqual(
            everything,
            [{"name": "Music", "value": "lib-1"}, {"name": "Soundtracks", "value": "lib-2"}],
        )
        self.assertEqual(filtered, [{"name": "Soundtracks", "value": "lib-2"}])
        self.assertEqual(api.library_requests, 1)

    async def test_library_suggestions_are_empty_when_the_server_is_unreachable(self):
        api = self.use_api()
        api.fail_libraries = True

        with self.assertLogs("jellychord", level="WARNING"):
            suggestions = await self.main.libraryAutocomplete(types.SimpleNamespace(value=""))

        self.assertEqual(suggestions, [])

    async def test_library_list_is_asked_for_again_once_the_cache_is_old(self):
        api = self.use_api(libraries=[{"Id": "lib-1", "Name": "Music"}])
        clock = [1000.0]

        with mock.patch.object(self.main.time, "monotonic", lambda: clock[0]):
            await self.main.getMusicLibraries()
            clock[0] += self.main.LIBRARY_CACHE_SECS - 1
            await self.main.getMusicLibraries()
            self.assertEqual(api.library_requests, 1)
            clock[0] += 2
            await self.main.getMusicLibraries()

        self.assertEqual(api.library_requests, 2)


class RadioDisplayTests(RadioTestCase):
    async def test_queue_shows_what_radio_will_do_under_the_tracks(self):
        self.main.queues[42] = [queue_entry("first"), queue_entry("second")]
        self.turn_radio_on()
        ctx = self.ctx()
        respond = ctx.respond = mock.AsyncMock()

        await self.main.queue(ctx)

        self.assertEqual(
            respond.await_args.args[0],
            "Tracks in playlist:\n1. Artist - first\n2. Artist - second\nPage: 1/1\n"
            "Radio is on: random songs play when the queue is empty",
        )

    async def test_queue_is_unchanged_without_radio(self):
        self.main.queues[42] = [queue_entry("first")]
        ctx = self.ctx()
        respond = ctx.respond = mock.AsyncMock()

        await self.main.queue(ctx)

        self.assertEqual(respond.await_args.args[0], "Tracks in playlist:\n1. Artist - first\nPage: 1/1")

    async def test_empty_queue_shows_what_radio_will_do(self):
        self.turn_radio_on()
        ctx = self.ctx()

        await self.main.queue(ctx)

        self.assertEqual(ctx.responses, ["Empty Queue\nRadio is on: random songs play when the queue is empty"])

    async def test_paging_the_queue_keeps_the_radio_line(self):
        self.main.queues[42] = [queue_entry(f"track-{i}") for i in range(1, 23)]
        self.turn_radio_on()
        view = self.main.listView(100, 2)
        interaction = make_interaction(guild_id=42)

        await view.showPage(interaction, 1)

        self.assertEqual(
            interaction.response.edit_message.await_args.kwargs["content"],
            "Tracks in playlist:\n21. Artist - track-21\n22. Artist - track-22\nPage: 2/2\n"
            "Radio is on: random songs play when the queue is empty",
        )

    def test_radio_line_names_the_album_that_will_continue(self):
        self.assertEqual(self.main.getRadioStatusLine(42), "")

        self.turn_radio_on(mode="album")
        self.assertEqual(
            self.main.getRadioStatusLine(42),
            "Radio is on: random albums play when the queue is empty",
        )

        self.turn_radio_on(mode="album", buffer=[radio_entry("a2", "First Album")], album="First Album")
        self.assertEqual(
            self.main.getRadioStatusLine(42),
            "Radio is on: continuing album First Album when the queue is empty",
        )

    def test_radio_line_shortens_a_very_long_album_name(self):
        name = "A" * 150
        self.turn_radio_on(mode="album", buffer=[radio_entry("a2", name)], album=name)

        self.assertEqual(
            self.main.getRadioStatusLine(42),
            f"Radio is on: continuing album {'A' * 97}... when the queue is empty",
        )

    async def test_nowplaying_marks_radio_tracks(self):
        self.use_api(
            songs=[raw_track("random-1")],
            albums=[{"Id": "album-1", "Name": "First Album"}],
            album_tracks={"album-1": [raw_track("a1")]},
        )
        self.turn_radio_on(mode="album")
        await self.main.advanceTrack(self.guild)
        ctx = self.ctx()
        await self.main.pause(ctx)
        self.main.playing[42]["playtime-offset"] = self.main.datetime.timedelta(seconds=5)

        await self.main.nowplaying(ctx)

        self.assertEqual(ctx.responses[-1], "Currently Playing: Artist - a1 00:05/02:00 [Radio: First Album]")

        self.turn_radio_on()
        await self.next_track(2)
        await self.main.pause(ctx)
        self.main.playing[42]["playtime-offset"] = self.main.datetime.timedelta(seconds=5)

        await self.main.nowplaying(ctx)

        self.assertEqual(ctx.responses[-1], "Currently Playing: Artist - random-1 00:05/02:00 [Radio]")


if __name__ == "__main__":
    unittest.main()
