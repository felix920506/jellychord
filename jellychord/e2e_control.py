"""End-to-end test control hook.

Discord does not deliver slash-command interactions created by bots, so an
automated test cannot invoke ``/jellychord play`` itself. This module lets the
E2E driver bot trigger the *real* command handlers by @mentioning the bot in a
dedicated control channel. Everything after command dispatch (queueing,
Jellyfin streaming, ffmpeg, the Discord voice connection) runs unchanged.

The hook is inert unless ``JELLYCHORD_E2E_DRIVER_ID`` is set, and it only
accepts messages from that exact user ID in the configured channel.

Protocol (one message per request / reply)::

    driver -> bot:  <@BOT> e2e <run-nonce> <request-id> <command> [key=value ...]
    bot -> driver:  <@DRIVER> e2e <run-nonce> <request-id> <reply text>

``<command>`` is a subcommand of the command group (``play``, ``skip`` ...) or
one of the built-in hook commands: ``diag`` (voice connection diagnostics),
``versions`` (library versions) and ``ping``. On every ``on_ready`` the bot posts ``... e2e <run-nonce> - ready``.

A subcommand request may also carry ``e2e.as=<user-id>``: the command then
runs as if that user ID had sent it (``ctx.author.id``), with everything else
about the author, voice state included, taken from the driver. The test only
has one Discord account, so this stands in for a second user, e.g. to test
fairplay's per-user rotation. ``.`` can't appear in a slash command option
name, so the key never collides with a real option.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

_log = logging.getLogger(__name__)

PREFIX = 'e2e'
# Hook-level request argument: run the command as another user ID.
AS_USER_ARG = 'e2e.as'
READY_REQUEST_ID = '-'
MAX_MESSAGE_LEN = 2000


@dataclass
class Request:
    nonce: str
    request_id: str
    command: str
    args: dict[str, str] = field(default_factory=dict)


class ParseError(ValueError):
    pass


def parse_request(content: str, bot_id: int) -> Request | None:
    """Parse a control message. Returns None if it isn't addressed to us."""
    try:
        tokens = shlex.split(content)
    except ValueError as exc:
        raise ParseError(f'unparseable request: {exc}') from exc

    mentions = {f'<@{bot_id}>', f'<@!{bot_id}>'}
    if len(tokens) < 2 or tokens[0] not in mentions or tokens[1] != PREFIX:
        return None
    if len(tokens) < 5:
        raise ParseError('expected: <@bot> e2e <nonce> <request-id> <command> [key=value ...]')

    _, _, nonce, request_id, command, *rest = tokens
    args = {}
    for token in rest:
        key, sep, value = token.partition('=')
        if not sep or not key:
            raise ParseError(f'argument {token!r} is not key=value')
        args[key] = value
    return Request(nonce, request_id, command, args)


def format_reply(driver_id: int, nonce: str, request_id: str, text: str) -> str:
    head = f'<@{driver_id}> {PREFIX} {nonce} {request_id} '
    text = text if text else '(empty)'
    room = MAX_MESSAGE_LEN - len(head)
    if len(text) > room:
        text = text[:room - 3] + '...'
    return head + text


def bind_arguments(command, raw_args: dict[str, str]) -> dict[str, Any]:
    """Map key=value strings onto a py-cord SlashCommand's callback parameters,
    applying the same defaults/type conversion that slash dispatch would."""
    kwargs = {}
    known = set()
    for option in command.options:
        known.add(option.name)
        param = option._parameter_name
        if option.name in raw_args:
            kwargs[param] = _convert(option, raw_args[option.name])
        elif option.required:
            raise ParseError(f'missing required option {option.name!r}')
        else:
            kwargs[param] = option.default
    unknown = set(raw_args) - known
    if unknown:
        raise ParseError(f'unknown option(s): {", ".join(sorted(unknown))}')
    return kwargs


def _convert(option, value: str):
    type_name = getattr(option.input_type, 'name', str(option.input_type))
    if type_name == 'integer':
        return int(value)
    if type_name == 'number':
        return float(value)
    if type_name == 'boolean':
        return value.lower() in ('1', 'true', 'yes')
    return value


class StandInAuthor:
    """The driver member, but with another user ID: a second user as far as
    the command handlers can tell. They only use the author's ID and voice."""

    def __init__(self, member, user_id: int):
        self._member = member
        self.id = user_id

    def __getattr__(self, name):
        return getattr(self._member, name)


def parse_user_id(value: str) -> int:
    if not value.isdigit():
        raise ParseError(f'{AS_USER_ARG} must be a numeric user ID, got {value!r}')
    return int(value)


class ControlContext:
    """Duck-typed stand-in for discord.ApplicationContext with the members
    jellychord's command handlers use. Responses go to the control channel."""

    def __init__(self, message, author, reply: Callable):
        self.message = message
        self.guild = message.guild
        self.guild_id = message.guild.id
        self.channel = message.channel
        self.author = author
        self.user = author
        self._reply = reply
        self.responses: list[str] = []

    @property
    def voice_client(self):
        return self.guild.voice_client

    async def defer(self, *args, **kwargs):
        pass

    async def respond(self, content=None, *args, view=None, ephemeral=False, **kwargs):
        text = '' if content is None else str(content)
        self.responses.append(text)
        await self._reply(text, view=view)

    send = respond


def voice_diagnostics(guild) -> dict:
    vc = guild.voice_client
    if vc is None:
        return {'connected': False}
    conn = getattr(vc, '_connection', None)
    dave = getattr(conn, 'dave_session', None)
    is_dave = getattr(vc, 'is_dave_connection', None)
    mode = getattr(conn, 'mode', None)
    endpoint = getattr(conn, 'endpoint', None)
    return {
        'connected': vc.is_connected(),
        'playing': vc.is_playing(),
        'paused': vc.is_paused(),
        'channel_id': str(vc.channel.id) if vc.channel else None,
        'channel_bitrate': getattr(vc.channel, 'bitrate', None),
        # py-cord uses a MISSING sentinel until the mode is negotiated
        'encryption_mode': mode if isinstance(mode, str) and mode else None,
        'dave_protocol_version': getattr(conn, 'dave_protocol_version', None),
        'dave_ready': bool(getattr(dave, 'ready', False)) if dave else False,
        'is_dave_connection': is_dave() if callable(is_dave) else is_dave,
        'voice_server': endpoint.split(':')[0] if isinstance(endpoint, str) and endpoint else None,
    }


def library_versions() -> dict:
    import platform
    from importlib import metadata

    versions = {'python': platform.python_version()}
    for dist in ('py-cord', 'davey', 'PyNaCl', 'aiohttp'):
        try:
            versions[dist] = metadata.version(dist)
        except metadata.PackageNotFoundError:
            versions[dist] = None
    return versions


def install(bot, command_group, *, driver_id: int, channel_id: int, nonce: str):
    commands = {c.name: c for c in command_group.subcommands}

    async def reply(channel, request_id: str, text: str, view=None):
        content = format_reply(driver_id, nonce, request_id, text)
        if view is not None:
            await channel.send(content, view=view)
        else:
            await channel.send(content)

    async def on_ready():
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        await reply(channel, READY_REQUEST_ID, 'ready')

    async def on_message(message):
        if message.author.id != driver_id or message.channel.id != channel_id or message.guild is None:
            return
        try:
            request = parse_request(message.content, bot.user.id)
        except ParseError as exc:
            await message.channel.send(f'<@{driver_id}> {PREFIX} parse-error {exc}')
            return
        if request is None or request.nonce != nonce:
            return

        async def respond(text, view=None):
            await reply(message.channel, request.request_id, text, view)

        try:
            await _dispatch(request, message, respond)
        except Exception as exc:
            _log.exception('E2E command %s failed', request.command)
            await respond(f'error {type(exc).__name__}: {exc}')

    async def _dispatch(request: Request, message, respond):
        if request.command == 'ping':
            await respond('pong')
            return
        if request.command == 'versions':
            await respond('versions ' + json.dumps(library_versions(), sort_keys=True))
            return
        if request.command == 'diag':
            await respond('diag ' + json.dumps(voice_diagnostics(message.guild), sort_keys=True))
            return
        command = commands.get(request.command)
        if command is None:
            await respond(f'error UnknownCommand: {request.command}')
            return
        args = dict(request.args)
        as_user = args.pop(AS_USER_ARG, None)
        kwargs = bind_arguments(command, args)
        author = message.guild.get_member(driver_id) or message.author
        if as_user is not None:
            author = StandInAuthor(author, parse_user_id(as_user))
        ctx = ControlContext(message, author, respond)
        await command.callback(ctx, **kwargs)
        if not ctx.responses:
            await respond('(no response)')

    bot.add_listener(on_ready, 'on_ready')
    bot.add_listener(on_message, 'on_message')
    _log.warning('E2E control hook enabled (driver=%s, channel=%s)', driver_id, channel_id)


def install_from_env(bot, command_group) -> bool:
    driver_id = os.getenv('JELLYCHORD_E2E_DRIVER_ID')
    if not driver_id:
        return False
    log_level = os.getenv('JELLYCHORD_E2E_LOG_LEVEL')
    if log_level:
        logging.basicConfig(
            level=getattr(logging, log_level.upper(), logging.INFO),
            format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        )
    install(
        bot,
        command_group,
        driver_id=int(driver_id),
        channel_id=int(os.environ['JELLYCHORD_E2E_CHANNEL_ID']),
        nonce=os.environ['JELLYCHORD_E2E_NONCE'],
    )
    return True
