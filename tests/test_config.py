import contextlib
import importlib
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

from tests.test_main_helpers import make_discord_stub

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "config.yml.example"

ENV_REQUIRED = {
    "JELLYCHORD_USE_CONF_FILE": "0",
    "JELLYCHORD_DC_TOKEN": "discord-token",
    "JELLYCHORD_JF_SERVER": "https://jellyfin.example.com/",
    "JELLYCHORD_JF_APIKEY": "api-key",
}
FILE_REQUIRED = (
    'discord-token: "discord-token"\n'
    'jf-server: "https://jellyfin.example.com/"\n'
    'jf-apikey: "api-key"\n'
)


class ImportMainTestCase(unittest.TestCase):
    """Imports main the way the bot starts: settings from the given environment
    only, and config.yml from a temporary working directory."""

    def import_main(self, env, config_yml=None):
        discord, discord_ext = make_discord_stub()
        with tempfile.TemporaryDirectory() as tmp:
            if config_yml is not None:
                pathlib.Path(tmp, "config.yml").write_text(config_yml, encoding="utf8")
            sys.modules.pop("main", None)
            self.addCleanup(sys.modules.pop, "main", None)
            # basicConfig is patched so these imports leave the test run's logging alone.
            with contextlib.chdir(tmp), mock.patch("logging.basicConfig"), mock.patch.dict(os.environ, env, clear=True), mock.patch.dict(
                sys.modules, {"discord": discord, "discord.ext": discord_ext}
            ):
                return importlib.import_module("main")

    def assert_config_exit(self, env, config_yml=None, *names):
        with self.assertRaises(SystemExit) as cm:
            self.import_main(env, config_yml)
        message = cm.exception.code
        # sys.exit(str) prints the message and exits with status 1
        self.assertIsInstance(message, str)
        self.assertIn("Invalid configuration", message)
        for name in names:
            self.assertIn(name, message)
        return message


class FileConfigTests(ImportMainTestCase):
    def test_example_config_loads_the_same_with_safe_load(self):
        text = EXAMPLE.read_text(encoding="utf8")
        self.assertEqual(yaml.safe_load(text), yaml.load(text, yaml.loader.Loader))

    def test_example_config_starts_the_bot(self):
        main = self.import_main({}, EXAMPLE.read_text(encoding="utf8"))

        self.assertEqual(main.bot.run_token, "Discord Bot Token Here")
        self.assertEqual(main.LIMIT, 25)
        self.assertFalse(main.DEBUG)
        self.assertEqual(main.bot.groups, [(("jellychord",), {})])

    def test_python_tags_are_rejected_not_constructed(self):
        # The full loader would build an int here; safe_load refuses the tag.
        config = FILE_REQUIRED + 'search-limit: !!python/object/apply:builtins.int ["7"]\n'
        with self.assertRaises(SystemExit) as cm:
            self.import_main({}, config)
        self.assertIn("config.yml is not valid YAML", cm.exception.code)

    def test_optional_keys_default_like_env_mode(self):
        main = self.import_main({}, FILE_REQUIRED)

        self.assertEqual(main.LIMIT, 25)
        self.assertFalse(main.DEBUG)
        self.assertFalse(main.config["fairplay"])
        self.assertEqual(main.bot.groups, [(("jellychord",), {})])
        self.assertEqual(main.bot.run_token, "discord-token")

    def test_missing_required_settings_are_named(self):
        message = self.assert_config_exit({}, 'command-group: "jellychord"\n', "config.yml",
                                          "discord-token", "jf-server", "jf-apikey")
        self.assertNotIn("JELLYCHORD_", message)

    def test_empty_required_setting_counts_as_missing(self):
        config = FILE_REQUIRED.replace('jf-apikey: "api-key"', 'jf-apikey: ""')
        message = self.assert_config_exit({}, config, "jf-apikey")
        self.assertNotIn("discord-token", message)

    def test_empty_file_names_every_required_setting(self):
        self.assert_config_exit({}, "", "discord-token", "jf-server", "jf-apikey")

    def test_debug_without_server_is_rejected(self):
        self.assert_config_exit({}, FILE_REQUIRED + "enable-debug: true\n", "debug-server")

    def test_debug_server_is_used(self):
        main = self.import_main({}, FILE_REQUIRED + "enable-debug: true\ndebug-server: 1234\n")

        self.assertTrue(main.DEBUG)
        self.assertEqual(main.DEBUG_SERVER, 1234)
        self.assertIn((("jellychord",), {"guild_ids": [1234]}), main.bot.groups)

    def test_non_numeric_search_limit_is_rejected(self):
        self.assert_config_exit({}, FILE_REQUIRED + "search-limit: lots\n", "search-limit")

    def test_whole_float_search_limit_is_accepted(self):
        main = self.import_main({}, FILE_REQUIRED + "search-limit: 10.0\n")
        self.assertEqual(main.LIMIT, 10)
        self.assertIsInstance(main.LIMIT, int)

    def test_large_float_search_limit_is_clamped(self):
        main = self.import_main({}, FILE_REQUIRED + "search-limit: 100.0\n")
        self.assertEqual(main.LIMIT, 25)

    def test_quoted_search_limit_is_accepted(self):
        main = self.import_main({}, FILE_REQUIRED + 'search-limit: "10"\n')
        self.assertEqual(main.LIMIT, 10)

    def test_fractional_or_boolean_search_limit_is_rejected(self):
        for value in ("10.5", "true"):
            with self.subTest(value=value):
                self.assert_config_exit({}, FILE_REQUIRED + f"search-limit: {value}\n", "search-limit")

    def test_flag_words_are_read_as_booleans(self):
        cases = {
            "true": True, "yes": True, "on": True, "1": True, "2": True,
            "false": False, "no": False, "off": False, "0": False,
            '"true"': True, '"Yes"': True, '"ON"': True, '"1"': True, '" true "': True,
            '"false"': False, '"No"': False, '"off"': False, '"0"': False, '"FALSE"': False,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                main = self.import_main({}, FILE_REQUIRED + f"fairplay: {value}\nenable-debug: {value}\ndebug-server: 1234\n")
                self.assertIs(main.config["fairplay"], expected)
                self.assertIs(main.DEBUG, expected)

    def test_quoted_false_debug_needs_no_server(self):
        main = self.import_main({}, FILE_REQUIRED + 'enable-debug: "false"\n')
        self.assertFalse(main.DEBUG)
        self.assertEqual(main.bot.groups, [(("jellychord",), {})])

    def test_unknown_flag_value_is_rejected(self):
        message = self.assert_config_exit({}, FILE_REQUIRED + "fairplay: sometimes\nenable-debug: [1]\n",
                                          "fairplay", "enable-debug")
        self.assertNotIn("debug-server", message)

    def test_log_level_is_stripped(self):
        with self.assertNoLogs("jellychord", "WARNING"):
            main = self.import_main({}, FILE_REQUIRED + 'log-level: " debug "\n')
        self.assertEqual(main.LOG_LEVEL, "DEBUG")

    def test_missing_file_is_a_clear_error(self):
        with self.assertRaises(SystemExit) as cm:
            self.import_main({})
        self.assertIn("config.yml not found", cm.exception.code)
        self.assertIn("JELLYCHORD_USE_CONF_FILE=0", cm.exception.code)


class EnvConfigTests(ImportMainTestCase):
    def test_required_settings_only(self):
        main = self.import_main(ENV_REQUIRED)

        self.assertEqual(main.LIMIT, 25)
        self.assertFalse(main.DEBUG)
        self.assertFalse(main.config["fairplay"])
        self.assertEqual(main.bot.groups, [(("jellychord",), {})])
        self.assertEqual(main.bot.run_token, "discord-token")

    def test_missing_required_settings_are_named(self):
        env = {"JELLYCHORD_USE_CONF_FILE": "0", "JELLYCHORD_JF_SERVER": ""}
        self.assert_config_exit(env, None, "environment variables", "JELLYCHORD_DC_TOKEN",
                                "JELLYCHORD_JF_SERVER", "JELLYCHORD_JF_APIKEY")

    def test_one_missing_setting_is_named_alone(self):
        env = dict(ENV_REQUIRED)
        del env["JELLYCHORD_DC_TOKEN"]
        message = self.assert_config_exit(env, None, "JELLYCHORD_DC_TOKEN")
        self.assertNotIn("JELLYCHORD_JF_APIKEY", message)

    def test_debug_without_server_is_rejected(self):
        env = {**ENV_REQUIRED, "JELLYCHORD_ENABLE_DEBUG": "1"}
        self.assert_config_exit(env, None, "JELLYCHORD_DEBUG_SERVER")

    def test_non_numeric_debug_server_is_rejected(self):
        env = {**ENV_REQUIRED, "JELLYCHORD_ENABLE_DEBUG": "1", "JELLYCHORD_DEBUG_SERVER": "my-server"}
        self.assert_config_exit(env, None, "JELLYCHORD_DEBUG_SERVER")

    def test_debug_server_is_used(self):
        env = {**ENV_REQUIRED, "JELLYCHORD_ENABLE_DEBUG": "1", "JELLYCHORD_DEBUG_SERVER": "1234"}
        main = self.import_main(env)

        self.assertEqual(main.DEBUG_SERVER, 1234)

    def test_non_numeric_search_limit_is_rejected(self):
        env = {**ENV_REQUIRED, "JELLYCHORD_SEARCH_LIMIT": "lots"}
        self.assert_config_exit(env, None, "JELLYCHORD_SEARCH_LIMIT")

    def test_log_level_is_stripped(self):
        main = self.import_main({**ENV_REQUIRED, "JELLYCHORD_LOG_LEVEL": " debug "})
        self.assertEqual(main.LOG_LEVEL, "DEBUG")

    def test_all_problems_are_reported_together(self):
        env = {"JELLYCHORD_USE_CONF_FILE": "0", "JELLYCHORD_SEARCH_LIMIT": "lots", "JELLYCHORD_ENABLE_DEBUG": "1"}
        self.assert_config_exit(env, None, "JELLYCHORD_DC_TOKEN", "JELLYCHORD_SEARCH_LIMIT",
                                "JELLYCHORD_DEBUG_SERVER")


class StartupExitTests(unittest.TestCase):
    def test_bad_config_exits_non_zero_with_message(self):
        # Runs the real entry point; it exits before touching Discord.
        env = {"PATH": os.environ.get("PATH", ""), "JELLYCHORD_USE_CONF_FILE": "0"}
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, str(REPO_ROOT / "main.py")],
                cwd=tmp, env=env, capture_output=True, text=True, timeout=120,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("JELLYCHORD_DC_TOKEN", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
