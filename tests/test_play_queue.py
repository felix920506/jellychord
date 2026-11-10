import asyncio
import math
import threading
import types
import unittest
from unittest import mock

from tests.test_main_helpers import import_main_with_stubs


def raw_track(track_id, name=None, runtime_ticks=120_000_0000):
    return {
        "Type": "Audio",
        "Artists": ["Artist"],
        "Name": name or track_id,
        "Id": track_id,
        "RunTimeTicks": runtime_ticks,
    }


def queue_entry(track_id, name=None, length=120):
    return {
        "Artists": ["Artist"],
        "Name": name or track_id,
        "Id": track_id,
        "Length": length,
    }


def queue_ids(queue):
    return [item["Id"] for item in queue]


class FakeJFAPI:
    def __init__(self, album_tracks=None):
        self.album_tracks = album_tracks or {}
        self.album_requests = []
        self.audio_requests = []

    async def getAlbumTracks(self, album_id):
        self.album_requests.append(album_id)
        return self.album_tracks[album_id]

    def getAudioHls(self, track_id, bitrate):
        self.audio_requests.append((track_id, bitrate))
        return f"https://stream.example/{track_id}?bitrate={bitrate}"


class FakeVoiceClient:
    """Mimics the parts of py-cord's VoiceClient the player uses. play()
    records the after= callback instead of starting a player thread. The
    first play_failures calls to play() raise. The first half_start_failures
    calls raise after storing the player, like py-cord 2.8.1 when the player
    thread fails to start: is_playing() is True, but after= is never called."""

    def __init__(self, bitrate=96_000, play_failures=0, half_start_failures=0):
        self.channel = types.SimpleNamespace(bitrate=bitrate)
        self.play_calls = []
        self.failed_plays = []
        self.play_failures = play_failures
        self.half_start_failures = half_start_failures
        self._half_started = False
        self.stopped = False
        self.connected = True
        self.disconnect_requested = False
        self._playing = False
        self._paused = False

    def is_connected(self):
        return self.connected

    def is_playing(self):
        return self._playing and not self._paused

    def is_paused(self):
        return self._playing and self._paused

    def play(self, audio, after):
        if self._playing:
            raise RuntimeError("Already playing audio.")
        if self.play_failures:
            self.play_failures -= 1
            self.failed_plays.append(audio)
            raise RuntimeError("voice play failed")
        if self.half_start_failures:
            self.half_start_failures -= 1
            self.failed_plays.append(audio)
            self._playing = True
            self._half_started = True
            raise RuntimeError("can't start new thread")
        self._playing = True
        self._paused = False
        self.play_calls.append((audio, after))

    def pause(self):
        if self._playing:
            self._paused = True

    def resume(self):
        self._paused = False

    def stop(self):
        self.stopped = True
        was_playing = self._playing and not self._half_started
        self._playing = False
        self._paused = False
        self._half_started = False
        if was_playing:
            # py-cord's player thread calls after= once it notices the stop
            self._call_after()

    def finish_track(self):
        """End the current track naturally."""
        self._playing = False
        self._paused = False
        self._call_after()

    def _call_after(self):
        # Like py-cord, run the callback on a thread other than the event loop's.
        _, after = self.play_calls[-1]
        thread = threading.Thread(target=after, args=(None,))
        thread.start()
        thread.join()

    async def disconnect(self, force=False):
        self.disconnect_requested = True
        self.connected = False


class FakeContext:
    def __init__(self, guild_id=42, author_id=100, voice_client=None):
        self.guild_id = guild_id
        self.author = types.SimpleNamespace(id=author_id, voice=None)
        self.voice_client = voice_client
        self.guild = types.SimpleNamespace(id=guild_id, voice_client=voice_client)
        self.responses = []

    async def respond(self, content=None, **kwargs):
        self.responses.append(content)


class PlayQueueTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = import_main_with_stubs()

    def setUp(self):
        self.main.queues.clear()
        self.main.playing.clear()
        self.main.loading.clear()
        self.main.playerLocks.clear()
        self.main.config["fairplay"] = False
        self.main.JF_APICLIENT = FakeJFAPI()

    async def test_play_helper_appends_regular_track_to_existing_queue(self):
        voice_client = FakeVoiceClient()
        ctx = FakeContext(voice_client=voice_client)
        self.main.queues[42] = [queue_entry("existing")]

        await self.main.playHelperGeneric(raw_track("new"), ctx, "last")

        self.assertEqual(queue_ids(self.main.queues[42]), ["existing", "new"])
        self.assertFalse(voice_client.stopped)

    async def test_play_helper_inserts_album_tracks_at_front_for_next(self):
        voice_client = FakeVoiceClient()
        ctx = FakeContext(voice_client=voice_client)
        self.main.queues[42] = [queue_entry("existing")]
        self.main.JF_APICLIENT = FakeJFAPI(
            {
                "album-1": [
                    raw_track("album-track-1", runtime_ticks=30_000_0000),
                    raw_track("album-track-2", runtime_ticks=45_000_0000),
                ]
            }
        )

        await self.main.playHelperGeneric({"Type": "MusicAlbum", "Id": "album-1"}, ctx, "next")

        self.assertEqual(self.main.JF_APICLIENT.album_requests, ["album-1"])
        self.assertEqual(queue_ids(self.main.queues[42]), ["album-track-1", "album-track-2", "existing"])
        self.assertEqual([item["Length"] for item in self.main.queues[42][:2]], [30, 45])
        self.assertFalse(voice_client.stopped)

    async def test_play_helper_play_now_inserts_front_and_stops_current_voice_client(self):
        voice_client = FakeVoiceClient()
        ctx = FakeContext(voice_client=voice_client)
        self.main.queues[42] = [queue_entry("existing")]

        await self.main.playHelperGeneric(raw_track("urgent"), ctx, "now")

        self.assertEqual(queue_ids(self.main.queues[42]), ["urgent", "existing"])
        self.assertTrue(voice_client.stopped)

    async def test_play_helper_fairplay_keeps_tracks_in_per_user_queues(self):
        self.main.config["fairplay"] = True

        await self.main.playHelperGeneric(raw_track("user-100-a"), FakeContext(author_id=100, voice_client=FakeVoiceClient()), "last")
        await self.main.playHelperGeneric(raw_track("user-200-a"), FakeContext(author_id=200, voice_client=FakeVoiceClient()), "last")
        await self.main.playHelperGeneric(raw_track("user-100-b"), FakeContext(author_id=100, voice_client=FakeVoiceClient()), "next")

        self.assertEqual(
            {user_id: queue_ids(queue) for user_id, queue in self.main.queues[42].items()},
            {
                100: ["user-100-b", "user-100-a"],
                200: ["user-200-a"],
            },
        )

    async def test_advance_track_regular_queue_starts_first_track_and_preserves_tail(self):
        voice_client = FakeVoiceClient(bitrate=128_000)
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = [queue_entry("first"), queue_entry("second")]

        await self.main.advanceTrack(guild)

        self.assertEqual(self.main.playing[42]["Id"], "first")
        self.assertEqual(queue_ids(self.main.queues[42]), ["second"])
        self.assertEqual(self.main.JF_APICLIENT.audio_requests, [("first", 128_000)])
        self.assertEqual(len(voice_client.play_calls), 1)
        audio, after = voice_client.play_calls[0]
        self.assertEqual(audio.url, "https://stream.example/first?bitrate=128000")
        self.assertEqual(audio.kwargs, {"codec": "copy"})
        self.assertTrue(audio.read_called)
        self.assertTrue(callable(after))
        self.assertFalse(self.main.playing[42]["paused"])
        self.assertNotIn(42, self.main.loading)

    async def test_advance_track_regular_queue_removes_guild_queue_after_last_track(self):
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = [queue_entry("only")]

        await self.main.advanceTrack(guild)

        self.assertEqual(self.main.playing[42]["Id"], "only")
        self.assertNotIn(42, self.main.queues)
        self.assertEqual(len(voice_client.play_calls), 1)

    async def test_advance_track_fairplay_rotates_users_and_removes_empty_user_queue(self):
        self.main.config["fairplay"] = True
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = {
            100: [queue_entry("user-100-a")],
            200: [queue_entry("user-200-a"), queue_entry("user-200-b")],
        }
        self.main.playing[42] = {"nextTrackUserIdx": 0}

        await self.main.advanceTrack(guild)

        self.assertEqual(self.main.playing[42]["Id"], "user-100-a")
        self.assertEqual(self.main.playing[42]["nextTrackUserIdx"], 0)
        self.assertEqual(
            {user_id: queue_ids(queue) for user_id, queue in self.main.queues[42].items()},
            {200: ["user-200-a", "user-200-b"]},
        )
        self.assertEqual(self.main.getFlattenedPlayQueue(42), [queue_entry("user-200-a"), queue_entry("user-200-b")])
        self.assertEqual(self.main.JF_APICLIENT.audio_requests, [("user-100-a", 96_000)])
        self.assertEqual(len(voice_client.play_calls), 1)

    def _drain_fairplay_queue(self, user_queues):
        self.main.config["fairplay"] = True
        self.main.queues[42] = {
            user_id: [queue_entry(track_id) for track_id in track_ids]
            for user_id, track_ids in user_queues.items()
        }

        played, previews = [], []
        while 42 in self.main.queues:
            played.append(self.main.popNextEntry(42)["Id"])
            previews.append(queue_ids(self.main.getFlattenedPlayQueue(42)))
        return played, previews

    def test_fairplay_rotation_when_a_user_runs_out(self):
        cases = {
            "first": ({100: ["a1"], 200: ["b1", "b2"], 300: ["c1", "c2"]}, ["a1", "b1", "c1", "b2", "c2"]),
            "middle": ({100: ["a1", "a2"], 200: ["b1"], 300: ["c1", "c2"]}, ["a1", "b1", "c1", "a2", "c2"]),
            "last": ({100: ["a1", "a2"], 200: ["b1", "b2"], 300: ["c1"]}, ["a1", "b1", "c1", "a2", "b2"]),
        }
        for position, (user_queues, expected) in cases.items():
            with self.subTest(user_running_out=position):
                self.main.queues.clear()
                self.main.playing.clear()

                played, previews = self._drain_fairplay_queue(user_queues)

                self.assertEqual(played, expected)
                # the /queue preview after each track must match what actually plays next
                for i, preview in enumerate(previews):
                    self.assertEqual(preview, expected[i + 1:])

    async def test_advance_track_fairplay_last_user_running_out_resets_to_first_user(self):
        self.main.config["fairplay"] = True
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = {
            100: [queue_entry("user-100-a")],
            200: [queue_entry("user-200-a")],
        }
        self.main.playing[42] = {"nextTrackUserIdx": 1}

        await self.main.advanceTrack(guild)

        self.assertEqual(self.main.playing[42]["Id"], "user-200-a")
        self.assertEqual(self.main.playing[42]["nextTrackUserIdx"], 0)

    async def test_advance_track_disconnects_when_queue_is_empty(self):
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.playing[42] = queue_entry("finished")

        await self.main.advanceTrack(guild)

        self.assertNotIn(42, self.main.playing)
        self.assertTrue(voice_client.disconnect_requested)
        self.assertEqual(voice_client.play_calls, [])

    async def test_track_end_on_player_thread_advances_queue_on_event_loop(self):
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = [queue_entry("first"), queue_entry("second")]
        await self.main.advanceTrack(guild)

        loop_thread = threading.get_ident()
        state_threads = []
        real_pop_next_entry = self.main.popNextEntry

        def recording_pop_next_entry(guild_id):
            state_threads.append(threading.get_ident())
            return real_pop_next_entry(guild_id)

        with mock.patch.object(self.main, "popNextEntry", recording_pop_next_entry):
            # The after= callback must only hand off, not change the queue itself.
            voice_client.finish_track()
            self.assertEqual(queue_ids(self.main.queues[42]), ["second"])
            self.assertEqual(self.main.playing[42]["Id"], "first")

            await self.wait_for(lambda: len(voice_client.play_calls) == 2)

        self.assertEqual(state_threads, [loop_thread])
        self.assertEqual(self.main.playing[42]["Id"], "second")
        self.assertNotIn(42, self.main.queues)

    async def test_skip_while_next_track_is_loading_drops_that_track(self):
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = [queue_entry("first"), queue_entry("second")]

        with self.blocking_load() as load:
            task = asyncio.create_task(self.main.advanceTrack(guild))
            await self.wait_for(lambda: len(load.loads) == 1)
            self.main.skipCurrentTrack(guild)
            load.finish()
            await task

        self.assertTrue(load.loads[0].cleaned_up)
        self.assertEqual([audio.url for audio, _ in voice_client.play_calls], [load.loads[1].url])
        self.assertEqual(self.main.playing[42]["Id"], "second")

    async def test_stop_while_track_is_loading_plays_nothing_and_disconnects(self):
        voice_client = FakeVoiceClient()
        ctx = FakeContext(voice_client=voice_client)
        self.main.queues[42] = [queue_entry("first"), queue_entry("second")]

        with self.blocking_load() as load:
            task = asyncio.create_task(self.main.advanceTrack(ctx.guild))
            await self.wait_for(lambda: len(load.loads) == 1)
            ctx.respond = mock.AsyncMock()
            await self.main.stop(ctx)
            load.finish()
            await task

        self.assertTrue(load.loads[0].cleaned_up)
        self.assertEqual(voice_client.play_calls, [])
        self.assertTrue(voice_client.disconnect_requested)
        self.assertNotIn(42, self.main.queues)
        self.assertNotIn(42, self.main.playing)

    async def test_pause_while_track_is_loading_starts_it_paused(self):
        voice_client = FakeVoiceClient()
        ctx = FakeContext(voice_client=voice_client)
        self.main.queues[42] = [queue_entry("first")]

        with self.blocking_load() as load:
            task = asyncio.create_task(self.main.advanceTrack(ctx.guild))
            await self.wait_for(lambda: len(load.loads) == 1)
            ctx.respond = mock.AsyncMock()
            await self.main.pause(ctx)
            load.finish()
            await task

        self.assertEqual(len(voice_client.play_calls), 1)
        self.assertTrue(voice_client.is_paused())
        self.assertTrue(self.main.playing[42]["paused"])

    async def test_failed_track_load_moves_on_to_next_track(self):
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = [queue_entry("broken"), queue_entry("good")]
        real_load_audio = self.main.loadAudio

        def flaky_load_audio(url):
            if "broken" in url:
                raise OSError("ffmpeg failed")
            return real_load_audio(url)

        with mock.patch.object(self.main, "loadAudio", flaky_load_audio), self.assertLogs("jellychord", "ERROR"):
            await self.main.advanceTrack(guild)

        self.assertEqual(self.main.playing[42]["Id"], "good")
        self.assertEqual(len(voice_client.play_calls), 1)

    async def test_concurrent_advances_start_only_one_track(self):
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = [queue_entry("first"), queue_entry("second")]

        await asyncio.gather(self.main.advanceTrack(guild), self.main.advanceTrack(guild))

        self.assertEqual(len(voice_client.play_calls), 1)
        self.assertEqual(queue_ids(self.main.queues[42]), ["second"])

    async def test_pause_and_resume_while_disconnecting_report_not_playing(self):
        voice_client = FakeVoiceClient()
        ctx = FakeContext(voice_client=voice_client)
        ctx.respond = mock.AsyncMock()

        await self.main.pause(ctx)
        await self.main.resume(ctx)

        self.assertEqual(
            [call.args[0] for call in ctx.respond.await_args_list],
            ["Not Currently Playing", "Not Currently Playing"],
        )

    async def test_failed_first_read_cleans_up_ffmpeg(self):
        created = []
        real_audio_class = self.main.discord.FFmpegOpusAudio

        class FailingAudio(real_audio_class):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                created.append(self)

            def read(self):
                raise OSError("stream unavailable")

        with mock.patch.object(self.main.discord, "FFmpegOpusAudio", FailingAudio):
            with self.assertRaises(OSError):
                self.main.loadAudio("https://stream.example/broken")

        self.assertTrue(created[0].cleaned_up)

    # A track can end, and advanceTrack run, while a command awaits its reply.
    # These replay that interleaving: "first" ends during ctx.respond().

    async def test_skip_when_track_ends_during_reply_skips_only_once(self):
        voice_client, ctx = await self.start_playing(["first", "second", "third"])

        await self.main.skip(ctx)

        self.assertEqual(self.main.playing[42]["Id"], "second")
        self.assertTrue(voice_client.is_playing())

    async def test_playnow_when_track_ends_during_reply_plays_requested_track(self):
        voice_client, ctx = await self.start_playing(["first", "second", "third"])

        await self.main.playnow(ctx, 2)

        self.assertEqual(self.main.playing[42]["Id"], "third")
        self.assertTrue(voice_client.is_playing())
        self.assertEqual(queue_ids(self.main.queues[42]), ["second"])

    async def test_pause_when_last_track_ends_during_reply(self):
        voice_client, ctx = await self.start_playing(["first"])

        await self.main.pause(ctx)

        ctx.respond.assert_awaited_once_with("Pausing playback")
        self.assertTrue(voice_client.disconnect_requested)

    async def test_resume_when_last_track_ends_during_reply(self):
        voice_client, ctx = await self.start_playing(["first"])
        self.main.playing[42]["paused"] = True
        voice_client.pause()

        await self.main.resume(ctx)

        ctx.respond.assert_awaited_once_with("Resuming playback")
        self.assertTrue(voice_client.disconnect_requested)

    async def test_shuffle_when_last_queued_track_starts_during_reply(self):
        voice_client, ctx = await self.start_playing(["first", "second"])

        await self.main.shuffle(ctx)

        ctx.respond.assert_awaited_once_with("Shuffling playlist")
        self.assertEqual(self.main.playing[42]["Id"], "second")

    async def test_clear_when_track_ends_during_reply_plays_nothing_more(self):
        voice_client, ctx = await self.start_playing(["first", "second"])

        await self.main.clear(ctx)

        ctx.respond.assert_awaited_once_with("Playlist cleared")
        self.assertEqual(len(voice_client.play_calls), 1)
        self.assertTrue(voice_client.disconnect_requested)

    async def start_playing(self, track_ids):
        """Start playing track_ids[0] with the rest queued. Returns a voice
        client and a context whose reply lets "first" end mid-await."""
        voice_client = FakeVoiceClient()
        ctx = FakeContext(voice_client=voice_client)
        self.main.queues[42] = [queue_entry(track_id) for track_id in track_ids]
        await self.main.advanceTrack(ctx.guild)

        async def first_track_ends_during_reply(*args, **kwargs):
            if voice_client._playing and self.main.playing.get(42, {}).get("Id") == "first":
                voice_client.finish_track()
            await self.wait_for(lambda: voice_client.is_playing() or voice_client.disconnect_requested)

        ctx.respond = mock.AsyncMock(side_effect=first_track_ends_during_reply)
        return voice_client, ctx

    async def wait_for(self, predicate, timeout=5):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.01)

    def blocking_load(self):
        """Patch loadAudio so the first load blocks until the event loop has
        had a chance to run a command, like a slow ffmpeg start. Call
        release() to let it finish."""
        test = self
        real_load_audio = self.main.loadAudio

        class BlockingLoad:
            def __enter__(self):
                self.loads = []
                self.release = threading.Event()
                self.patch = mock.patch.object(test.main, "loadAudio", self.load)
                self.patch.start()
                return self

            def load(self, url):
                audio = real_load_audio(url)
                self.loads.append(audio)
                if len(self.loads) == 1:
                    self.release.wait(5)
                return audio

            def finish(self):
                self.release.set()

            def __exit__(self, *exc):
                self.release.set()
                self.patch.stop()

        return BlockingLoad()



class AdvanceTrackFailureTests(unittest.IsolatedAsyncioTestCase):
    """advanceTrack also runs from py-cord's after= callback, where nothing
    awaits it: a failure must be logged, and must not leave the bot idle in
    the voice channel."""

    @classmethod
    def setUpClass(cls):
        cls.main = import_main_with_stubs()

    def setUp(self):
        self.main.queues.clear()
        self.main.playing.clear()
        self.main.loading.clear()
        self.main.playerLocks.clear()
        self.main.config["fairplay"] = False
        self.main.JF_APICLIENT = FakeJFAPI()

    async def test_playback_error_is_logged_and_next_track_starts(self):
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = [queue_entry("next")]

        with self.assertLogs("jellychord", "ERROR") as logs:
            await self.main.advanceTrack(guild, RuntimeError("stream broke"))

        self.assertIn("Playback error in guild 42", logs.output[0])
        self.assertIn("RuntimeError: stream broke", logs.output[0])
        self.assertEqual(self.main.playing[42]["Id"], "next")
        self.assertEqual(len(voice_client.play_calls), 1)

    async def test_failed_track_is_logged_and_skipped(self):
        voice_client = FakeVoiceClient(play_failures=1)
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = [queue_entry("broken"), queue_entry("good")]

        with self.assertLogs("jellychord", "ERROR") as logs:
            await self.main.advanceTrack(guild)

        self.assertIn("Failed to start the next track in guild 42", logs.output[0])
        self.assertEqual([audio.url for audio in voice_client.failed_plays], ["https://stream.example/broken?bitrate=96000"])
        self.assertTrue(voice_client.failed_plays[0].cleaned_up)
        self.assertEqual(self.main.playing[42]["Id"], "good")
        self.assertEqual(len(voice_client.play_calls), 1)
        self.assertNotIn(42, self.main.queues)
        self.assertFalse(voice_client.disconnect_requested)

    async def test_gives_up_and_disconnects_after_repeated_failures(self):
        attempts = self.main.MAX_START_ATTEMPTS
        voice_client = FakeVoiceClient(play_failures=attempts)
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = [queue_entry(f"broken-{i}") for i in range(attempts)] + [queue_entry("untouched")]

        with self.assertLogs("jellychord", "ERROR") as logs:
            await self.main.advanceTrack(guild)

        self.assertIn(f"Giving up on playback in guild 42 after {attempts} failed attempts", logs.output[-1])
        self.assertEqual(len(voice_client.failed_plays), attempts)
        self.assertTrue(all(audio.cleaned_up for audio in voice_client.failed_plays))
        self.assertEqual(queue_ids(self.main.queues[42]), ["untouched"])
        self.assertNotIn(42, self.main.playing)
        self.assertTrue(voice_client.disconnect_requested)

    async def test_load_failures_count_towards_giving_up(self):
        attempts = self.main.MAX_START_ATTEMPTS
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = [queue_entry(f"broken-{i}") for i in range(attempts)] + [queue_entry("untouched")]

        def failing_load_audio(url):
            raise OSError("ffmpeg failed")

        with mock.patch.object(self.main, "loadAudio", failing_load_audio), self.assertLogs("jellychord", "ERROR"):
            await self.main.advanceTrack(guild)

        self.assertEqual(queue_ids(self.main.queues[42]), ["untouched"])
        self.assertEqual(voice_client.play_calls, [])
        self.assertTrue(voice_client.disconnect_requested)

    async def test_fairplay_queue_is_still_listable_after_giving_up(self):
        self.main.config["fairplay"] = True
        attempts = self.main.MAX_START_ATTEMPTS
        voice_client = FakeVoiceClient(play_failures=attempts)
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = {
            100: [queue_entry(f"user-100-{i}") for i in range(attempts)],
            200: [queue_entry("user-200-a")],
        }

        with self.assertLogs("jellychord", "ERROR"):
            await self.main.advanceTrack(guild)

        self.assertNotIn(42, self.main.playing)
        # fairplay alternates users between attempts: 100-0, 200-a, 100-1
        self.assertEqual(queue_ids(self.main.getFlattenedPlayQueue(42)), ["user-100-2"])

    async def test_does_nothing_when_a_track_is_already_playing(self):
        voice_client = FakeVoiceClient()
        voice_client._playing = True
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = [queue_entry("queued"), queue_entry("kept")]

        await self.main.advanceTrack(guild)

        self.assertEqual(voice_client.play_calls, [])
        self.assertEqual(queue_ids(self.main.queues[42]), ["queued", "kept"])
        self.assertFalse(voice_client.disconnect_requested)

    async def test_failure_when_queue_runs_out_disconnects(self):
        voice_client = FakeVoiceClient(play_failures=1)
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = [queue_entry("broken")]

        with self.assertLogs("jellychord", "ERROR"):
            await self.main.advanceTrack(guild)

        self.assertNotIn(42, self.main.playing)
        self.assertTrue(voice_client.disconnect_requested)

    async def test_missing_voice_client_clears_now_playing_without_raising(self):
        guild = types.SimpleNamespace(id=42, voice_client=None)
        self.main.queues[42] = [queue_entry("queued")]
        self.main.playing[42] = queue_entry("finished")

        await self.main.advanceTrack(guild)

        self.assertNotIn(42, self.main.playing)
        self.assertEqual(queue_ids(self.main.queues[42]), ["queued"])

    async def test_empty_guild_queue_disconnects_instead_of_raising(self):
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = []

        await self.main.advanceTrack(guild)

        self.assertNotIn(42, self.main.queues)
        self.assertEqual(voice_client.play_calls, [])
        self.assertTrue(voice_client.disconnect_requested)

    async def test_fairplay_skips_empty_user_queue(self):
        self.main.config["fairplay"] = True
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = {100: [], 200: [queue_entry("user-200-a")]}

        await self.main.advanceTrack(guild)

        self.assertEqual(self.main.playing[42]["Id"], "user-200-a")
        self.assertEqual(len(voice_client.play_calls), 1)

    async def test_half_started_player_is_cleared_and_next_track_plays(self):
        voice_client = FakeVoiceClient(half_start_failures=1)
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = [queue_entry("first"), queue_entry("second")]

        with self.assertLogs("jellychord", "ERROR"):
            await self.main.advanceTrack(guild)

        self.assertTrue(voice_client.failed_plays[0].cleaned_up)
        self.assertEqual(self.main.playing[42]["Id"], "second")
        self.assertEqual(len(voice_client.play_calls), 1)
        self.assertFalse(voice_client.disconnect_requested)

    async def test_repeated_half_started_players_give_up_and_disconnect(self):
        attempts = self.main.MAX_START_ATTEMPTS
        voice_client = FakeVoiceClient(half_start_failures=attempts)
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = [queue_entry(f"broken-{i}") for i in range(attempts)] + [queue_entry("untouched")]

        with self.assertLogs("jellychord", "ERROR"):
            await self.main.advanceTrack(guild)

        self.assertEqual(len(voice_client.failed_plays), attempts)
        self.assertEqual(queue_ids(self.main.queues[42]), ["untouched"])
        self.assertNotIn(42, self.main.playing)
        self.assertTrue(voice_client.disconnect_requested)

    async def test_stream_url_failure_counts_and_next_track_plays(self):
        class BrokenUrlJFAPI(FakeJFAPI):
            def getAudioHls(self, track_id, bitrate):
                if track_id == "bad-url":
                    raise ValueError("cannot build stream URL")
                return super().getAudioHls(track_id, bitrate)

        self.main.JF_APICLIENT = BrokenUrlJFAPI()
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = [queue_entry("bad-url"), queue_entry("good")]

        with self.assertLogs("jellychord", "ERROR") as logs:
            await self.main.advanceTrack(guild)

        self.assertIn("Failed to start the next track in guild 42 (attempt 1/", logs.output[0])
        self.assertEqual(self.main.playing[42]["Id"], "good")
        self.assertFalse(voice_client.disconnect_requested)

    async def test_skipped_tracks_that_fail_to_load_do_not_count_towards_giving_up(self):
        attempts = self.main.MAX_START_ATTEMPTS
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = [queue_entry(f"broken-{i}") for i in range(attempts)] + [queue_entry("good")]
        real_load_audio = self.main.loadAudio
        releases = []

        def slow_failing_load_audio(url):
            if "broken" not in url:
                return real_load_audio(url)
            release = threading.Event()
            releases.append(release)
            release.wait(5)
            raise OSError("ffmpeg failed")

        with mock.patch.object(self.main, "loadAudio", slow_failing_load_audio):
            task = asyncio.create_task(self.main.advanceTrack(guild))
            for i in range(attempts):
                async with asyncio.timeout(5):
                    while len(releases) <= i:
                        await asyncio.sleep(0.01)
                # the user skips each broken track while it is still loading
                self.main.skipCurrentTrack(guild)
                releases[i].set()
            await task

        self.assertEqual(self.main.playing[42]["Id"], "good")
        self.assertFalse(voice_client.disconnect_requested)

    async def test_after_callback_with_error_chains_to_next_track(self):
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = [queue_entry("first"), queue_entry("second")]

        await self.main.advanceTrack(guild)
        _, after = voice_client.play_calls[0]
        voice_client._playing = False
        with self.assertLogs("jellychord", "ERROR"):
            thread = threading.Thread(target=after, args=(RuntimeError("ffmpeg died"),))
            thread.start()
            thread.join()
            async with asyncio.timeout(5):
                while len(voice_client.play_calls) < 2:
                    await asyncio.sleep(0.01)

        self.assertEqual(self.main.playing[42]["Id"], "second")


class EmptyAlbumTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = import_main_with_stubs()

    def setUp(self):
        self.main.queues.clear()
        self.main.playing.clear()
        self.main.JF_APICLIENT = FakeJFAPI({"empty-album": []})

    async def test_empty_album_is_not_queued(self):
        for fairplay in (False, True):
            with self.subTest(fairplay=fairplay):
                self.main.config["fairplay"] = fairplay
                ctx = FakeContext(voice_client=FakeVoiceClient())

                with self.assertLogs("jellychord", "WARNING"):
                    await self.main.playHelperGeneric({"Type": "MusicAlbum", "Id": "empty-album"}, ctx, "last")

                self.assertNotIn(42, self.main.queues)
                self.assertEqual(ctx.responses, ["That album has no tracks to play"])


class FairplayPromoteDemoteTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = import_main_with_stubs()

    def setUp(self):
        self.main.queues.clear()
        self.main.playing.clear()
        self.main.config["fairplay"] = True
        self.main.queues[42] = {
            100: [queue_entry("user-100-a"), queue_entry("user-100-b"), queue_entry("user-100-c")],
            200: [queue_entry("user-200-a")],
        }

    def user_queue_ids(self):
        return {user_id: queue_ids(queue) for user_id, queue in self.main.queues[42].items()}

    async def test_demote_middle_track_moves_it_to_the_back(self):
        ctx = FakeContext(author_id=100)
        # flattened queue: user-100-a, user-200-a, user-100-b, user-100-c
        await self.main.demote(ctx, 3)

        self.assertEqual(
            self.user_queue_ids(),
            {100: ["user-100-a", "user-100-c", "user-100-b"], 200: ["user-200-a"]},
        )
        self.assertEqual(ctx.responses, ["Demoted track to the back of your tracks: Artist - user-100-b"])

    async def test_promote_last_track_moves_it_to_the_front(self):
        ctx = FakeContext(author_id=100)
        await self.main.promote(ctx, 4)

        self.assertEqual(
            self.user_queue_ids(),
            {100: ["user-100-c", "user-100-a", "user-100-b"], 200: ["user-200-a"]},
        )

    async def test_user_without_tracks_gets_own_tracks_reply(self):
        for command, verb in ((self.main.promote, "promote"), (self.main.demote, "demote")):
            with self.subTest(command=verb):
                ctx = FakeContext(author_id=300)

                await command(ctx, 1)

                self.assertEqual(ctx.responses, [f"You can only {verb} your own tracks in fairplay mode"])
                self.assertEqual(
                    self.user_queue_ids(),
                    {100: ["user-100-a", "user-100-b", "user-100-c"], 200: ["user-200-a"]},
                )


class FairplayDuplicateTrackTests(unittest.IsolatedAsyncioTestCase):
    """remove/promote/demote act on the entry at the index, not the first copy
    with the same Id."""

    @classmethod
    def setUpClass(cls):
        cls.main = import_main_with_stubs()

    def setUp(self):
        self.main.queues.clear()
        self.main.playing.clear()
        self.main.config["fairplay"] = True

    def tearDown(self):
        self.main.config["fairplay"] = False

    def entry(self, track_id, tag):
        entry = queue_entry(track_id)
        entry["tag"] = tag
        return entry

    def tags(self):
        return {uid: [e["tag"] for e in q] for uid, q in self.main.queues[42].items()}

    async def test_remove_duplicate_from_one_user_removes_copy_at_index(self):
        self.main.queues[42] = {100: [self.entry("song", "first"), self.entry("other", "x"), self.entry("song", "second")]}

        await self.main.remove(FakeContext(author_id=100), 3)

        self.assertEqual(self.tags(), {100: ["first", "x"]})

    async def test_remove_duplicate_across_users_removes_copy_at_index(self):
        self.main.queues[42] = {
            100: [self.entry("song", "user-100")],
            200: [self.entry("song", "user-200")],
        }

        # flattened: user-100 song, user-200 song
        await self.main.remove(FakeContext(author_id=200), 2)

        self.assertEqual(self.tags(), {100: ["user-100"]})

    async def test_promote_duplicate_moves_copy_at_index(self):
        self.main.queues[42] = {100: [self.entry("song", "first"), self.entry("other", "x"), self.entry("song", "second")]}

        await self.main.promote(FakeContext(author_id=100), 3)

        self.assertEqual(self.tags(), {100: ["second", "first", "x"]})

    async def test_demote_duplicate_moves_copy_at_index(self):
        self.main.queues[42] = {100: [self.entry("song", "first"), self.entry("song", "second"), self.entry("other", "x")]}

        await self.main.demote(FakeContext(author_id=100), 2)

        self.assertEqual(self.tags(), {100: ["first", "x", "second"]})

    async def test_promote_other_users_duplicate_is_refused(self):
        self.main.queues[42] = {
            100: [self.entry("other", "x"), self.entry("song", "user-100")],
            200: [self.entry("song", "user-200")],
        }
        ctx = FakeContext(author_id=100)

        # flattened: x, user-200 song, user-100 song
        await self.main.promote(ctx, 2)

        self.assertEqual(ctx.responses, ["You can only promote your own tracks in fairplay mode"])
        self.assertEqual(self.tags(), {100: ["x", "user-100"], 200: ["user-200"]})

    def set_up_rotation(self, next_idx):
        self.main.queues[42] = {
            100: [self.entry("a1", "a1"), self.entry("a2", "a2")],
            200: [self.entry("b1", "b1")],
            300: [self.entry("c1", "c1"), self.entry("c2", "c2")],
            400: [self.entry("d1", "d1"), self.entry("d2", "d2")],
        }
        self.main.playing[42] = {**queue_entry("now"), "nextTrackUserIdx": next_idx}

    def flattened_tags(self):
        return [e["tag"] for e in self.main.getFlattenedPlayQueue(42)]

    async def test_removing_users_last_track_keeps_rotation(self):
        # users: 100, 200 (one track, b1), 300, 400
        cases = {
            # removed user before the user due next
            "before": (2, ["c1", "d1", "a1", "c2", "d2", "a2"]),
            # removed user is the one due next: the following user is next
            "at": (1, ["c1", "d1", "a1", "c2", "d2", "a2"]),
            # removed user after the user due next
            "after": (0, ["a1", "c1", "d1", "a2", "c2", "d2"]),
        }
        for name, (next_idx, expected) in cases.items():
            with self.subTest(name):
                self.set_up_rotation(next_idx)
                tags = self.flattened_tags()
                await self.main.remove(FakeContext(), tags.index("b1") + 1)

                self.assertEqual(self.flattened_tags(), expected)
                self.assertNotIn(200, self.main.queues[42])

    async def test_removing_last_users_only_track_wraps_to_first_user(self):
        self.main.queues[42] = {
            100: [self.entry("a1", "a1")],
            200: [self.entry("b1", "b1")],
        }
        self.main.playing[42] = {**queue_entry("now"), "nextTrackUserIdx": 1}

        # flattened: b1, a1
        await self.main.remove(FakeContext(), 1)

        self.assertEqual(self.main.playing[42]["nextTrackUserIdx"], 0)
        self.assertEqual(self.flattened_tags(), ["a1"])
        self.assertEqual(self.main.popNextEntry(42)["tag"], "a1")

    async def test_removing_users_track_while_nothing_plays(self):
        self.main.queues[42] = {
            100: [self.entry("a1", "a1")],
            200: [self.entry("b1", "b1"), self.entry("b2", "b2")],
        }

        await self.main.remove(FakeContext(), 1)

        self.assertEqual(self.flattened_tags(), ["b1", "b2"])
        self.assertNotIn(42, self.main.playing)

    async def test_removing_only_track_clears_guild_queue(self):
        self.main.queues[42] = {100: [self.entry("a1", "a1")]}
        self.main.playing[42] = {**queue_entry("now"), "nextTrackUserIdx": 0}

        await self.main.remove(FakeContext(), 1)

        self.assertNotIn(42, self.main.queues)
        self.assertEqual(self.main.playing[42]["nextTrackUserIdx"], 0)


class VoiceStateListenerTests(unittest.IsolatedAsyncioTestCase):
    """onVoiceStateUpdate clears a guild's state when the bot is kicked or
    disconnected from voice, but not when it leaves on its own, moves, or
    py-cord briefly drops and recovers the connection."""

    BOT_ID = 999

    @classmethod
    def setUpClass(cls):
        cls.main = import_main_with_stubs()

    def setUp(self):
        self.main.queues.clear()
        self.main.playing.clear()
        self.main.loading.clear()
        self.main.playerLocks.clear()
        self.main.expectedDisconnects.clear()
        self.main.voiceEpochs.clear()
        self.main.config["fairplay"] = False
        self.main.JF_APICLIENT = FakeJFAPI()
        self.main.bot.user = types.SimpleNamespace(id=self.BOT_ID)
        self.main.queues[42] = [queue_entry("queued")]
        self.main.playing[42] = queue_entry("current")
        self.main.queues[7] = [queue_entry("other-guild")]
        self.guild = types.SimpleNamespace(id=42, voice_client=None)

    def member(self, member_id):
        return types.SimpleNamespace(id=member_id, guild=self.guild)

    def voice_state(self, channel):
        return types.SimpleNamespace(channel=channel)

    async def bot_leaves(self):
        await self.main.onVoiceStateUpdate(
            self.member(self.BOT_ID), self.voice_state(object()), self.voice_state(None)
        )

    def test_listener_is_registered(self):
        self.assertIn(
            ("on_voice_state_update", self.main.onVoiceStateUpdate),
            self.main.bot.listeners,
        )

    async def test_kick_clears_guild_state(self):
        await self.bot_leaves()

        self.assertNotIn(42, self.main.queues)
        self.assertNotIn(42, self.main.playing)
        self.assertEqual(queue_ids(self.main.queues[7]), ["other-guild"])

    async def test_kick_clears_state_once_voice_client_is_removed(self):
        # py-cord marks the connection disconnected first and removes the voice
        # client a moment later.
        voice_client = FakeVoiceClient()
        voice_client.connected = False
        self.guild.voice_client = voice_client

        async def fake_sleep(delay):
            self.guild.voice_client = None

        with mock.patch.object(self.main.asyncio, "sleep", fake_sleep):
            await self.bot_leaves()

        self.assertNotIn(42, self.main.queues)
        self.assertNotIn(42, self.main.playing)

    async def test_play_after_kick_does_not_replay_old_queue(self):
        await self.bot_leaves()
        ctx = FakeContext(voice_client=None)

        with mock.patch.object(self.main, "startPlayer", mock.AsyncMock()):
            await self.main.playHelperGeneric(raw_track("requested"), ctx, "last")

        self.assertEqual(queue_ids(self.main.queues[42]), ["requested"])

    async def test_voice_reconnect_keeps_state(self):
        # py-cord leaves and rejoins on its own while recovering a dropped
        # connection, keeping the same voice client.
        voice_client = FakeVoiceClient()
        voice_client.connected = False
        self.guild.voice_client = voice_client

        async def fake_sleep(delay):
            voice_client.connected = True

        with mock.patch.object(self.main.asyncio, "sleep", fake_sleep):
            await self.bot_leaves()

        self.assertEqual(queue_ids(self.main.queues[42]), ["queued"])
        self.assertEqual(self.main.playing[42]["Id"], "current")

    async def test_voice_that_never_recovers_keeps_state(self):
        voice_client = FakeVoiceClient()
        voice_client.connected = False
        self.guild.voice_client = voice_client

        with mock.patch.object(self.main, "VOICE_RECOVERY_TIMEOUT", 0.01), \
                mock.patch.object(self.main, "VOICE_POLL_INTERVAL", 0.005):
            await self.bot_leaves()

        self.assertIn(42, self.main.queues)
        self.assertIn(42, self.main.playing)

    async def test_own_leave_keeps_track_queued_while_leaving(self):
        # The queue ran out and the bot started leaving; someone /play'd
        # before the leave event arrived. /start should still find the track.
        self.main.queues.pop(42)
        voice_client = FakeVoiceClient()
        self.guild.voice_client = voice_client

        await self.main.advanceTrack(self.guild)
        self.assertTrue(voice_client.disconnect_requested)
        self.assertIn(42, self.main.expectedDisconnects)

        await self.main.playHelperGeneric(raw_track("late"), FakeContext(voice_client=voice_client), "last")
        self.guild.voice_client = None
        await self.bot_leaves()

        self.assertEqual(queue_ids(self.main.queues[42]), ["late"])
        self.assertNotIn(42, self.main.expectedDisconnects)

    async def test_kick_after_own_leave_is_handled_normally(self):
        self.main.expectedDisconnects[42] = math.inf
        await self.bot_leaves()  # the bot's own leave consumes the flag
        self.main.queues[42] = [queue_entry("new-session")]

        await self.bot_leaves()

        self.assertNotIn(42, self.main.queues)

    async def test_kick_after_stale_own_leave_flag_clears_state(self):
        # stopPlayback's disconnect finished without py-cord delivering a leave
        # event, so the flag expired. A later kick must still clean up.
        self.main.expectedDisconnects[42] = 0

        await self.bot_leaves()

        self.assertNotIn(42, self.main.queues)
        self.assertNotIn(42, self.main.expectedDisconnects)

    async def test_own_leave_flag_expires_after_disconnect_without_leave_event(self):
        voice_client = FakeVoiceClient()
        self.guild.voice_client = voice_client

        with mock.patch.object(self.main.time, "monotonic", return_value=1000.0):
            await self.main.stopPlayback(self.guild, voice_client)

        self.assertEqual(self.main.expectedDisconnects[42], 1000.0 + self.main.EXPECTED_LEAVE_GRACE)
        self.guild.voice_client = None
        with mock.patch.object(self.main.time, "monotonic", return_value=1000.0 + self.main.EXPECTED_LEAVE_GRACE + 1):
            await self.bot_leaves()

        self.assertNotIn(42, self.main.queues)

    async def test_failed_own_disconnect_does_not_hide_later_kick(self):
        voice_client = FakeVoiceClient()
        self.guild.voice_client = voice_client

        async def failing_disconnect(force=False):
            raise RuntimeError("gateway closed")

        voice_client.disconnect = failing_disconnect
        with self.assertLogs("jellychord", "ERROR"):
            await self.main.stopPlayback(self.guild, voice_client)

        self.assertNotIn(42, self.main.expectedDisconnects)
        self.guild.voice_client = None
        await self.bot_leaves()

        self.assertNotIn(42, self.main.queues)

    async def test_older_listener_does_not_clear_after_later_own_leave(self):
        # Listener A waits on a reconnect leave. Meanwhile the bot recovers,
        # finishes the queue and leaves on its own (listener B), and someone
        # queues a track. A must not delete it when it wakes.
        voice_client = FakeVoiceClient()
        voice_client.connected = False
        self.guild.voice_client = voice_client
        self.main.queues[42] = [queue_entry("late")]

        async def fake_sleep(delay):
            self.main.expectedDisconnects[42] = math.inf
            self.guild.voice_client = None
            await self.bot_leaves()

        with mock.patch.object(self.main.asyncio, "sleep", fake_sleep):
            await self.bot_leaves()

        self.assertEqual(queue_ids(self.main.queues[42]), ["late"])

    async def test_listener_does_not_clear_after_new_session_starts(self):
        voice_client = FakeVoiceClient()
        voice_client.connected = False
        self.guild.voice_client = voice_client
        new_client = FakeVoiceClient()
        ctx = FakeContext()
        ctx.guild = self.guild

        async def connect():
            self.guild.voice_client = new_client
            return new_client

        ctx.author.voice = types.SimpleNamespace(channel=types.SimpleNamespace(connect=connect))

        async def fake_sleep(delay):
            # kicked, then /play starts a new session before the listener wakes
            self.guild.voice_client = None
            self.main.expectedDisconnects[42] = math.inf  # stale flag from an earlier session
            with mock.patch.object(self.main, "playTrack", mock.AsyncMock()):
                await self.main.startPlayer(ctx)

        with mock.patch.object(self.main.asyncio, "sleep", fake_sleep):
            await self.bot_leaves()

        self.assertIn(42, self.main.queues)
        self.assertNotIn(42, self.main.expectedDisconnects)

    async def test_bot_moving_channels_keeps_state(self):
        await self.main.onVoiceStateUpdate(
            self.member(self.BOT_ID), self.voice_state(object()), self.voice_state(object())
        )

        self.assertIn(42, self.main.queues)
        self.assertIn(42, self.main.playing)

    async def test_other_member_leaving_voice_keeps_state(self):
        await self.main.onVoiceStateUpdate(
            self.member(123), self.voice_state(object()), self.voice_state(None)
        )

        self.assertIn(42, self.main.queues)
        self.assertIn(42, self.main.playing)


if __name__ == "__main__":
    unittest.main()
