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
    def __init__(self, bitrate=96_000):
        self.channel = types.SimpleNamespace(bitrate=bitrate)
        self.loop = object()
        self.play_calls = []
        self.stopped = False
        self.disconnect_requested = False
        self.playing = False
        self.paused = False
        self.pause_calls = 0
        self.resume_calls = 0
        self.connected = True
        self.disconnect_kwargs = None

    def play(self, audio, after):
        self.play_calls.append((audio, after))
        self.playing = True

    def stop(self):
        self.stopped = True
        self.playing = self.paused = False

    def is_playing(self):
        return self.playing and not self.paused

    def is_paused(self):
        return self.playing and self.paused

    def pause(self):
        self.pause_calls += 1
        self.paused = True

    def resume(self):
        self.resume_calls += 1
        self.paused = False

    def is_connected(self):
        return self.connected

    def disconnect(self, **kwargs):
        self.disconnect_requested = True
        self.disconnect_kwargs = kwargs
        return "disconnect-coroutine"


class FakeContext:
    def __init__(self, guild_id=42, author_id=100, voice_client=None):
        self.guild_id = guild_id
        self.author = types.SimpleNamespace(id=author_id, voice=None)
        self.voice_client = voice_client
        self.guild = types.SimpleNamespace(id=guild_id, voice_client=voice_client)
        self.responses = []

    async def respond(self, message, **kwargs):
        self.responses.append(message)


class PlayQueueTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = import_main_with_stubs()

    def setUp(self):
        self.main.queues.clear()
        self.main.playing.clear()
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

    def test_play_next_track_regular_queue_starts_first_track_and_preserves_tail(self):
        voice_client = FakeVoiceClient(bitrate=128_000)
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = [queue_entry("first"), queue_entry("second")]
        self.main.JF_APICLIENT = FakeJFAPI()

        self.main.playNextTrack(guild)

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

    def test_play_next_track_regular_queue_removes_guild_queue_after_last_track(self):
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = [queue_entry("only")]
        self.main.JF_APICLIENT = FakeJFAPI()

        self.main.playNextTrack(guild)

        self.assertEqual(self.main.playing[42]["Id"], "only")
        self.assertNotIn(42, self.main.queues)
        self.assertEqual(len(voice_client.play_calls), 1)

    def test_play_next_track_fairplay_rotates_users_and_removes_empty_user_queue(self):
        self.main.config["fairplay"] = True
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = {
            100: [queue_entry("user-100-a")],
            200: [queue_entry("user-200-a"), queue_entry("user-200-b")],
        }
        self.main.playing[42] = {"nextTrackUserIdx": 0}
        self.main.JF_APICLIENT = FakeJFAPI()

        self.main.playNextTrack(guild)

        self.assertEqual(self.main.playing[42]["Id"], "user-100-a")
        self.assertEqual(self.main.playing[42]["nextTrackUserIdx"], 0)
        self.assertEqual(
            {user_id: queue_ids(queue) for user_id, queue in self.main.queues[42].items()},
            {200: ["user-200-a", "user-200-b"]},
        )
        self.assertEqual(self.main.getFlattenedPlayQueue(42), [queue_entry("user-200-a"), queue_entry("user-200-b")])
        self.assertEqual(self.main.JF_APICLIENT.audio_requests, [("user-100-a", 96_000)])
        self.assertEqual(len(voice_client.play_calls), 1)

    def test_play_next_track_disconnects_when_queue_is_empty(self):
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.playing[42] = queue_entry("finished")
        scheduled = []

        def fake_run_coroutine_threadsafe(coro, loop):
            scheduled.append((coro, loop))
            return object()

        with mock.patch.object(self.main.asyncio, "run_coroutine_threadsafe", fake_run_coroutine_threadsafe):
            self.main.playNextTrack(guild)

        self.assertNotIn(42, self.main.playing)
        self.assertTrue(voice_client.disconnect_requested)
        self.assertEqual(scheduled, [("disconnect-coroutine", voice_client.loop)])


    def test_play_next_track_publishes_fully_initialised_entry(self):
        self.main.config["fairplay"] = True
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = {100: [queue_entry("first")], 200: [queue_entry("second")]}
        published = []

        class RecordingDict(dict):
            # Commands on the event loop can read `playing` at any moment, so
            # whatever gets stored there must already be complete.
            def __setitem__(self, key, value):
                published.append(dict(value))
                super().__setitem__(key, value)

        with mock.patch.object(self.main, "playing", RecordingDict()):
            self.main.playNextTrack(guild)

        self.assertEqual(len(published), 1)
        for key in ("playtime-offset", "starttime", "paused", "nextTrackUserIdx"):
            self.assertIn(key, published[0])
        self.assertFalse(published[0]["paused"])

    def test_play_next_track_skips_track_that_fails_to_start(self):
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = [queue_entry("broken"), queue_entry("good")]

        class BrokenAudio:
            instances = []

            def __init__(self, url, **kwargs):
                self.url = url
                self.cleaned_up = False
                BrokenAudio.instances.append(self)

            def read(self):
                if "broken" in self.url:
                    raise RuntimeError("ffmpeg failed")
                return b""

            def cleanup(self):
                self.cleaned_up = True

        with mock.patch.object(self.main.discord, "FFmpegOpusAudio", BrokenAudio), \
                self.assertLogs(self.main._log, "ERROR"):
            self.main.playNextTrack(guild)

        self.assertEqual(self.main.playing[42]["Id"], "good")
        self.assertEqual(len(voice_client.play_calls), 1)
        self.assertTrue(BrokenAudio.instances[0].cleaned_up)
        self.assertNotIn(42, self.main.queues)

    def test_play_next_track_disconnects_when_only_track_fails_to_start(self):
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = [queue_entry("broken")]
        self.main.JF_APICLIENT.getAudioHls = mock.Mock(side_effect=RuntimeError("boom"))

        with mock.patch.object(self.main.asyncio, "run_coroutine_threadsafe", lambda coro, loop: None), \
                self.assertLogs(self.main._log, "ERROR"):
            self.main.playNextTrack(guild)

        self.assertNotIn(42, self.main.playing)
        self.assertTrue(voice_client.disconnect_requested)
        self.assertEqual(voice_client.play_calls, [])


    def test_play_next_track_stops_when_voice_connection_is_lost(self):
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = [queue_entry("first"), queue_entry("second"), queue_entry("third")]

        def play(audio, after):
            raise RuntimeError("Not connected to voice")

        voice_client.play = play
        voice_client.connected = False
        scheduled = []

        with mock.patch.object(self.main.asyncio, "run_coroutine_threadsafe", lambda coro, loop: scheduled.append(coro)), \
                self.assertLogs(self.main._log, "ERROR"):
            self.main.playNextTrack(guild)

        # Only the track being started is consumed; the rest is kept for /start.
        self.assertEqual(queue_ids(self.main.queues[42]), ["second", "third"])
        self.assertNotIn(42, self.main.playing)
        self.assertEqual(scheduled, ["disconnect-coroutine"])
        self.assertEqual(voice_client.disconnect_kwargs, {"force": True})

    def test_play_next_track_fairplay_wraps_to_first_user_after_last_user_finishes(self):
        self.main.config["fairplay"] = True
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = {
            100: [queue_entry("a1"), queue_entry("a2")],
            200: [queue_entry("b1"), queue_entry("b2")],
            300: [queue_entry("c1")],
        }
        self.main.playing[42] = {"nextTrackUserIdx": 2}

        played = []
        for _ in range(4):
            self.main.playNextTrack(guild)
            played.append(self.main.playing[42]["Id"])

        self.assertEqual(played, ["c1", "a1", "b1", "a2"])

    def test_play_next_track_fairplay_skips_failed_track_and_keeps_rotation(self):
        self.main.config["fairplay"] = True
        voice_client = FakeVoiceClient()
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = {
            100: [queue_entry("a")],
            200: [queue_entry("b")],
            300: [queue_entry("broken")],
        }
        self.main.playing[42] = {"nextTrackUserIdx": 2}
        jf = self.main.JF_APICLIENT

        def get_audio_hls(track_id, bitrate):
            if track_id == "broken":
                raise RuntimeError("boom")
            return FakeJFAPI.getAudioHls(jf, track_id, bitrate)

        jf.getAudioHls = get_audio_hls
        with self.assertLogs(self.main._log, "ERROR"):
            self.main.playNextTrack(guild)

        self.assertEqual(self.main.playing[42]["Id"], "a")
        self.assertEqual({uid: queue_ids(q) for uid, q in self.main.queues[42].items()}, {200: ["b"]})
        self.main.playNextTrack(guild)
        self.assertEqual(self.main.playing[42]["Id"], "b")
        self.assertNotIn(42, self.main.queues)


class PauseResumeTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = import_main_with_stubs()

    def setUp(self):
        self.main.queues.clear()
        self.main.playing.clear()
        self.main.config["fairplay"] = False
        self.main.JF_APICLIENT = FakeJFAPI()

    def start_track(self, voice_client):
        guild = types.SimpleNamespace(id=42, voice_client=voice_client)
        self.main.queues[42] = [queue_entry("track")]
        self.main.playNextTrack(guild)

    async def test_pause_and_resume_when_connected_but_nothing_loaded(self):
        for command in (self.main.pause, self.main.resume):
            voice_client = FakeVoiceClient()
            ctx = FakeContext(voice_client=voice_client)
            await command(ctx)
            self.assertEqual(ctx.responses, ["Not currently playing"])
            self.assertEqual((voice_client.pause_calls, voice_client.resume_calls), (0, 0))

    async def test_pause_and_resume_between_tracks(self):
        # Entry for the next track exists, but its stream hasn't started yet.
        self.main.playing[42] = queue_entry("next")
        for command in (self.main.pause, self.main.resume):
            voice_client = FakeVoiceClient()
            ctx = FakeContext(voice_client=voice_client)
            await command(ctx)
            self.assertEqual(ctx.responses, ["Not currently playing"])
            self.assertEqual((voice_client.pause_calls, voice_client.resume_calls), (0, 0))

    async def test_pause_then_resume_while_playing(self):
        voice_client = FakeVoiceClient()
        self.start_track(voice_client)
        ctx = FakeContext(voice_client=voice_client)

        await self.main.resume(ctx)
        await self.main.pause(ctx)
        self.assertTrue(self.main.playing[42]["paused"])
        await self.main.pause(ctx)
        await self.main.resume(ctx)
        self.assertFalse(self.main.playing[42]["paused"])

        self.assertEqual(
            ctx.responses,
            ["Already playing", "Pausing playback", "Already paused", "Resuming playback"],
        )
        self.assertEqual((voice_client.pause_calls, voice_client.resume_calls), (1, 1))

    async def test_pause_and_resume_survive_track_ending_during_reply(self):
        for command, expected_paused in ((self.main.pause, True), (self.main.resume, False)):
            voice_client = FakeVoiceClient()
            self.start_track(voice_client)
            if command is self.main.resume:
                await self.main.pause(FakeContext(voice_client=voice_client))
            ctx = FakeContext(voice_client=voice_client)
            seen = []

            async def respond(message, **kwargs):
                # The reply is a network round trip; the track can end meanwhile.
                seen.append(self.main.playing[42]["paused"])
                self.main.playing.pop(42)

            ctx.respond = respond
            await command(ctx)
            self.assertEqual(seen, [expected_paused])

    async def test_pause_and_resume_when_not_connected(self):
        for command in (self.main.pause, self.main.resume):
            ctx = FakeContext(voice_client=None)
            await command(ctx)
            self.assertEqual(ctx.responses, ["Not connect to any voice channel"])


if __name__ == "__main__":
    unittest.main()
