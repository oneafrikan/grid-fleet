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
        # cells: name busy wait idle out24 out7 (the idle column sits before the tokens)
        self.assertEqual(cells[4:6], ["unknown", "unknown"])
        self.assertEqual(self.row(texts, "beta").strip("│ ").split()[4], "412k")

    def test_null_tokens_render_unknown_and_drop_muted_line(self):
        self.put("alpha", tokens=NULL)
        texts = self.text()
        cells = self.row(texts, "alpha").strip("│ ").split()
        self.assertEqual(cells[:6], ["alpha", "2", "1", "0", "unknown", "unknown"])
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
        self.put("alpha", sessions={"busy": 2, "waiting": 1, "idle": 0})
        self.put("beta", age_s=41 * 60, sessions={"busy": 9, "waiting": 7, "idle": 5})
        texts = self.text()
        total = self.row(texts, "total").strip("│ ").split()
        self.assertEqual(total[:6], ["total", "2", "1", "0", "412k", "3.1M"])
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
        self.assertEqual(total[1:6], ["unknown"] * 5)

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


class TestIdleColumn(RenderCase):
    """The idle column: an open session at the prompt must not look like no session."""

    IDLE_ONLY = {"busy": 0, "waiting": 0, "idle": 1, "other": 0, "total": 1}

    def cells(self, texts, name):
        return self.row(texts, name).strip("│| ").split()

    def head(self, texts):
        return next(t for t in texts if t.strip("│| ").startswith("host"))

    def test_header_has_idle_between_wait_and_tokens(self):
        self.put("alpha", sessions=self.IDLE_ONLY)
        head = self.head(self.text(width=120, height=30)).split()
        self.assertEqual(head[:5], ["│", "host", "busy", "wait", "idle"])

    def test_idle_only_host_shows_idle_1_and_total_sums_it(self):
        self.put("alpha", sessions=self.IDLE_ONLY)
        self.put("beta", sessions={"busy": 0, "waiting": 0, "idle": 2, "other": 0, "total": 2})
        for w, h in ((120, 30), (72, 24)):
            texts = self.text(width=w, height=h)
            self.assertEqual(self.cells(texts, "alpha")[:4], ["alpha", "0", "0", "1"], w)
            self.assertEqual(self.cells(texts, "total")[:4], ["total", "0", "0", "3"], w)

    def test_null_sessions_idle_is_unknown(self):
        self.put("alpha", sessions=NULL)
        self.assertEqual(self.cells(self.text(width=120, height=30), "alpha")[1:4], ["unknown"] * 3)

    def test_partial_total_is_marked(self):
        self.put("alpha", sessions=self.IDLE_ONLY)
        self.put("beta", sessions=NULL)
        total = self.cells(self.text(width=120, height=30), "total")
        self.assertEqual(total[3], "1+")

    def test_idle_is_dim_never_busy_or_waiting_colour(self):
        self.put("alpha", sessions=self.IDLE_ONLY)
        lines = self.lines(width=120, height=30)
        row = next(ln for ln in lines if "alpha" in "".join(t for t, _ in ln))
        # cells are [" ", name, gap, busy, gap, wait, gap, idle, ...]: idle is the third count
        counts = [(t.strip(), tag) for t, tag in row if t.strip().isdigit()]
        self.assertEqual(counts[2], ("1", view.DIM))

    def test_ascii_and_no_color_still_say_idle(self):
        self.put("alpha", sessions=self.IDLE_ONLY)
        lines = self.lines(width=120, height=30, ascii_mode=True, color=False)
        texts = view.plain_text(lines)
        self.assertTrue(all(ord(c) < 128 for t in texts for c in t))
        self.assertIn("idle", self.head(texts))
        self.assertEqual(self.cells(texts, "alpha")[:4], ["alpha", "0", "0", "1"])
        self.assertTrue(all(tag in ("plain", "bold") for ln in lines for _, tag in ln))

    def test_layout_is_exact_width_at_72_and_120(self):
        self.put("alpha", sessions=self.IDLE_ONLY)
        self.put("beta", age_s=41 * 60)
        for w in (72, 120):
            texts = self.text(width=w, height=30, expected=["gamma"])
            self.assertTrue(all(len(t) == w for t in texts), (w, [len(t) for t in texts]))
            self.assertIn("stale 41m", self.row(texts, "beta"))

    def test_idle_column_dropped_at_the_60_col_floor_not_broken(self):
        # No room at 60x16: busy/wait/tokens/age must stay intact and the box exact.
        self.put("alpha", sessions=self.IDLE_ONLY)
        self.put("beta", age_s=41 * 60)
        texts = self.text(width=60, height=24)
        self.assertTrue(all(len(t) == 60 for t in texts), [len(t) for t in texts])
        self.assertNotIn("idle", self.head(texts))
        self.assertEqual(self.cells(texts, "alpha")[:3], ["alpha", "0", "0"])
        self.assertIn("stale 41m", self.row(texts, "beta"))

    def test_idle_column_kept_at_the_narrowest_width_that_fits(self):
        # The cut-over is where host-name and age columns hit their minimums.
        self.put("alpha", sessions=self.IDLE_ONLY)
        self.assertNotIn("idle", self.head(self.text(width=62, height=24)))
        self.assertIn("idle", self.head(self.text(width=63, height=24)))


class TestColumnSpacing(RenderCase):
    """Width tiers for the hosts table: compact below 100 cols, roomy 100-139, wide 140+."""

    IDLE3 = {"busy": 2, "waiting": 1, "idle": 3, "other": 0, "total": 6}

    def setUp(self):
        super().setUp()
        self.put("alpha", sessions=self.IDLE3)
        self.put("gamma-long-host", age_s=41 * 60)

    def table(self, width, height=30):
        """(all rows, header row, stale row) at `width`; the stale host may be clipped."""
        texts = self.text(width=width, height=height, expected=["delta"])
        head = next(t for t in texts if t.strip("│| ").startswith("host"))
        stale = next(t for t in texts if "stale 41m" in t)
        return texts, head, stale

    def test_compact_layout_is_unchanged_below_100_columns(self):
        # Exact rows captured from the pre-tier renderer: the 72- and 99-col layouts must not move.
        texts, head, _ = self.table(72)
        self.assertEqual(head, "│ host              busy    wait    idle  out 24h   out 7d  age        │")
        self.assertEqual(self.row(texts, "alpha"),
                         "│ alpha                2       1       3     412k     3.1M  3m         │")
        self.assertEqual(self.row(texts, "total"),
                         "│ total                2       1       3     412k     3.1M             │")
        texts, head, _ = self.table(99)
        self.assertEqual(head, "│ host               busy    wait    idle  out 24h   out 7d  age"
                               + " " * 34 + "│")
        self.assertEqual(self.row(texts, "alpha"),
                         "│ alpha                 2       1       3     412k     3.1M  3m"
                         + " " * 35 + "│")

    def test_compact_tier_at_the_idle_cutover(self):
        self.assertEqual(self.table(63, 24)[1],
                         "│ host     busy    wait    idle  out 24h   out 7d  age        │")
        self.assertEqual(self.table(62, 24)[1],
                         "│ host            busy    wait  out 24h   out 7d  age        │")

    def test_tier_boundaries(self):
        # 99 is still compact (cells 7/8, 1-space gaps); 100 is roomy; 139 roomy; 140 wide.
        for w, count_w, tok_w, gap in ((99, 7, 8, 1), (100, 8, 10, 2), (139, 8, 10, 2),
                                       (140, 9, 11, 3)):
            lay = view.layout_for(w)
            self.assertEqual((lay.count_w, lay.tok_w, len(lay.gap)), (count_w, tok_w, gap), w)
            _, head, _ = self.table(w)
            self.assertEqual(len(head), w, w)
            # busy and wait headers sit exactly one pitch apart
            self.assertEqual(head.index("wait") - head.index("busy"), count_w + gap, w)

    def test_roomy_and_wide_tiers_separate_columns_by_at_least_two_spaces(self):
        for w in (100, 120, 139, 140, 200):
            texts, head, _ = self.table(w)
            self.assertRegex(head, r"busy {2,}wait {2,}idle {2,}out 24h {2,}out 7d {2,}age", w)
            for name in ("alpha", "total"):
                self.assertRegex(self.row(texts, name), r"2 {2,}1 {2,}3 {2,}412k {2,}3\.1M", (w, name))

    def test_figures_do_not_touch_in_roomy_tiers(self):
        # A 6-char token value (`540.3k`) keeps real space before the next figure.
        self.put("big", sessions=self.IDLE3, tokens={
            "last_24h": tokens_window(output=540300), "last_7d": tokens_window(output=1500000)})
        for w in (100, 120, 140):
            texts, _, _ = self.table(w)
            self.assertRegex(self.row(texts, "big"), r"540\.3k {4,}1\.5M", w)

    def test_header_and_cells_stay_right_aligned_together(self):
        # The last character of every numeric header sits in the same column as the last
        # character of the figure beneath it, at every tier.
        for w in (72, 100, 120, 140, 180):
            texts, head, _ = self.table(w)
            alpha = self.row(texts, "alpha")
            for header, cell in (("busy", "2"), ("wait", "1"), ("idle", "3"),
                                 ("out 24h", "412k"), ("out 7d", "3.1M")):
                end = head.index(header) + len(header)
                self.assertTrue(alpha[:end].endswith(cell), (w, header, alpha))
                self.assertEqual(alpha[end], " ", (w, header))

    def test_age_column_aligned_for_fresh_and_stale_rows(self):
        for w in (63, 72, 99, 100, 120, 140, 200):
            texts, head, stale = self.table(w, 24)
            col = head.index("age")
            self.assertEqual(self.row(texts, "alpha")[col:col + 2], "3m", w)
            self.assertEqual(stale[col:col + 9], "stale 41m", w)

    def test_box_exact_and_special_rows_fit_at_every_width(self):
        for w in range(60, 201):
            texts, _, _ = self.table(w, 24)
            self.assertTrue(all(len(t) == w for t in texts), (w, [len(t) for t in texts]))
            self.assertIn("unknown (never reported)", self.row(texts, "delta"), w)

    def test_idle_column_and_full_host_name_kept_in_roomy_tiers(self):
        for w in (100, 140):
            texts, head, _ = self.table(w)
            self.assertIn("idle", head)
            self.assertIn("gamma-long-host", "\n".join(texts))  # 15-char name unclipped

    def test_ascii_no_color_table_rows_match_unicode_width(self):
        for w in (80, 120, 160):
            plain = self.text(width=w, height=30, expected=["delta"], ascii_mode=True, color=False)
            self.assertTrue(all(len(t) == w for t in plain), w)
            self.assertTrue(all(ord(c) < 128 for t in plain for c in t), w)
            self.assertRegex(next(t for t in plain if " host " in t), r"busy +wait +idle +out 24h")


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
        for i in range(4):  # 4 hosts x 2 rows = 8 rows = exactly the room at height 20
            self.put("host%d" % i)  # (height 16 minus the 3-row limits strip leaves only 6)
        out = "\n".join(self.text(width=80, height=20))
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
                                    view.GREY, view.DIM, view.OK, view.WARN, view.CRIT))

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
        COLOR_CYAN=6, COLOR_YELLOW=3, COLOR_BLACK=0, COLOR_GREEN=2, COLOR_RED=1,
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

    def test_limit_bars_get_green_yellow_red_pairs(self):
        # Synthetic limits: 10 % (green), 70 % (yellow), 90 % (red) across two accounts.
        lim = lambda a, b: syn.make_limits(NOW - 60, syn.limit_window(a, NOW + 7000),  # noqa: E731
                                           syn.limit_window(b, NOW + 90000))
        self.put("alpha", account="team-a", limits=lim(10.0, 70.0))
        self.put("beta", account="team-b", limits=lim(90.0, 10.0))
        screen, api = self.run_loop([Q])
        pairs = {c[1]: c[2] for c in api.calls if c[0] == "init_pair"}
        self.assertEqual((pairs[4], pairs[5], pairs[6]),
                         (api.COLOR_GREEN, api.COLOR_YELLOW, api.COLOR_RED))
        self.assertTrue({4 << 8, 5 << 8, 6 << 8} <= screen.frame_attrs(), screen.frame_attrs())

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

    # --- NO_COLOR must not start colour at all (F6) -----------------------------------

    def _config(self):
        # An empty state dir: the dashboard then renders "no hosts", which is all these need.
        tmp = tempfile.TemporaryDirectory(prefix="gf-view-")
        self.addCleanup(tmp.cleanup)
        return cfg.Config(state_dir=tmp.name)

    def _fake_terminal(self, stdscr_keys):
        """Patch the real `curses` module functions that view_main would call, so nothing
        touches a terminal. Returns (log, stdscr). `log` records the order of setup/restore
        calls."""
        log = []
        stdscr = FakeScreen(stdscr_keys)
        stdscr.keypad = lambda flag: log.append(("keypad", flag))
        patches = mock.patch.multiple(
            "curses",
            initscr=lambda: (log.append(("initscr",)), stdscr)[1],
            noecho=lambda: log.append(("noecho",)),
            cbreak=lambda: log.append(("cbreak",)),
            echo=lambda: log.append(("echo",)),
            nocbreak=lambda: log.append(("nocbreak",)),
            endwin=lambda: log.append(("endwin",)),
            start_color=lambda: log.append(("start_color",)),
            has_colors=lambda: True,
            curs_set=lambda v: log.append(("curs_set", v)),
        )
        return log, stdscr, patches

    def test_no_color_never_calls_start_color_and_restores_the_terminal(self):
        log, _screen, patches = self._fake_terminal([Q])
        a, b = self._tty()
        with a, b, patches, mock.patch.dict(os.environ, {"NO_COLOR": "1", "GRID_FLEET_NOW": str(NOW)}):
            rc = view.view_main(self._config(), None)
        self.assertEqual(rc, 0)
        names = [c[0] for c in log]
        self.assertNotIn("start_color", names)
        self.assertEqual(names[:4], ["initscr", "noecho", "cbreak", "keypad"])
        # Same restore sequence as curses.wrapper.
        self.assertEqual(names[-4:], ["keypad", "echo", "nocbreak", "endwin"])

    def test_no_color_route_restores_the_terminal_when_the_loop_raises(self):
        log, screen, patches = self._fake_terminal([])  # getch pops from []: IndexError mid-loop
        a, b = self._tty()
        with a, b, patches, mock.patch.dict(os.environ, {"NO_COLOR": "1", "GRID_FLEET_NOW": str(NOW)}):
            with self.assertRaises(IndexError):
                view.view_main(self._config(), None)
        self.assertEqual([c[0] for c in log][-4:], ["keypad", "echo", "nocbreak", "endwin"])

    def test_color_route_still_uses_curses_wrapper(self):
        a, b = self._tty()
        with a, b, mock.patch.dict(os.environ, {"NO_COLOR": ""}), \
                mock.patch("curses.wrapper") as wrapper, mock.patch("curses.initscr") as initscr:
            view.view_main(cfg.Config(), None)
        wrapper.assert_called_once()
        initscr.assert_not_called()

    def test_ctrl_c_is_a_normal_exit(self):
        a, b = self._tty()
        with a, b, mock.patch.dict(os.environ, {"NO_COLOR": ""}), \
                mock.patch("curses.wrapper", side_effect=KeyboardInterrupt) as wrapper:
            rc = view.view_main(cfg.Config(), None)
        self.assertEqual(rc, 0)
        self.assertIs(wrapper.call_args[0][0], view.run_loop)  # run inside curses.wrapper

    def test_other_exceptions_propagate(self):
        a, b = self._tty()
        with a, b, mock.patch.dict(os.environ, {"NO_COLOR": ""}), \
                mock.patch("curses.wrapper", side_effect=RuntimeError("boom")):
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
