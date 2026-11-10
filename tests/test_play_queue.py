import asyncio
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
    records the after= callback instead of starting a player thread."""

    def __init__(self, bitrate=96_000):
        self.channel = types.SimpleNamespace(bitrate=bitrate)
        self.play_calls = []
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
        was_playing = self._playing
        self._playing = False
        self._paused = False
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

    async def test_play_failure_cleans_up_and_leaves_voice(self):
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = [queue_entry("first"), queue_entry("second")]
        loads = []
        real_load_audio = self.main.loadAudio

        def recording_load_audio(url):
            loads.append(real_load_audio(url))
            return loads[-1]

        def failing_play(audio, after):
            raise RuntimeError("Not connected to voice.")

        voice_client.play = failing_play
        with mock.patch.object(self.main, "loadAudio", recording_load_audio), self.assertLogs("jellychord", "ERROR"):
            await self.main.advanceTrack(guild)

        self.assertTrue(loads[0].cleaned_up)
        self.assertTrue(voice_client.disconnect_requested)
        self.assertNotIn(42, self.main.playing)
        # the rest of the queue is kept for /start
        self.assertEqual(queue_ids(self.main.queues[42]), ["second"])

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


if __name__ == "__main__":
    unittest.main()
