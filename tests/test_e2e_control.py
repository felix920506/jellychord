import asyncio
import json
import types
import unittest

from jellychord import e2e_control
from jellychord.e2e_control import ParseError, bind_arguments, format_reply, parse_driver_ids, parse_request

BOT_ID = 111
DRIVER_ID = 222
SECOND_ID = 555
CHANNEL_ID = 333
NONCE = 'run42'


def option(name, param=None, required=False, default=None, input_type='string'):
    return types.SimpleNamespace(
        name=name,
        _parameter_name=param or name,
        required=required,
        default=default,
        input_type=types.SimpleNamespace(name=input_type),
    )


def command(name, callback, options):
    return types.SimpleNamespace(name=name, callback=callback, options=options)


class ParseRequestTests(unittest.TestCase):
    def test_parses_quoted_arguments(self):
        req = parse_request(f'<@{BOT_ID}> e2e run42 r1 play term="Tone A" when=now', BOT_ID)
        self.assertEqual(req.nonce, 'run42')
        self.assertEqual(req.request_id, 'r1')
        self.assertEqual(req.command, 'play')
        self.assertEqual(req.args, {'term': 'Tone A', 'when': 'now'})

    def test_accepts_nickname_mention(self):
        req = parse_request(f'<@!{BOT_ID}> e2e n r skip', BOT_ID)
        self.assertEqual(req.command, 'skip')

    def test_ignores_messages_not_addressed_to_bot(self):
        self.assertIsNone(parse_request('hello there', BOT_ID))
        self.assertIsNone(parse_request(f'<@999> e2e n r skip', BOT_ID))
        self.assertIsNone(parse_request(f'<@{BOT_ID}> hi', BOT_ID))

    def test_rejects_malformed_requests(self):
        with self.assertRaises(ParseError):
            parse_request(f'<@{BOT_ID}> e2e n r', BOT_ID)
        with self.assertRaises(ParseError):
            parse_request(f'<@{BOT_ID}> e2e n r play novalue', BOT_ID)
        with self.assertRaises(ParseError):
            parse_request(f'<@{BOT_ID}> e2e n r play term="unterminated', BOT_ID)


class FormatReplyTests(unittest.TestCase):
    def test_formats_reply_header(self):
        self.assertEqual(format_reply(DRIVER_ID, 'n', 'r1', 'ok'), f'<@{DRIVER_ID}> e2e n r1 ok')

    def test_truncates_to_discord_limit(self):
        reply = format_reply(DRIVER_ID, 'n', 'r1', 'x' * 5000)
        self.assertEqual(len(reply), 2000)
        self.assertTrue(reply.endswith('...'))

    def test_empty_text_is_visible(self):
        self.assertTrue(format_reply(DRIVER_ID, 'n', 'r', '').endswith('(empty)'))


class BindArgumentsTests(unittest.TestCase):
    def setUp(self):
        self.play = command('play', None, [
            option('term', required=True),
            option('type', param='searchtype'),
            option('when', default='last'),
        ])

    def test_applies_defaults_and_parameter_names(self):
        kwargs = bind_arguments(self.play, {'term': 'x', 'type': 'Album'})
        self.assertEqual(kwargs, {'term': 'x', 'searchtype': 'Album', 'when': 'last'})

    def test_converts_integers(self):
        remove = command('remove', None, [option('index', required=True, input_type='integer')])
        self.assertEqual(bind_arguments(remove, {'index': '3'}), {'index': 3})

    def test_missing_required_option(self):
        with self.assertRaises(ParseError):
            bind_arguments(self.play, {})

    def test_unknown_option(self):
        with self.assertRaises(ParseError):
            bind_arguments(self.play, {'term': 'x', 'bogus': '1'})


class ParseDriverIdsTests(unittest.TestCase):
    def test_parses_comma_separated_ids(self):
        self.assertEqual(parse_driver_ids('555, 666,'), [555, 666])

    def test_unset_or_blank_is_empty(self):
        self.assertEqual(parse_driver_ids(None), [])
        self.assertEqual(parse_driver_ids(''), [])


class FakeChannel:
    def __init__(self):
        self.id = CHANNEL_ID
        self.sent = []

    async def send(self, content, view=None):
        self.sent.append((content, view))


class FakeBot:
    def __init__(self, channel):
        self.user = types.SimpleNamespace(id=BOT_ID)
        self.listeners = {}
        self.channel = channel

    def add_listener(self, func, name):
        self.listeners[name] = func

    def get_channel(self, channel_id):
        return self.channel if channel_id == CHANNEL_ID else None


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.channel = FakeChannel()
        self.bot = FakeBot(self.channel)
        self.calls = []
        self.voice_client = None

        async def play(ctx, term, searchtype, when):
            self.calls.append((ctx, term, searchtype, when))
            await ctx.defer(invisible=True)
            await ctx.respond(f'Playing {term}', view='a-view')

        async def silent(ctx):
            pass

        async def broken(ctx):
            raise RuntimeError('boom')

        group = types.SimpleNamespace(subcommands=[
            command('play', play, [option('term', required=True), option('type', param='searchtype'), option('when', default='last')]),
            command('silent', silent, []),
            command('broken', broken, []),
        ])
        e2e_control.install(self.bot, group, driver_id=DRIVER_ID, channel_id=CHANNEL_ID, nonce=NONCE,
                            extra_driver_ids=[SECOND_ID])

        self.member = types.SimpleNamespace(id=DRIVER_ID, voice='in-voice')
        self.second = types.SimpleNamespace(id=SECOND_ID, voice=None)
        members = {DRIVER_ID: self.member, SECOND_ID: self.second}
        self.guild = types.SimpleNamespace(
            id=444,
            voice_client=None,
            get_member=members.get,
        )

    def message(self, content, author_id=DRIVER_ID, channel=None):
        return types.SimpleNamespace(
            content=content,
            author=types.SimpleNamespace(id=author_id),
            channel=channel or self.channel,
            guild=self.guild,
        )

    def send(self, content, **kwargs):
        asyncio.run(self.bot.listeners['on_message'](self.message(content, **kwargs)))

    def test_ready_announcement(self):
        asyncio.run(self.bot.listeners['on_ready']())
        self.assertEqual(self.channel.sent, [(f'<@{DRIVER_ID}> e2e {NONCE} - ready', None)])

    def test_dispatches_to_command_callback(self):
        self.send(f'<@{BOT_ID}> e2e {NONCE} r1 play term="Tone A"')
        ctx, term, searchtype, when = self.calls[0]
        self.assertEqual((term, searchtype, when), ('Tone A', None, 'last'))
        self.assertIs(ctx.author, self.member)
        self.assertEqual(ctx.guild_id, 444)
        self.assertEqual(self.channel.sent, [(f'<@{DRIVER_ID}> e2e {NONCE} r1 Playing Tone A', 'a-view')])

    def test_voice_client_is_read_live_from_guild(self):
        self.send(f'<@{BOT_ID}> e2e {NONCE} r1 play term=x')
        ctx = self.calls[0][0]
        self.assertIsNone(ctx.voice_client)
        self.guild.voice_client = 'vc'
        self.assertEqual(ctx.voice_client, 'vc')

    def test_ignores_other_authors_channels_and_nonces(self):
        self.send(f'<@{BOT_ID}> e2e {NONCE} r1 play term=x', author_id=999)
        other = FakeChannel()
        other.id = 1
        self.send(f'<@{BOT_ID}> e2e {NONCE} r1 play term=x', channel=other)
        self.send(f'<@{BOT_ID}> e2e stale-run r1 play term=x')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.channel.sent, [])
        self.assertEqual(other.sent, [])

    def test_reports_command_without_response(self):
        self.send(f'<@{BOT_ID}> e2e {NONCE} r2 silent')
        self.assertEqual(self.channel.sent[0][0], f'<@{DRIVER_ID}> e2e {NONCE} r2 (no response)')

    def test_reports_exceptions(self):
        self.send(f'<@{BOT_ID}> e2e {NONCE} r3 broken')
        self.assertEqual(self.channel.sent[0][0], f'<@{DRIVER_ID}> e2e {NONCE} r3 error RuntimeError: boom')

    def test_reports_unknown_command_and_bad_args(self):
        self.send(f'<@{BOT_ID}> e2e {NONCE} r4 nope')
        self.send(f'<@{BOT_ID}> e2e {NONCE} r5 play')
        self.assertIn('error UnknownCommand: nope', self.channel.sent[0][0])
        self.assertIn("error ParseError: missing required option 'term'", self.channel.sent[1][0])

    def test_runs_command_as_the_extra_driver_with_its_own_voice_state(self):
        self.send(f'<@{BOT_ID}> e2e {NONCE} r1 play term=x', author_id=SECOND_ID)
        ctx, term, searchtype, when = self.calls[0]
        self.assertEqual((term, searchtype, when), ('x', None, 'last'))
        self.assertIs(ctx.author, self.second)
        self.assertIsNone(ctx.author.voice)
        # the reply goes to whoever asked
        self.assertEqual(self.channel.sent, [(f'<@{SECOND_ID}> e2e {NONCE} r1 Playing x', 'a-view')])

    def test_falls_back_to_the_message_author_when_the_member_is_not_cached(self):
        # The bot caches members only while they are in voice. For a guild
        # message py-cord makes the author a Member from the message's member
        # data, and Member.voice reads the guild's voice states.
        author = types.SimpleNamespace(id=SECOND_ID, voice=None)
        self.guild.get_member = lambda uid: None
        message = self.message(f'<@{BOT_ID}> e2e {NONCE} r1 play term=x', author_id=SECOND_ID)
        message.author = author
        asyncio.run(self.bot.listeners['on_message'](message))
        self.assertIs(self.calls[0][0].author, author)
        self.assertTrue(self.channel.sent[0][0].startswith(f'<@{SECOND_ID}> e2e {NONCE} r1 '))

    def test_parse_errors_mention_the_sender(self):
        self.send(f'<@{BOT_ID}> e2e', author_id=SECOND_ID)
        self.assertTrue(self.channel.sent[0][0].startswith(f'<@{SECOND_ID}> e2e parse-error '))

    def test_builtin_drivers_lists_accepted_ids(self):
        self.send(f'<@{BOT_ID}> e2e {NONCE} r1 drivers', author_id=SECOND_ID)
        reply = self.channel.sent[0][0]
        self.assertTrue(reply.startswith(f'<@{SECOND_ID}> e2e {NONCE} r1 drivers '))
        self.assertEqual(json.loads(reply.split(' drivers ', 1)[1]), [str(DRIVER_ID), str(SECOND_ID)])

    def test_e2e_as_is_no_longer_accepted(self):
        self.send(f'<@{BOT_ID}> e2e {NONCE} r1 play term=x e2e.as=555')
        self.assertEqual(self.calls, [])
        self.assertIn('error ParseError: unknown option(s): e2e.as', self.channel.sent[0][0])

    def test_builtin_ping_and_diag(self):
        self.send(f'<@{BOT_ID}> e2e {NONCE} r6 ping')
        self.send(f'<@{BOT_ID}> e2e {NONCE} r7 diag')
        self.assertTrue(self.channel.sent[0][0].endswith('r6 pong'))
        diag = json.loads(self.channel.sent[1][0].split(' diag ', 1)[1])
        self.assertEqual(diag, {'connected': False})


if __name__ == '__main__':
    unittest.main()
