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

    def play(self, audio, after):
        self.play_calls.append((audio, after))

    def stop(self):
        self.stopped = True

    def disconnect(self):
        self.disconnect_requested = True
        return "disconnect-coroutine"


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


if __name__ == "__main__":
    unittest.main()
