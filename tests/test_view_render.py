"""view.py (tasks 4.2-4.4): the pure render function, and the curses loop against a fake screen.

No test here needs a terminal. The curses layer is driven by FakeScreen/FakeCurses, which
record what would be drawn. Real-terminal behaviour (colours, resize, restore on exit) is
task 4.5 (HUMAN) and is NOT covered by these tests.
"""

import io
import os
import re
import socket
import sys
import tempfile
import types
import unittest
from unittest import mock

from grid_fleet import config as cfg
from grid_fleet import hub, view
from helpers import synthetic_snapshot as syn
from helpers.synthetic_snapshot import NOW, NULL, make_snapshot, tokens_window, write_inbox_file

UTF8_ENV = {"LANG": "en_US.UTF-8"}


class RenderCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = self.tmp.name

    def put(self, host, age_s=180, **kw):
        write_inbox_file(self.state_dir, host + ".json", make_snapshot(host, **kw), NOW - age_s)

    def hubstate(self, expected=()):
        return hub.read_hub(self.state_dir, NOW, expected)

    def lines(self, expected=(), width=80, height=24, ascii_mode=False, color=True):
        return view.render(self.hubstate(expected), width, height, ascii_mode, color)

    def text(self, **kw):
        return view.plain_text(self.lines(**kw))

    @staticmethod
    def row(texts, name):
        """The first rendered row whose first cell (after the border) is `name`."""
        for t in texts:
            cells = t.strip("│|").split()
            if cells and cells[0] == name:
                return t
        raise AssertionError("no row for %r in:\n%s" % (name, "\n".join(texts)))

    @staticmethod
    def span_tag(lines, needle):
        """Tag of the first span whose text contains `needle`."""
        for ln in lines:
            for text, tag in ln:
                if needle in text:
                    return tag
        raise AssertionError("no span containing %r" % needle)


class TestFormatters(unittest.TestCase):
    def test_fmt_tokens(self):
        cases = [
            (0, "0"), (999, "999"), (1000, "1k"), (1500, "1.5k"), (400000, "400k"),
            (412000, "412k"), (412300, "412.3k"), (999999, "1M"), (3100000, "3.1M"),
            (55000000, "55M"), (420000000, "420M"), (1200000000, "1.2G"),
            (123000000000, "123G"),
        ]
        for n, want in cases:
            self.assertEqual(view.fmt_tokens(n), want, n)

    def test_fmt_tokens_unknown(self):
        self.assertEqual(view.fmt_tokens(None), "unknown")

    def test_fmt_age(self):
        cases = [(0, "<1m"), (59, "<1m"), (60, "1m"), (180, "3m"), (2460, "41m"),
                 (3599, "59m"), (3600, "1h00m"), (7500, "2h05m"), (86400, "1d00h"),
                 (3 * 86400 + 4 * 3600, "3d04h")]
        for s, want in cases:
            self.assertEqual(view.fmt_age(s), want, s)

    def test_locale_detection(self):
        self.assertTrue(view.locale_is_utf8({"LANG": "en_US.UTF-8"}))
        self.assertTrue(view.locale_is_utf8({"LANG": "C.utf8"}))
        self.assertFalse(view.locale_is_utf8({"LANG": "C"}))
        self.assertFalse(view.locale_is_utf8({}))
        # LC_ALL beats LANG
        self.assertFalse(view.locale_is_utf8({"LC_ALL": "C", "LANG": "en_US.UTF-8"}))
        self.assertTrue(view.locale_is_utf8({"LC_ALL": "en_US.UTF-8", "LANG": "C"}))

    def test_ascii_when_config_or_non_utf8(self):
        self.assertTrue(view.use_ascii(True, UTF8_ENV))
        self.assertTrue(view.use_ascii(False, {"LANG": "C"}))
        self.assertFalse(view.use_ascii(False, UTF8_ENV))

    def test_no_color_env(self):
        self.assertFalse(view.use_color({"NO_COLOR": "1"}))
        self.assertTrue(view.use_color({"NO_COLOR": ""}))  # empty = unset, per no-color.org
        self.assertTrue(view.use_color({}))


class TestHostsTable(RenderCase):
    def test_fresh_host_shows_busy_and_waiting(self):
        self.put("alpha", sessions={"busy": 2, "waiting": 1})
        cells = self.row(self.text(), "alpha").strip("│ ").split()
        self.assertEqual(cells[:3], ["alpha", "2", "1"])

    def test_output_is_the_headline_cache_is_muted(self):
        # 400 thousand output tokens, 50 million cache-read tokens.
        self.put("alpha", tokens={
            "last_24h": tokens_window(output=400000, cache_read=50000000),
            "last_7d": tokens_window(output=900000, cache_read=60000000),
        })
        lines = self.lines()
        texts = view.plain_text(lines)
        i = texts.index(self.row(texts, "alpha"))
        main_row, muted_row = texts[i], texts[i + 1]
        self.assertIn("400k", main_row)
        self.assertNotIn("50M", main_row)
        self.assertIn("in+cache", muted_row)
        self.assertIn("50M", muted_row)
        self.assertEqual(self.span_tag(lines, "in+cache"), view.DIM)
        # the total row carries output only, never the cache figure
        self.assertNotIn("50M", self.row(texts, "total"))

    def test_null_sessions_render_unknown_not_zero(self):
        self.put("alpha", sessions=NULL)
        lines = self.lines()
        cells = self.row(view.plain_text(lines), "alpha").strip("│ ").split()
        self.assertEqual(cells[:3], ["alpha", "unknown", "unknown"])
        self.assertEqual(self.span_tag(lines, "unknown"), view.GREY)
        self.assertIn("412k", cells)  # the other sections are still shown

    def test_absurdly_large_counts_do_not_crash_the_view(self):
        # 10**400 is well under the 256 KiB file cap but overflows float division in
        # fmt_tokens. The reader must treat it as unknown; other hosts still render.
        huge = 10 ** 400
        self.put("alpha", tokens={
            "last_24h": tokens_window(huge, huge, huge, huge),
            "last_7d": tokens_window(huge, huge, huge, huge),
            "by_day": [],
        })
        self.put("beta")
        texts = self.text()  # must not raise
        cells = self.row(texts, "alpha").strip("│ ").split()
        self.assertEqual(cells[3:5], ["unknown", "unknown"])
        self.assertEqual(self.row(texts, "beta").strip("│ ").split()[3], "412k")

    def test_null_tokens_render_unknown_and_drop_muted_line(self):
        self.put("alpha", tokens=NULL)
        texts = self.text()
        cells = self.row(texts, "alpha").strip("│ ").split()
        self.assertEqual(cells[:5], ["alpha", "2", "1", "unknown", "unknown"])
        self.assertFalse(any("in+cache" in t for t in texts))

    def test_stale_host_is_grey_unknown_with_age(self):
        self.put("beta", age_s=41 * 60, interval_s=600)
        lines = self.lines()
        row = self.row(view.plain_text(lines), "beta")
        self.assertIn("unknown", row)
        self.assertIn("stale 41m", row)
        self.assertEqual(self.span_tag(lines, "unknown"), view.GREY)
        self.assertEqual(self.span_tag(lines, "stale 41m"), view.GREY)
        # and none of its numbers are shown
        self.assertNotIn("412k", row)

    def test_stale_host_excluded_from_total(self):
        self.put("alpha", sessions={"busy": 2, "waiting": 1})
        self.put("beta", age_s=41 * 60, sessions={"busy": 9, "waiting": 7})
        texts = self.text()
        total = self.row(texts, "total").strip("│ ").split()
        self.assertEqual(total[:5], ["total", "2", "1", "412k", "3.1M"])
        self.assertTrue(any("1 of 2 hosts fresh" in t for t in texts))

    def test_never_reported(self):
        self.put("alpha")
        texts = self.text(expected=["alpha", "beta"])
        self.assertIn("unknown (never reported)", self.row(texts, "beta"))

    def test_clock_skew_independence(self):
        # Spoke clock wrong by an hour either way; only the file mtime counts.
        self.put("alpha", age_s=30, collected_at=syn.COLLECTED_AT - 3600)
        self.put("beta", age_s=30, collected_at=NOW + 3600)
        self.put("gamma", age_s=41 * 60, collected_at=NOW)  # looks fresh by its own clock
        texts = self.text()
        self.assertNotIn("stale", self.row(texts, "alpha"))
        self.assertNotIn("stale", self.row(texts, "beta"))
        self.assertIn("stale 41m", self.row(texts, "gamma"))

    def test_partial_total_is_marked(self):
        self.put("alpha", sessions={"busy": 2, "waiting": 1})
        self.put("beta", sessions=NULL)
        total = self.row(self.text(), "total").strip("│ ").split()
        self.assertEqual(total[1:3], ["2+", "1+"])

    def test_total_unknown_with_no_fresh_hosts(self):
        self.put("beta", age_s=41 * 60)
        total = self.row(self.text(), "total").strip("│ ").split()
        self.assertEqual(total[1:5], ["unknown"] * 4)

    def test_empty_inbox(self):
        texts = self.text()
        self.assertTrue(any("no snapshots in the inbox yet" in t for t in texts))
        self.assertTrue(any("0 of 0 hosts fresh" in t for t in texts))

    def test_busy_waiting_tags_only_when_positive(self):
        self.put("alpha", sessions={"busy": 2, "waiting": 0})
        lines = self.lines()
        row = next(ln for ln in lines if "alpha" in "".join(t for t, _ in ln))
        tags = {text.strip(): tag for text, tag in row}
        self.assertEqual(tags["2"], view.BUSY)
        self.assertEqual(tags["0"], view.PLAIN)

    def test_no_session_details_or_paths_leak(self):
        snap = make_snapshot("alpha")
        snap["cwd"] = "/secret/place"
        snap["sessions"]["name"] = "my-secret-session"
        write_inbox_file(self.state_dir, "alpha.json", snap, NOW - 60)
        out = "\n".join(self.text())
        self.assertNotIn("secret", out)


class TestFooter(RenderCase):
    def test_one_corrupt_file_footer(self):
        self.put("alpha")
        write_inbox_file(self.state_dir, "bad.json", "{oops", NOW)
        texts = self.text()
        self.assertIn("alpha", "\n".join(texts))  # the valid host still renders
        self.assertTrue(any("1 file skipped" in t for t in texts))

    def test_plural(self):
        write_inbox_file(self.state_dir, "a.json", "{", NOW)
        write_inbox_file(self.state_dir, "b.json", "{", NOW)
        self.assertTrue(any("2 files skipped" in t for t in self.text()))

    def test_no_skipped_text_when_none(self):
        self.put("alpha")
        self.assertFalse(any("skipped" in t for t in self.text()))

    def test_keys_are_advertised(self):
        footer = [t for t in self.text() if "q quit" in t]
        self.assertTrue(footer and "r refresh" in footer[0])


class TestSizes(RenderCase):
    def test_too_small_width(self):
        self.put("alpha")
        texts = self.text(width=50, height=24)
        self.assertEqual(len(texts), 1)
        self.assertIn("terminal too small", texts[0])
        self.assertLessEqual(len(texts[0]), 50)

    def test_too_small_height(self):
        self.put("alpha")
        texts = self.text(width=80, height=15)
        self.assertEqual(len(texts), 1)
        self.assertIn("terminal too small", texts[0])

    def test_one_below_minimum_width(self):
        self.assertIn("too small", self.text(width=59, height=24)[0])

    def test_minimum_size_is_a_full_layout(self):
        self.put("alpha")
        texts = self.text(width=60, height=16)
        self.assertGreater(len(texts), 1)
        self.assertTrue(all(len(t) == 60 for t in texts), [len(t) for t in texts])
        self.assertLessEqual(len(texts), 16)

    def test_narrow_60_columns_with_long_names_and_stale(self):
        long_name = "a-very-long-host-name-indeed.example.test"
        self.put("alpha")
        self.put(long_name)
        self.put("beta", age_s=3 * 86400 + 4 * 3600)
        texts = self.text(width=60, height=24, expected=["gamma"])
        self.assertTrue(all(len(t) == 60 for t in texts), [len(t) for t in texts])
        out = "\n".join(texts)
        self.assertNotIn(long_name, out)  # clipped with an ellipsis, not wrapped
        self.assertIn("stale 3d04h", out)
        self.assertIn("unknown (never reported)", out)

    def test_wide_terminal_fills_width(self):
        self.put("alpha")
        texts = self.text(width=120, height=40)
        self.assertTrue(all(len(t) == 120 for t in texts))

    def test_many_hosts_truncated_but_total_and_footer_survive(self):
        for i in range(12):
            self.put("host%02d" % i)
        texts = self.text(width=80, height=16)
        self.assertLessEqual(len(texts), 16)
        out = "\n".join(texts)
        self.assertRegex(out, r"\d+ more hosts not shown")
        self.assertTrue(any(t.strip("│ ").startswith("total") for t in texts))
        self.assertIn("12 of 12 hosts fresh", out)

    def test_hosts_that_just_fit_get_no_more_notice(self):
        for i in range(4):  # 4 hosts x 2 rows = 8 rows = exactly the room at height 16
            self.put("host%d" % i)
        out = "\n".join(self.text(width=80, height=16))
        self.assertNotIn("more hosts", out)
        self.assertIn("host3", out)


class TestGlyphsAndColour(RenderCase):
    def setUp(self):
        super().setUp()
        self.put("alpha")
        self.put("beta", age_s=41 * 60)

    def test_unicode_mode_uses_box_drawing(self):
        out = "\n".join(self.text())
        self.assertIn("┌", out)
        self.assertIn("·", out)

    def test_ascii_mode_is_pure_ascii(self):
        write_inbox_file(self.state_dir, "bad.json", "{", NOW)  # footer separator too
        for width in (60, 80):
            out = "\n".join(self.text(ascii_mode=True, width=width))
            self.assertTrue(all(ord(c) < 128 for c in out), out)
            self.assertIn("+--", out)
            self.assertIn("|", out)

    def test_ascii_mode_clips_long_names_with_ascii_ellipsis(self):
        self.put("x" * 40)
        out = "\n".join(self.text(ascii_mode=True, width=60))
        self.assertTrue(all(ord(c) < 128 for c in out))
        self.assertIn("~", out)

    def test_no_color_emits_only_plain_and_bold(self):
        tags = {tag for ln in self.lines(color=False) for _, tag in ln}
        self.assertTrue(tags <= {view.PLAIN, view.BOLD}, tags)

    def test_no_color_still_conveys_meaning_in_text(self):
        out = "\n".join(self.text(color=False, expected=["gamma"]))
        self.assertIn("stale 41m", out)
        self.assertIn("unknown", out)
        self.assertIn("(never reported)", out)

    def test_color_mode_uses_the_documented_tags(self):
        tags = {tag for ln in self.lines(color=True) for _, tag in ln}
        self.assertTrue({view.BUSY, view.WAITING, view.GREY, view.DIM, view.BOLD} <= tags, tags)

    def test_text_is_identical_with_and_without_color(self):
        self.assertEqual(self.text(color=True), self.text(color=False))

    def test_every_line_is_a_list_of_text_tag_pairs(self):
        for ln in self.lines():
            for text, tag in ln:
                self.assertIsInstance(text, str)
                self.assertIn(tag, (view.PLAIN, view.BOLD, view.BUSY, view.WAITING,
                                    view.GREY, view.DIM))

    def test_title_has_name_and_timestamp(self):
        self.assertRegex(self.text()[0], r"grid-fleet .* \d{4}-\d{2}-\d{2} \d{2}:\d{2}")


# ---------------------------------------------------------------------------
# Curses loop against fakes
# ---------------------------------------------------------------------------


class FakeError(Exception):
    """Stands in for curses.error."""


class FakeScreen(object):
    """Records frames. `keys` entries are key codes, or callables(screen) -> key code
    (so a test can change the world between two ticks)."""

    def __init__(self, keys, size=(24, 80)):
        self.keys = list(keys)
        self.size = size
        self.frames = []  # list of lists of (y, x, text, attr)
        self._cur = []
        self.timeout_ms = None

    def timeout(self, ms):
        self.timeout_ms = ms

    def getmaxyx(self):
        return self.size

    def erase(self):
        self._cur = []

    def addstr(self, y, x, text, attr=0):
        self._cur.append((y, x, text, attr))

    def refresh(self):
        self.frames.append(self._cur)

    def getch(self):
        item = self.keys.pop(0)  # IndexError = the loop failed to quit; test would error
        return item(self) if callable(item) else item

    def frame_text(self, n=-1):
        rows = {}
        for y, x, text, _ in self.frames[n]:
            rows.setdefault(y, []).append((x, text))
        return "\n".join("".join(t for _, t in sorted(rows[y])) for y in sorted(rows))

    def frame_attrs(self, n=-1):
        return {a for _, _, _, a in self.frames[n]}


def fake_curses():
    calls = []
    api = types.SimpleNamespace(
        A_BOLD=1, A_DIM=2, KEY_RESIZE=410, error=FakeError, COLORS=256,
        COLOR_CYAN=6, COLOR_YELLOW=3, COLOR_BLACK=0,
        has_colors=lambda: True,
        start_color=lambda: calls.append(("start_color",)),
        use_default_colors=lambda: None,
        init_pair=lambda n, fg, bg: calls.append(("init_pair", n, fg, bg)),
        color_pair=lambda n: n << 8,
        curs_set=lambda v: calls.append(("curs_set", v)),
        update_lines_cols=lambda: calls.append(("update_lines_cols",)),
    )
    api.calls = calls
    return api


Q, R = ord("q"), ord("r")


class TestRunLoop(RenderCase):
    def config(self, **kw):
        return cfg.Config(state_dir=self.state_dir, refresh_s=kw.pop("refresh_s", 7), **kw)

    def run_loop(self, keys, size=(24, 80), env=None, config=None, api=None):
        screen = FakeScreen(keys, size)
        api = api or fake_curses()
        env = UTF8_ENV if env is None else env
        with mock.patch.dict(os.environ, {"GRID_FLEET_NOW": str(NOW)}):
            view.run_loop(screen, config or self.config(), api, env)
        return screen, api

    def test_q_quits_after_one_frame_and_restores_cursor(self):
        self.put("alpha")
        screen, api = self.run_loop([Q])
        self.assertEqual(len(screen.frames), 1)
        self.assertIn("alpha", screen.frame_text())
        self.assertEqual([c for c in api.calls if c[0] == "curs_set"], [("curs_set", 0), ("curs_set", 1)])

    def test_capital_q_also_quits(self):
        screen, _ = self.run_loop([ord("Q")])
        self.assertEqual(len(screen.frames), 1)

    def test_getch_timeout_is_refresh_s(self):
        screen, _ = self.run_loop([Q], config=self.config(refresh_s=3))
        self.assertEqual(screen.timeout_ms, 3000)

    def test_new_file_appears_on_next_tick(self):
        def drop_file(screen):
            self.put("alpha")
            return -1  # getch timeout

        screen, _ = self.run_loop([drop_file, Q])
        self.assertEqual(len(screen.frames), 2)
        self.assertNotIn("alpha", screen.frame_text(0))
        self.assertIn("alpha", screen.frame_text(1))

    def test_unchanged_inbox_is_not_rescanned_on_tick(self):
        self.put("alpha")
        with mock.patch.object(hub, "scan_inbox", wraps=hub.scan_inbox) as scan:
            self.run_loop([-1, -1, Q])
        self.assertEqual(scan.call_count, 1)  # only the initial read

    def test_age_still_advances_without_a_rescan(self):
        # Staleness is recomputed from the cached scan every tick.
        self.put("alpha", age_s=1190, interval_s=600)  # fresh: 1190 <= 1200

        def tick(screen):
            os.environ["GRID_FLEET_NOW"] = str(NOW + 30)  # now 1220 s old -> stale
            return -1

        screen, _ = self.run_loop([tick, Q])
        self.assertNotIn("stale", screen.frame_text(0))
        self.assertIn("stale 20m", screen.frame_text(1))

    def test_r_forces_a_rescan(self):
        self.put("alpha")
        with mock.patch.object(hub, "scan_inbox", wraps=hub.scan_inbox) as scan:
            self.run_loop([R, Q])
        self.assertEqual(scan.call_count, 2)

    def test_resize_redraws_at_new_size(self):
        self.put("alpha")

        def shrink(screen):
            screen.size = (10, 50)  # terminal resized under us
            return 410  # KEY_RESIZE

        screen, api = self.run_loop([shrink, Q])
        self.assertIn("alpha", screen.frame_text(0))
        self.assertIn("terminal too small", screen.frame_text(1))
        self.assertIn(("update_lines_cols",), api.calls)

    def test_exception_in_loop_still_restores_cursor(self):
        def boom(screen):
            raise RuntimeError("boom")

        api = fake_curses()
        with self.assertRaises(RuntimeError):
            self.run_loop([boom], api=api)
        self.assertEqual(api.calls[-1], ("curs_set", 1))

    def test_ctrl_c_propagates_and_restores_cursor(self):
        def ctrl_c(screen):
            raise KeyboardInterrupt

        api = fake_curses()
        with self.assertRaises(KeyboardInterrupt):
            self.run_loop([ctrl_c], api=api)
        self.assertEqual(api.calls[-1], ("curs_set", 1))

    def test_cursor_hide_failure_is_tolerated(self):
        api = fake_curses()

        def curs_set(v):
            raise FakeError("no such capability")

        api.curs_set = curs_set
        screen, _ = self.run_loop([Q], api=api)
        self.assertEqual(len(screen.frames), 1)

    def test_no_color_env_sets_no_color_attributes(self):
        self.put("alpha")
        self.put("beta", age_s=41 * 60)
        screen, api = self.run_loop([Q], env=dict(UTF8_ENV, NO_COLOR="1"))
        self.assertEqual([c for c in api.calls if c[0] in ("start_color", "init_pair")], [])
        self.assertTrue(screen.frame_attrs() <= {0, api.A_BOLD}, screen.frame_attrs())

    def test_color_attributes_used_by_default(self):
        self.put("alpha")
        self.put("beta", age_s=41 * 60)
        screen, api = self.run_loop([Q])
        self.assertTrue(any(c[0] == "init_pair" for c in api.calls))
        self.assertTrue(screen.frame_attrs() - {0, api.A_BOLD})

    def test_no_color_when_terminal_has_none(self):
        api = fake_curses()
        api.has_colors = lambda: False
        screen, _ = self.run_loop([Q], api=api)
        self.assertTrue(screen.frame_attrs() <= {0, api.A_BOLD})

    def test_ascii_when_locale_not_utf8(self):
        self.put("alpha")
        screen, _ = self.run_loop([Q], env={"LANG": "C"})
        self.assertTrue(all(ord(c) < 128 for c in screen.frame_text()))

    def test_ascii_when_config_says_so(self):
        self.put("alpha")
        screen, _ = self.run_loop([Q], env=UTF8_ENV, config=self.config(ascii=True))
        self.assertTrue(all(ord(c) < 128 for c in screen.frame_text()))

    def test_unicode_in_utf8_locale(self):
        screen, _ = self.run_loop([Q])
        self.assertIn("┌", screen.frame_text())

    def test_addstr_error_is_swallowed(self):
        # curses raises when writing the bottom-right cell; the loop must carry on.
        screen = FakeScreen([Q])
        screen.addstr = mock.Mock(side_effect=FakeError("bottom-right"))
        with mock.patch.dict(os.environ, {"GRID_FLEET_NOW": str(NOW)}):
            view.run_loop(screen, self.config(), fake_curses(), UTF8_ENV)
        self.assertTrue(screen.addstr.called)

    def test_expected_hosts_from_config_are_used(self):
        screen, _ = self.run_loop([Q], config=self.config(expected_hosts=("gamma",)))
        self.assertIn("unknown (never reported)", screen.frame_text())


class TestViewMain(unittest.TestCase):
    def test_non_tty_exits_1_without_touching_curses(self):
        err = io.StringIO()
        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = False
        with mock.patch.object(sys, "stdin", fake_stdin), mock.patch.object(sys, "stderr", err), \
                mock.patch("curses.wrapper") as wrapper:
            rc = view.view_main(cfg.Config(), None)
        self.assertEqual(rc, 1)
        self.assertIn("interactive terminal", err.getvalue())
        wrapper.assert_not_called()

    def _tty(self):
        tty = mock.Mock()
        tty.isatty.return_value = True
        return mock.patch.object(sys, "stdin", tty), mock.patch.object(sys, "stdout", tty)

    def test_ctrl_c_is_a_normal_exit(self):
        a, b = self._tty()
        with a, b, mock.patch("curses.wrapper", side_effect=KeyboardInterrupt) as wrapper:
            rc = view.view_main(cfg.Config(), None)
        self.assertEqual(rc, 0)
        self.assertIs(wrapper.call_args[0][0], view.run_loop)  # run inside curses.wrapper

    def test_other_exceptions_propagate(self):
        a, b = self._tty()
        with a, b, mock.patch("curses.wrapper", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                view.view_main(cfg.Config(), None)


# ---------------------------------------------------------------------------
# Spec: Read-only, and no network
# ---------------------------------------------------------------------------


class TestReadOnlyNoNetwork(RenderCase):
    def setUp(self):
        super().setUp()
        self.put("alpha")
        self.put("beta", age_s=41 * 60)
        write_inbox_file(self.state_dir, "bad.json", "{oops", NOW)
        write_inbox_file(self.state_dir, ".gamma.json.tmp", "partial", NOW)

    def _forbid_network(self):
        def refuse(*a, **k):
            raise AssertionError("view touched the network")

        patches = [
            mock.patch.object(socket.socket, "__init__", refuse),
            mock.patch.object(socket.socket, "connect", refuse),
            mock.patch.object(socket.socket, "connect_ex", refuse),
            mock.patch.object(socket.socket, "bind", refuse),
            mock.patch.object(socket, "create_connection", refuse),
            mock.patch.object(socket, "getaddrinfo", refuse),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_network_guard_actually_bites(self):
        self._forbid_network()
        with self.assertRaises(AssertionError):
            socket.socket()

    def test_hub_read_render_and_loop_open_no_sockets_and_write_nothing(self):
        self._forbid_network()
        before = syn.snapshot_tree(self.state_dir)

        # hub read + render
        state = hub.read_hub(self.state_dir, NOW, ["alpha", "beta", "gamma"])
        self.assertTrue(view.render(state, 80, 24))

        # the whole loop: initial read, a tick, a forced refresh, a resize, quit
        screen = FakeScreen([-1, R, 410, Q])
        with mock.patch.dict(os.environ, {"GRID_FLEET_NOW": str(NOW)}):
            view.run_loop(screen, cfg.Config(state_dir=self.state_dir), fake_curses(), UTF8_ENV)
        self.assertEqual(len(screen.frames), 4)

        # inbox byte-identical, mtimes unchanged, nothing created (not even a directory)
        self.assertEqual(before, syn.snapshot_tree(self.state_dir))

    def test_view_does_not_create_a_missing_state_dir(self):
        missing = os.path.join(self.state_dir, "does-not-exist")
        screen = FakeScreen([Q])
        with mock.patch.dict(os.environ, {"GRID_FLEET_NOW": str(NOW)}):
            view.run_loop(screen, cfg.Config(state_dir=missing), fake_curses(), UTF8_ENV)
        self.assertFalse(os.path.exists(missing))
        self.assertIn("no snapshots in the inbox yet", screen.frame_text())

    def test_source_has_no_socket_or_write_calls(self):
        # Belt and braces next to the behavioural tests: the modules do not even import
        # socket, and open() is only ever used for reading.
        for mod in (view, hub):
            with open(mod.__file__, encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotRegex(src, r"^\s*(import|from)\s+socket\b", mod.__name__)
            for m in re.finditer(r"open\([^)]*\)", src):
                self.assertNotRegex(m.group(0), r"""['"][wax+]""", m.group(0))


if __name__ == "__main__":
    unittest.main()
