import types
import unittest
from unittest import mock

from tests.test_main_helpers import import_main_with_stubs
from tests.test_play_queue import queue_entry


OWNER_ID = 100


def make_ctx(guild_id=1, user_id=OWNER_ID):
    return types.SimpleNamespace(
        guild_id=guild_id,
        author=types.SimpleNamespace(id=user_id),
        respond=mock.AsyncMock(),
    )


def make_interaction(guild_id=1, user_id=OWNER_ID):
    return types.SimpleNamespace(
        guild_id=guild_id,
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


class QueueViewTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = import_main_with_stubs()

    def setUp(self):
        self.main.queues.clear()
        self.main.playing.clear()
        self.main.config["fairplay"] = False

    def set_queue(self, count, guild_id=1):
        self.main.queues[guild_id] = [queue_entry(f"t{i}", name=f"Track {i}") for i in range(count)]

    def option_labels(self, view):
        return [option["kwargs"]["label"] for option in view.selection.options]

    def selected_labels(self, view):
        return [option["kwargs"]["label"] for option in view.selection.options if option["kwargs"]["default"]]

    def test_page_count_rounds_up_and_has_at_least_one_page(self):
        size = self.main.PLAYLIST_PAGESIZE
        self.assertEqual(self.main.getPageCount(0), 1)
        self.assertEqual(self.main.getPageCount(1), 1)
        self.assertEqual(self.main.getPageCount(size), 1)
        self.assertEqual(self.main.getPageCount(size + 1), 2)
        self.assertEqual(self.main.getPageCount(2 * size), 2)
        self.assertEqual(self.main.getPageCount(2 * size + 1), 3)

    async def test_queue_numbers_first_page_from_one(self):
        self.set_queue(3)
        ctx = make_ctx()

        await self.main.queue(ctx)

        content = ctx.respond.await_args.args[0]
        lines = content.split("\n")
        self.assertEqual(lines[0], "Tracks in playlist:")
        self.assertTrue(lines[1].startswith("1. "))
        self.assertIn("Track 0", lines[1])
        self.assertTrue(lines[3].startswith("3. "))
        self.assertIn("Track 2", lines[3])
        self.assertEqual(lines[-1], "Page: 1/1")

    async def test_queue_lists_tracks_missing_artists_and_length(self):
        raw = [
            {"Type": "Audio", "Name": "Bare", "Id": "bare"},
            {"Type": "Audio", "Name": "Live", "Id": "live", "Artists": None, "RunTimeTicks": None},
        ]
        self.main.queues[1] = [self.main.getItemEntry(item) for item in raw]
        ctx = make_ctx()

        await self.main.queue(ctx)

        self.assertEqual(ctx.respond.await_args.args[0], "Tracks in playlist:\n1. Bare\n2. Live\nPage: 1/1")

    async def test_queue_number_matches_remove_index(self):
        self.set_queue(3)
        ctx = make_ctx()
        await self.main.queue(ctx)
        first_line = ctx.respond.await_args.args[0].split("\n")[1]

        await self.main.remove(make_ctx(), 1)

        # The number /queue shows for a track is the index /remove takes for it.
        self.assertTrue(first_line.startswith("1. "))
        self.assertIn("Track 0", first_line)
        self.assertEqual([t["Id"] for t in self.main.queues[1]], ["t1", "t2"])

    async def test_queue_with_exact_multiple_of_page_size_has_no_empty_page(self):
        self.set_queue(2 * self.main.PLAYLIST_PAGESIZE)
        ctx = make_ctx()

        await self.main.queue(ctx)

        content = ctx.respond.await_args.args[0]
        self.assertTrue(content.endswith("Page: 1/2"))
        view = ctx.respond.await_args.kwargs["view"]
        self.assertEqual(self.option_labels(view), ["1", "2"])
        self.assertTrue(view.prevButton.disabled)
        self.assertFalse(view.nextButton.disabled)

        interaction = make_interaction()
        await view.nextButton.callback(interaction)

        content = interaction.response.edit_message.await_args.kwargs["content"]
        lines = content.split("\n")
        self.assertEqual(len(lines), self.main.PLAYLIST_PAGESIZE + 2)
        self.assertTrue(lines[1].startswith(f"{self.main.PLAYLIST_PAGESIZE + 1}. "))
        self.assertTrue(lines[-2].startswith(f"{2 * self.main.PLAYLIST_PAGESIZE}. "))
        self.assertEqual(lines[-1], "Page: 2/2")
        self.assertTrue(view.nextButton.disabled)
        self.assertFalse(view.prevButton.disabled)

    async def test_full_single_page_disables_next(self):
        self.set_queue(self.main.PLAYLIST_PAGESIZE)
        ctx = make_ctx()

        await self.main.queue(ctx)

        self.assertTrue(ctx.respond.await_args.args[0].endswith("Page: 1/1"))
        view = ctx.respond.await_args.kwargs["view"]
        self.assertEqual(self.option_labels(view), ["1"])
        self.assertTrue(view.nextButton.disabled)

    async def test_refresh_after_queue_empties_shows_one_page(self):
        self.set_queue(5)
        ctx = make_ctx()
        await self.main.queue(ctx)
        view = ctx.respond.await_args.kwargs["view"]
        self.main.queues[1].clear()

        interaction = make_interaction()
        await view.items[1].callback(interaction)

        content = interaction.response.edit_message.await_args.kwargs["content"]
        self.assertEqual(content.split("\n")[-1], "Page: 1/1")
        self.assertEqual(self.option_labels(view), ["1"])

    async def test_paging_buttons_number_tracks_consistently(self):
        size = self.main.PLAYLIST_PAGESIZE
        self.set_queue(2 * size + 5)
        ctx = make_ctx()
        await self.main.queue(ctx)
        view = ctx.respond.await_args.kwargs["view"]

        interaction = make_interaction()
        await view.nextButton.callback(interaction)
        await view.nextButton.callback(interaction)
        content = interaction.response.edit_message.await_args.kwargs["content"]
        self.assertTrue(content.split("\n")[1].startswith(f"{2 * size + 1}. "))
        self.assertTrue(content.endswith("Page: 3/3"))

        await view.prevButton.callback(interaction)
        await view.prevButton.callback(interaction)
        content = interaction.response.edit_message.await_args.kwargs["content"]
        self.assertEqual(content, ctx.respond.await_args.args[0])
        self.assertIs(interaction.response.edit_message.await_args.kwargs["view"], view)

    async def test_page_selector_reaches_pages_beyond_twenty_five(self):
        size = self.main.PLAYLIST_PAGESIZE
        self.set_queue(40 * size)
        ctx = make_ctx()
        await self.main.queue(ctx)
        view = ctx.respond.await_args.kwargs["view"]

        # First page: window starts at page 1.
        self.assertEqual(self.option_labels(view), [str(i) for i in range(1, 26)])
        self.assertEqual(self.selected_labels(view), ["1"])

        # Selecting page 25 recentres the window so later pages appear.
        interaction = make_interaction()
        view.selection.refresh_state({"values": ["25"]})
        await view.selection.callback(interaction)
        self.assertEqual(view.page, 24)
        self.assertEqual(self.option_labels(view), [str(i) for i in range(13, 38)])
        self.assertEqual(self.selected_labels(view), ["25"])

        view.selection.refresh_state({"values": ["37"]})
        await view.selection.callback(interaction)
        content = interaction.response.edit_message.await_args.kwargs["content"]
        self.assertTrue(content.split("\n")[1].startswith(f"{36 * size + 1}. "))
        self.assertTrue(content.endswith("Page: 37/40"))
        # Near the end the window stays 25 pages wide and ends at the last page.
        self.assertEqual(self.option_labels(view), [str(i) for i in range(16, 41)])
        self.assertEqual(self.selected_labels(view), ["37"])

    async def open_queue(self, count):
        self.set_queue(count)
        ctx = make_ctx()
        await self.main.queue(ctx)
        return ctx.respond.await_args.kwargs["view"]

    def last_footer(self, interaction):
        return interaction.response.edit_message.await_args.kwargs["content"].split("\n")[-1]

    async def test_next_after_queue_shrinks_clamps_to_last_page(self):
        size = self.main.PLAYLIST_PAGESIZE
        view = await self.open_queue(6 * size)
        view.selection.refresh_state({"values": ["6"]})
        await view.selection.callback(make_interaction())
        self.assertEqual(view.page, 5)
        del self.main.queues[1][2 * size:]

        interaction = make_interaction()
        await view.nextButton.callback(interaction)

        self.assertEqual(view.page, 1)
        self.assertEqual(self.last_footer(interaction), "Page: 2/2")
        content = interaction.response.edit_message.await_args.kwargs["content"]
        self.assertTrue(content.split("\n")[1].startswith(f"{size + 1}. "))
        self.assertTrue(view.nextButton.disabled)
        self.assertFalse(view.prevButton.disabled)
        self.assertEqual(self.selected_labels(view), ["2"])

    async def test_prev_after_queue_shrinks_clamps_to_last_page(self):
        size = self.main.PLAYLIST_PAGESIZE
        view = await self.open_queue(6 * size)
        view.selection.refresh_state({"values": ["6"]})
        await view.selection.callback(make_interaction())
        del self.main.queues[1][2 * size:]

        interaction = make_interaction()
        await view.prevButton.callback(interaction)

        # Page 5 no longer exists; the last remaining page is shown.
        self.assertEqual(view.page, 1)
        self.assertEqual(self.last_footer(interaction), "Page: 2/2")
        self.assertTrue(view.nextButton.disabled)

    async def test_refresh_on_late_page_after_queue_empties_shows_page_one(self):
        size = self.main.PLAYLIST_PAGESIZE
        view = await self.open_queue(3 * size)
        await view.nextButton.callback(make_interaction())
        await view.nextButton.callback(make_interaction())
        self.main.queues[1].clear()

        interaction = make_interaction()
        await view.items[1].callback(interaction)

        self.assertEqual(view.page, 0)
        self.assertEqual(self.last_footer(interaction), "Page: 1/1")
        self.assertTrue(view.prevButton.disabled)
        self.assertTrue(view.nextButton.disabled)
        self.assertEqual(self.selected_labels(view), ["1"])

    async def test_prev_on_first_page_of_empty_queue_stays_on_page_one(self):
        view = await self.open_queue(5)
        self.main.queues[1].clear()

        interaction = make_interaction()
        await view.prevButton.callback(interaction)

        self.assertEqual(view.page, 0)
        self.assertEqual(self.last_footer(interaction), "Page: 1/1")

    async def test_other_user_cannot_page_and_invoker_still_can(self):
        view = await self.open_queue(3 * self.main.PLAYLIST_PAGESIZE)

        other = make_interaction(user_id=200)
        await dispatch(view, view.nextButton, other)

        other.response.send_message.assert_awaited_once()
        self.assertTrue(other.response.send_message.await_args.kwargs.get("ephemeral"))
        other.response.edit_message.assert_not_awaited()
        self.assertEqual(view.page, 0)

        owner = make_interaction()
        await dispatch(view, view.nextButton, owner)

        self.assertEqual(view.page, 1)
        self.assertEqual(self.last_footer(owner), "Page: 2/3")
        owner.response.send_message.assert_not_awaited()

    async def test_timeout_shows_disabled_items(self):
        view = await self.open_queue(3 * self.main.PLAYLIST_PAGESIZE)
        view.message = types.SimpleNamespace(edit=mock.AsyncMock())

        # py-cord calls view.on_timeout() with no arguments.
        await view.on_timeout()

        view.message.edit.assert_awaited_once_with(view=view)
        self.assertTrue(all(item.disabled for item in view.items))

    def test_page_selector_range_window(self):
        r = self.main.getPageSelectorRange
        self.assertEqual(list(r(1, 0)), [0])
        self.assertEqual(list(r(25, 24)), list(range(25)))
        self.assertEqual(list(r(26, 0)), list(range(25)))
        self.assertEqual(list(r(26, 25)), list(range(1, 26)))
        self.assertEqual(list(r(100, 50)), list(range(38, 63)))
        for pages in (1, 24, 25, 26, 100):
            for page in range(pages):
                window = r(pages, page)
                self.assertIn(page, window)
                self.assertEqual(len(window), min(pages, 25))


if __name__ == "__main__":
    unittest.main()
