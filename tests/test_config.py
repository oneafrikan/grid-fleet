"""config.py: defaults, schema/interval validation, sanitiser, env overrides, clock."""

import json
import os
import tempfile
import unittest
from unittest import mock

from grid_fleet import config as cfg


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "config.json")

    def env(self, **extra):
        # Only the overrides we set: never inherits the real environment or home config.
        e = {"GRID_FLEET_CONFIG": self.path}
        e.update(extra)
        return e

    def write(self, obj):
        with open(self.path, "w") as fh:
            json.dump(obj, fh)


class TestLoad(ConfigCase):
    def test_missing_file_gives_defaults(self):
        c = cfg.load_config(self.env())
        self.assertEqual(c.account, "default")
        self.assertEqual(c.interval_s, 600)
        self.assertIsNone(c.hub)
        self.assertEqual(c.expected_hosts, ())
        self.assertEqual(c.refresh_s, 5)
        self.assertFalse(c.ascii)
        self.assertIsNone(c.statusline_original)
        # default host name is the sanitised short hostname: never empty, always safe
        self.assertEqual(c.host_name, cfg.sanitize_host_name(c.host_name))
        self.assertNotIn(".", c.host_name[:1])

    def test_full_file(self):
        self.write({
            "schema": 1, "host_name": "alpha", "account": "work", "interval_s": 300,
            "hub": "hub-alias",
            "view": {"expected_hosts": ["alpha", "beta"], "refresh_s": 2, "ascii": True},
            "statusline_original": "echo hi",
        })
        c = cfg.load_config(self.env())
        self.assertEqual(
            (c.host_name, c.account, c.interval_s, c.hub), ("alpha", "work", 300, "hub-alias"))
        self.assertEqual(c.expected_hosts, ("alpha", "beta"))
        self.assertEqual((c.refresh_s, c.ascii), (2, True))
        self.assertEqual(c.statusline_original, "echo hi")

    def test_schema_only_file_is_valid(self):
        self.write({"schema": 1})
        self.assertEqual(cfg.load_config(self.env()).interval_s, 600)

    def test_unknown_schema_is_error(self):
        for bad in (2, 0, "1", None, True):
            self.write({"schema": bad})
            with self.assertRaises(cfg.ConfigError, msg=repr(bad)):
                cfg.load_config(self.env())

    def test_missing_schema_in_existing_file_is_error(self):
        self.write({"account": "x"})
        with self.assertRaises(cfg.ConfigError):
            cfg.load_config(self.env())

    def test_unknown_keys_ignored(self):
        self.write({"schema": 1, "future_thing": [1, 2], "view": {"nope": 1}})
        self.assertEqual(cfg.load_config(self.env()).account, "default")

    def test_bad_interval_is_error(self):
        # Decision: error, not silent fallback (a typo would skew staleness maths).
        for bad in (0, 60, 601, "600", 600.5, True, -300):
            self.write({"schema": 1, "interval_s": bad})
            with self.assertRaises(cfg.ConfigError, msg=repr(bad)):
                cfg.load_config(self.env())

    def test_good_intervals(self):
        for good in (300, 600, 900):
            self.write({"schema": 1, "interval_s": good})
            self.assertEqual(cfg.load_config(self.env()).interval_s, good)

    def test_not_json_and_not_object(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        with self.assertRaises(cfg.ConfigError):
            cfg.load_config(self.env())
        self.write([1, 2])
        with self.assertRaises(cfg.ConfigError):
            cfg.load_config(self.env())

    def test_wrong_value_types(self):
        for body in ({"hub": 5}, {"account": []}, {"view": []}, {"view": {"refresh_s": 0}},
                     {"view": {"expected_hosts": [1]}}, {"view": {"ascii": "yes"}}):
            body["schema"] = 1
            self.write(body)
            with self.assertRaises(cfg.ConfigError, msg=repr(body)):
                cfg.load_config(self.env())

    def test_hostile_host_name_in_file_is_sanitised(self):
        self.write({"schema": 1, "host_name": "../x; rm -rf ~", "view": {"expected_hosts": ["../y"]}})
        c = cfg.load_config(self.env())
        self.assertRegex(c.host_name, r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")
        self.assertEqual(c.expected_hosts, (cfg.sanitize_host_name("../y"),))


class TestSanitiser(unittest.TestCase):
    def test_hostile_name(self):
        out = cfg.sanitize_host_name("../x; rm -rf ~")
        self.assertTrue(out)
        self.assertNotIn("/", out)
        self.assertNotIn(" ", out)
        self.assertFalse(out.startswith("."))
        self.assertRegex(out, r"^[A-Za-z0-9._-]+$")

    def test_clean_names_unchanged(self):
        for ok in ("alpha", "a.b-c_d", "Host01"):
            self.assertEqual(cfg.sanitize_host_name(ok), ok)

    def test_leading_dots_stripped(self):
        self.assertEqual(cfg.sanitize_host_name(".hidden"), "hidden")
        self.assertEqual(cfg.sanitize_host_name("..a"), "a")

    def test_length_cap(self):
        self.assertEqual(len(cfg.sanitize_host_name("a" * 200)), 64)
        # cap applies after stripping leading dots
        self.assertEqual(cfg.sanitize_host_name("." * 10 + "a" * 100), "a" * 64)

    def test_fallback_when_nothing_left(self):
        for raw in ("", ".", "...", None, 5):
            self.assertEqual(cfg.sanitize_host_name(raw), cfg.HOST_NAME_FALLBACK)

    def test_non_ascii_and_control_chars(self):
        out = cfg.sanitize_host_name("hé\x00\n/ost")
        self.assertRegex(out, r"^[A-Za-z0-9._-]+$")


class TestEnvOverrides(ConfigCase):
    def test_all_overrides(self):
        e = self.env(
            GRID_FLEET_STATE_DIR="/s", GRID_FLEET_CLAUDE_DIR="/c", GRID_FLEET_CLAUDE_BIN="/b/claude")
        c = cfg.load_config(e)
        self.assertEqual((c.config_path, c.state_dir, c.claude_dir, c.claude_bin),
                         (self.path, "/s", "/c", "/b/claude"))

    def test_xdg_and_home_defaults(self):
        e = {"XDG_CONFIG_HOME": "/xc", "XDG_STATE_HOME": "/xs"}
        self.assertEqual(cfg.config_path(e), "/xc/grid-fleet/config.json")
        self.assertEqual(cfg.state_dir(e), "/xs/grid-fleet")
        home = os.path.expanduser("~")
        self.assertEqual(cfg.config_path({}), os.path.join(home, ".config/grid-fleet/config.json"))
        self.assertEqual(cfg.state_dir({}), os.path.join(home, ".local/state/grid-fleet"))

    def test_relative_xdg_ignored(self):
        home = os.path.expanduser("~")
        self.assertEqual(cfg.state_dir({"XDG_STATE_HOME": "rel"}),
                         os.path.join(home, ".local/state/grid-fleet"))

    def test_claude_dir_precedence(self):
        self.assertEqual(cfg.claude_dir({"GRID_FLEET_CLAUDE_DIR": "/a", "CLAUDE_CONFIG_DIR": "/b"}), "/a")
        self.assertEqual(cfg.claude_dir({"CLAUDE_CONFIG_DIR": "/b"}), "/b")
        self.assertEqual(cfg.claude_dir({}), os.path.join(os.path.expanduser("~"), ".claude"))

    def test_claude_bin_default(self):
        self.assertEqual(cfg.claude_bin({}), "claude")


class TestNow(unittest.TestCase):
    def test_fixed_clock(self):
        with mock.patch.dict(os.environ, {"GRID_FLEET_NOW": "1792580580"}):
            self.assertEqual(cfg.now(), 1792580580)

    def test_real_clock_when_unset(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("GRID_FLEET_NOW", None)
            self.assertGreater(cfg.now(), 1700000000)

    def test_bad_value(self):
        with mock.patch.dict(os.environ, {"GRID_FLEET_NOW": "yesterday"}):
            with self.assertRaises(cfg.ConfigError):
                cfg.now()


if __name__ == "__main__":
    unittest.main()
