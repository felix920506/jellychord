import types
import unittest
from unittest import mock

from tests.test_main_helpers import import_main_with_stubs
from tests.test_play_queue import raw_track


def make_search_ctx():
    return types.SimpleNamespace(
        guild_id=42,
        author=types.SimpleNamespace(id=100, voice=object()),
        voice_client=None,
        defer=mock.AsyncMock(),
        respond=mock.AsyncMock(),
    )


def make_interaction(user_id=100):
    return types.SimpleNamespace(
        user=types.SimpleNamespace(id=user_id),
        response=types.SimpleNamespace(
            edit_message=mock.AsyncMock(),
            send_message=mock.AsyncMock(),
        ),
    )


async def dispatch(view, item, interaction):
    # Mirrors py-cord's View._scheduled_task: the item callback only runs if
    # interaction_check passes, otherwise on_check_failure is called.
    if not await view.interaction_check(interaction):
        return await view.on_check_failure(interaction)
    await item.callback(interaction)


class SearchViewTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = import_main_with_stubs()

    async def open_search(self, results, when="last"):
        ctx = make_search_ctx()
        with mock.patch.object(self.main, "searchHelper", mock.AsyncMock(return_value=results)):
            await self.main.search(ctx, "term", None, when)
        view = ctx.respond.await_args.kwargs["view"]
        view.message = types.SimpleNamespace(edit=mock.AsyncMock())
        return ctx, view, view.items[0]

    async def select(self, dropdown, index, interaction=None):
        dropdown.refresh_state({"values": [str(index)]})
        await dropdown.callback(interaction or make_interaction())

    async def test_selection_stops_view_and_plays_selected_item(self):
        tracks = [raw_track("a", name="Song A"), raw_track("b", name="Song B")]
        ctx, view, dropdown = await self.open_search(tracks, when="next")
        interaction = make_interaction()

        with mock.patch.object(self.main, "playHelperGeneric", mock.AsyncMock()) as play:
            await self.select(dropdown, 1, interaction)

        self.assertTrue(view.stopped)
        kwargs = interaction.response.edit_message.await_args.kwargs
        self.assertIn("Song B", kwargs["content"])
        self.assertIsNone(kwargs["view"])
        play.assert_awaited_once_with(tracks[1], ctx, "next")

    async def test_view_is_stopped_before_the_message_is_edited(self):
        # A timeout that fires while edit_message is awaited must not be able
        # to replace "Playing X", so the view has to be stopped already.
        _, view, dropdown = await self.open_search([raw_track("a")])
        interaction = make_interaction()
        stopped_during_edit = []
        interaction.response.edit_message.side_effect = lambda **kwargs: stopped_during_edit.append(view.stopped)

        with mock.patch.object(self.main, "playHelperGeneric", mock.AsyncMock()):
            await self.select(dropdown, 0, interaction)

        self.assertEqual(stopped_during_edit, [True])

    async def test_view_is_stopped_when_editing_the_message_fails(self):
        _, view, dropdown = await self.open_search([raw_track("a")])
        interaction = make_interaction()
        interaction.response.edit_message.side_effect = RuntimeError("edit failed")

        with mock.patch.object(self.main, "playHelperGeneric", mock.AsyncMock()) as play:
            with self.assertRaises(RuntimeError):
                await self.select(dropdown, 0, interaction)

        self.assertTrue(view.stopped)
        play.assert_not_awaited()

    async def test_view_is_stopped_when_playing_fails(self):
        _, view, dropdown = await self.open_search([raw_track("a")])

        with mock.patch.object(self.main, "playHelperGeneric", mock.AsyncMock(side_effect=RuntimeError("play failed"))):
            with self.assertRaises(RuntimeError):
                await self.select(dropdown, 0)

        self.assertTrue(view.stopped)

    async def test_other_user_cannot_pick_and_invoker_still_can(self):
        tracks = [raw_track("a", name="Song A"), raw_track("b", name="Song B")]
        ctx, view, dropdown = await self.open_search(tracks)
        other = make_interaction(user_id=200)

        with mock.patch.object(self.main, "playHelperGeneric", mock.AsyncMock()) as play:
            dropdown.refresh_state({"values": ["0"]})
            await dispatch(view, dropdown, other)

            other.response.send_message.assert_awaited_once()
            self.assertTrue(other.response.send_message.await_args.kwargs.get("ephemeral"))
            other.response.edit_message.assert_not_awaited()
            play.assert_not_awaited()
            self.assertFalse(view.stopped)

            owner = make_interaction(user_id=ctx.author.id)
            dropdown.refresh_state({"values": ["1"]})
            await dispatch(view, dropdown, owner)

        self.assertTrue(view.stopped)
        self.assertIn("Song B", owner.response.edit_message.await_args.kwargs["content"])
        play.assert_awaited_once_with(tracks[1], ctx, "last")

    async def test_labels_for_items_missing_artists_or_length(self):
        bare = {"Type": "Audio", "Name": "Bare", "Id": "bare"}
        unknown_length = {"Type": "Audio", "Artists": None, "Name": "Live", "Id": "live", "RunTimeTicks": None}
        album = {"Type": "MusicAlbum", "Name": "Album", "Id": "album"}
        _, _, dropdown = await self.open_search([bare, unknown_length, album])

        self.assertEqual(
            [option["label"] for option in dropdown.options],
            ["Track: Bare", "Track: Live", "Album: Album"],
        )

    async def test_picking_an_item_missing_artists_and_length_queues_it(self):
        bare = {"Type": "Audio", "Name": "Bare", "Id": "bare"}
        _, _, dropdown = await self.open_search([bare])
        interaction = make_interaction()

        with mock.patch.object(self.main, "playHelperGeneric", mock.AsyncMock()) as play:
            await self.select(dropdown, 0, interaction)

        self.assertEqual(interaction.response.edit_message.await_args.kwargs["content"], "Playing Track: Bare")
        play.assert_awaited_once()

    async def test_timeout_without_selection_shows_timed_out(self):
        # py-cord calls view.on_timeout() with no arguments.
        _, view, dropdown = await self.open_search([raw_track("a")])

        await view.on_timeout()

        view.message.edit.assert_awaited_once_with("Selection timed out.", view=None)
        self.assertTrue(dropdown.disabled)


class PycordSelectContractTests(unittest.TestCase):
    """The stub's Select mirrors py-cord: a string select's choice is read from
    the public values property once py-cord has refreshed it from the
    interaction."""

    def test_values_returns_the_selected_strings(self):
        try:
            import discord
        except ImportError:
            self.skipTest("py-cord is not installed")
        select = discord.ui.Select(options=[discord.SelectOption(label="a", value="0"),
                                            discord.SelectOption(label="b", value="1")])
        self.assertIsNone(select.values)

        interaction = mock.Mock(spec=discord.Interaction)
        interaction.data = {"values": ["1"]}
        select.refresh_state(interaction)

        self.assertEqual(select.values, ["1"])


if __name__ == "__main__":
    unittest.main()
