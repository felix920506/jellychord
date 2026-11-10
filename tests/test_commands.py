"""Command handler tests for the paths the playback and queue view tests
don't reach: the refusals (empty queue, bad index, not in voice, nothing
playing), non-fairplay /promote and /demote, and /pause, /resume and
/nowplaying together."""

import datetime
import types
import unittest
from unittest import mock

from tests.test_main_helpers import import_main_with_stubs
from tests.test_play_queue import FakeContext, FakeVoiceClient, LiveContext, queue_entry, queue_ids, raw_track


def fill_queue(main, fairplay, guild_id=42):
    """Queue three tracks: non-fairplay a, b, c; fairplay user 100 a, c and
    user 200 b, which play (and are numbered) a, b, c."""
    if fairplay:
        main.queues[guild_id] = {100: [queue_entry("a"), queue_entry("c")], 200: [queue_entry("b")]}
    else:
        main.queues[guild_id] = [queue_entry("a"), queue_entry("b"), queue_entry("c")]


class CommandTestCase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = import_main_with_stubs()

    def setUp(self):
        for state in (self.main.queues, self.main.playing, self.main.loading, self.main.playerLocks,
                      self.main.leaving):
            state.clear()
        self.main.config["fairplay"] = False

    def tearDown(self):
        self.main.config["fairplay"] = False

    def flattened_ids(self):
        return queue_ids(self.main.getFlattenedPlayQueue(42))


class QueueCommandRefusalTests(CommandTestCase):
    """Queue commands refuse without touching the queue, in both modes."""

    def queue_commands(self, fairplay):
        commands = {
            "remove": self.main.remove,
            "promote": self.main.promote,
            "demote": self.main.demote,
        }
        if not fairplay:
            # /playnow is only registered without fairplay
            commands["playnow"] = self.main.playnow
        return commands

    async def test_empty_queue(self):
        empty_replies = {
            "remove": "Queue is empty",
            "promote": "Queue is empty",
            "demote": "Queue is empty",
            "playnow": "Playlist is empty",
            "shuffle": "Playlist is empty",
            "clear": "Playlist is empty",
            "queue": "Empty Queue",
        }
        for fairplay in (False, True):
            commands = {
                **{name: (lambda c: lambda ctx: c(ctx, 1))(c) for name, c in self.queue_commands(fairplay).items()},
                "shuffle": self.main.shuffle,
                "clear": self.main.clear,
                "queue": self.main.queue,
            }
            for name, command in commands.items():
                with self.subTest(fairplay=fairplay, command=name):
                    self.main.config["fairplay"] = fairplay
                    ctx = FakeContext(voice_client=FakeVoiceClient())

                    await command(ctx)

                    self.assertEqual(ctx.responses, [empty_replies[name]])
                    self.assertEqual(self.main.queues, {})
                    self.assertFalse(ctx.voice_client.stopped)

    async def test_index_past_the_end(self):
        for fairplay in (False, True):
            for name, command in self.queue_commands(fairplay).items():
                with self.subTest(fairplay=fairplay, command=name):
                    self.main.config["fairplay"] = fairplay
                    fill_queue(self.main, fairplay)
                    ctx = FakeContext(author_id=100, voice_client=FakeVoiceClient())

                    await command(ctx, 4)

                    self.assertEqual(ctx.responses, ["Specified index does not exist"])
                    self.assertEqual(self.flattened_ids(), ["a", "b", "c"])
                    self.assertFalse(ctx.voice_client.stopped)


class QueueCommandTests(CommandTestCase):
    async def test_promote_moves_track_to_the_front(self):
        fill_queue(self.main, fairplay=False)
        ctx = FakeContext()

        await self.main.promote(ctx, 3)

        self.assertEqual(queue_ids(self.main.queues[42]), ["c", "a", "b"])
        self.assertEqual(ctx.responses, ["Promoted track to the front: Artist - c"])

    async def test_demote_moves_track_to_the_back(self):
        fill_queue(self.main, fairplay=False)
        ctx = FakeContext()

        await self.main.demote(ctx, 1)

        self.assertEqual(queue_ids(self.main.queues[42]), ["b", "c", "a"])
        self.assertEqual(ctx.responses, ["Demoted track to the back: Artist - a"])

    async def test_promote_and_demote_ignore_the_author_without_fairplay(self):
        # Without fairplay anyone may reorder any track.
        fill_queue(self.main, fairplay=False)

        await self.main.promote(FakeContext(author_id=300), 2)
        await self.main.demote(FakeContext(author_id=400), 1)

        self.assertEqual(queue_ids(self.main.queues[42]), ["a", "c", "b"])

    async def test_removing_the_last_track_empties_the_queue(self):
        self.main.queues[42] = [queue_entry("only")]
        ctx = FakeContext()

        await self.main.remove(ctx, 1)
        await self.main.queue(ctx)
        await self.main.start(ctx)

        self.assertNotIn(42, self.main.queues)
        self.assertEqual(ctx.responses, ["Deleted track: Artist - only", "Empty Queue", "No tracks to play"])

    async def test_shuffle_shuffles_the_queue(self):
        fill_queue(self.main, fairplay=False)
        ctx = FakeContext()

        with mock.patch.object(self.main.random, "shuffle", side_effect=lambda items: items.reverse()):
            await self.main.shuffle(ctx)

        self.assertEqual(queue_ids(self.main.queues[42]), ["c", "b", "a"])
        self.assertEqual(ctx.responses, ["Shuffling playlist"])

    async def test_fairplay_queue_numbers_are_the_indexes_commands_take(self):
        self.main.config["fairplay"] = True
        fill_queue(self.main, fairplay=True)
        ctx = FakeContext(author_id=100)

        await self.main.queue(ctx)
        # /queue lists the tracks in rotation order, numbered from 1
        self.assertEqual(
            ctx.responses[-1],
            "Tracks in playlist:\n1. Artist - a\n2. Artist - b\n3. Artist - c\nPage: 1/1",
        )

        # #3 is user 100's "c": promoting it puts it first among their tracks
        await self.main.promote(ctx, 3)
        await self.main.queue(ctx)
        self.assertEqual(
            ctx.responses[-1],
            "Tracks in playlist:\n1. Artist - c\n2. Artist - b\n3. Artist - a\nPage: 1/1",
        )

        await self.main.remove(ctx, 2)
        self.assertEqual(ctx.responses[-1], "Deleted track: Artist - b")
        self.assertEqual(self.flattened_ids(), ["c", "a"])


class PlaybackCommandTests(CommandTestCase):
    def play(self, voice_client, length=185, started_ago=30):
        """Make a track the now-playing one, started_ago seconds in."""
        voice_client._playing = True
        self.main.playing[42] = {
            **queue_entry("song", length=length),
            "playtime-offset": datetime.timedelta(),
            "starttime": datetime.datetime.now() - datetime.timedelta(seconds=started_ago),
            "paused": False,
        }
        return self.main.playing[42]

    async def test_pause_freezes_and_resume_continues_the_elapsed_time(self):
        voice_client = FakeVoiceClient()
        ctx = FakeContext(voice_client=voice_client)
        track = self.play(voice_client)

        await self.main.nowplaying(ctx)
        await self.main.pause(ctx)
        await self.main.pause(ctx)
        await self.main.nowplaying(ctx)
        self.assertTrue(voice_client.is_paused())

        # time passing while paused doesn't count
        track["starttime"] -= datetime.timedelta(seconds=60)
        await self.main.nowplaying(ctx)

        await self.main.resume(ctx)
        await self.main.resume(ctx)
        self.assertTrue(voice_client.is_playing())
        track["starttime"] -= datetime.timedelta(seconds=10)
        await self.main.nowplaying(ctx)

        self.assertEqual(ctx.responses, [
            "Currently Playing: Artist - song 00:30/03:05",
            "Pausing playback",
            "Already paused",
            "Currently Playing: Artist - song 00:30/03:05",
            "Currently Playing: Artist - song 00:30/03:05",
            "Resuming playback",
            "Already playing",
            "Currently Playing: Artist - song 00:40/03:05",
        ])

    async def test_playback_commands_when_not_in_voice(self):
        # Kept queue after the bot left voice: nothing to control, and the
        # queue stays for /start.
        self.main.queues[42] = [queue_entry("kept")]
        ctx = FakeContext(voice_client=None)

        for command in (self.main.skip, self.main.pause, self.main.resume, self.main.stop, self.main.nowplaying):
            await command(ctx)

        self.assertEqual(ctx.responses, [
            "Not currently playing",
            "Not connect to any voice channel",
            "Not connect to any voice channel",
            "Not connected to any voice channel",
            "Not Currently Playing",
        ])
        self.assertEqual(queue_ids(self.main.queues[42]), ["kept"])

    async def test_start_when_already_in_voice(self):
        voice_client = FakeVoiceClient()
        ctx = FakeContext(voice_client=voice_client)
        ctx.author.voice = types.SimpleNamespace(channel=None)
        self.play(voice_client)
        self.main.queues[42] = [queue_entry("next")]

        await self.main.start(ctx)

        self.assertEqual(ctx.responses, ["Already Playing"])
        self.assertEqual(self.main.playing[42]["Id"], "song")
        self.assertEqual(queue_ids(self.main.queues[42]), ["next"])

    async def test_start_when_invoker_not_in_voice(self):
        self.main.queues[42] = [queue_entry("kept")]
        ctx = FakeContext(voice_client=None)

        await self.main.start(ctx)

        self.assertEqual(ctx.responses, ["You are not in any voice channel"])
        self.assertEqual(queue_ids(self.main.queues[42]), ["kept"])


class SearchRefusalTests(CommandTestCase):
    async def search(self, command, results, in_voice):
        guild = types.SimpleNamespace(id=42, voice_client=None)
        ctx = LiveContext(guild, channel=object() if in_voice else None)
        with mock.patch.object(self.main, "searchHelper", mock.AsyncMock(return_value=results)):
            await command(ctx, "term", None, "last")
        return ctx

    async def test_no_results(self):
        for name, command, reply in (("search", self.main.search, "No items match your query."),
                                     ("play", self.main.play, "No items match your query")):
            with self.subTest(command=name):
                ctx = await self.search(command, [], in_voice=True)

                self.assertEqual(ctx.responses, [reply])
                self.assertEqual(self.main.queues, {})

    async def test_search_when_invoker_not_in_voice(self):
        ctx = await self.search(self.main.search, [raw_track("found")], in_voice=False)

        self.assertEqual(ctx.responses, ["You are not in any voice channel"])
        self.assertEqual(self.main.queues, {})


class FairplayRegistrationTests(unittest.TestCase):
    def test_playnow_is_not_registered_with_fairplay(self):
        main = import_main_with_stubs({"JELLYCHORD_ENABLE_FAIRPLAY": "1"})

        self.assertTrue(main.config["fairplay"])
        self.assertFalse(hasattr(main, "playnow"))


if __name__ == "__main__":
    unittest.main()
