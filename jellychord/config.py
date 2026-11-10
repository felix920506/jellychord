"""Loading and validating the bot's settings.

Settings come from config.yml, or from JELLYCHORD_* environment variables
when JELLYCHORD_USE_CONF_FILE is 0. Both modes get the same defaults and the
same checks. A problem raises ConfigError naming every bad setting the way
the operator wrote it (a config.yml key or an environment variable).
"""
import os

import yaml

CONFIG_FILE = 'config.yml'

# setting -> environment variable
ENV_VARS = {
    'discord-token': 'JELLYCHORD_DC_TOKEN',
    'jf-server': 'JELLYCHORD_JF_SERVER',
    'jf-apikey': 'JELLYCHORD_JF_APIKEY',
    'command-group': 'JELLYCHORD_COMMAND_GROUP',
    'search-limit': 'JELLYCHORD_SEARCH_LIMIT',
    'enable-debug': 'JELLYCHORD_ENABLE_DEBUG',
    'debug-server': 'JELLYCHORD_DEBUG_SERVER',
    'fairplay': 'JELLYCHORD_ENABLE_FAIRPLAY',
    'log-level': 'JELLYCHORD_LOG_LEVEL',
}
REQUIRED = ('discord-token', 'jf-server', 'jf-apikey')
DEFAULTS = {
    'command-group': 'jellychord',
    'search-limit': 25,
    'enable-debug': False,
    'fairplay': False,
    'log-level': None,
}


class ConfigError(Exception):
    pass


def _isUnset(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _toInt(value):
    # bool is an int subclass, but "search-limit: true" is a mistake
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _readFile(path: str) -> dict:
    try:
        with open(path, 'r', encoding='utf8') as conffile:
            raw = yaml.safe_load(conffile)
    except FileNotFoundError:
        raise ConfigError(
            f'{path} not found. Copy config.yml.example to {path}, or set '
            'JELLYCHORD_USE_CONF_FILE=0 to configure with environment variables.'
        ) from None
    except yaml.YAMLError as e:
        raise ConfigError(f'{path} is not valid YAML: {e}') from None
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f'{path} must contain "key: value" settings')
    return raw


def _readEnv(environ) -> dict:
    raw = {key: environ.get(var) for key, var in ENV_VARS.items()}
    # Flags are on only when set to 1, as before.
    raw['enable-debug'] = environ.get(ENV_VARS['enable-debug']) == '1'
    raw['fairplay'] = environ.get(ENV_VARS['fairplay']) == '1'
    return raw


def load_config(environ=None, path: str = CONFIG_FILE) -> dict:
    environ = os.environ if environ is None else environ
    fromFile = environ.get('JELLYCHORD_USE_CONF_FILE') != '0'
    raw = _readFile(path) if fromFile else _readEnv(environ)
    name = (lambda key: key) if fromFile else (lambda key: ENV_VARS[key])

    config = dict(raw)
    for key, default in DEFAULTS.items():
        if _isUnset(config.get(key)):
            config[key] = default

    problems = []
    missing = [name(key) for key in REQUIRED if _isUnset(config.get(key))]
    if missing:
        problems.append(f'missing required setting(s): {", ".join(missing)}')

    limit = _toInt(config['search-limit'])
    if limit is None:
        problems.append(f'{name("search-limit")} must be a whole number, got {config["search-limit"]!r}')
    else:
        config['search-limit'] = limit

    if config['enable-debug']:
        if _isUnset(config.get('debug-server')):
            problems.append(f'{name("debug-server")} is required when debug mode is enabled')
        else:
            server = _toInt(config['debug-server'])
            if server is None:
                problems.append(f'{name("debug-server")} must be a Discord server ID, got {config["debug-server"]!r}')
            else:
                config['debug-server'] = server

    if problems:
        source = path if fromFile else 'environment variables'
        raise ConfigError(f'Invalid configuration ({source}): ' + '; '.join(problems))
    return config
