"""Subagent leaderboard + output-token sparkline, view half of tasks 6.2 / 6.3.

Driven entirely by SYNTHETIC snapshots carrying `agents.last_7d` and `tokens.by_day` shaped
exactly as snapshot-collect / design.md define them (helpers.synthetic_snapshot.make_agents,
by_day_rows). Two layers:

  - hub.build_agents / build_by_day / build_state: cross-host sum, `main`, sort + tie-break,
    top-8 cut, stale hosts excluded, null sections, hostile numbers and strings.
  - view.render: the panel's text, scaling, ASCII, NO_COLOR, sizes, degradation order.

Nothing here touches the collect side (sources_claude.py, snapshot.py). All names invented.
These tests verify my own work only; independent verification is qa-engineer's.
"""

import re
import tempfile
import unittest

from grid_fleet import hub, view
from helpers.synthetic_snapshot import (NOW, NULL, agent_row, by_day_rows, make_agents,
                                        make_snapshot, make_tokens, write_inbox_file)

# NOW is 2026-10-21 UTC; a collector's by_day then ends on that date and starts 6 days earlier.
FIRST_DAY = "2026-10-15"
WEEK = [100000, 300000, 500000, 900000, 700000, 300000, 100000]


class PanelCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = self.tmp.name

    def put(self, host, age_s=180, **kw):
        write_inbox_file(self.state_dir, host + ".json", make_snapshot(host, **kw), NOW - age_s)

    def hubstate(self):
        return hub.read_hub(self.state_dir, NOW)

    def lines(self, width=80, height=24, ascii_mode=False, color=True):
        return view.render(self.hubstate(), width, height, ascii_mode, color)

    def text(self, **kw):
        return view.plain_text(self.lines(**kw))

    def board(self):
        """{type: invocations} from the hub state."""
        return dict((r.type, r.invocations) for r in self.hubstate().agents)

    def board_order(self):
        return [r.type for r in self.hubstate().agents]

    def days(self):
        return [(d.day, d.output) for d in self.hubstate().by_day]


# ---------------------------------------------------------------------------
# hub: leaderboard aggregation
# ---------------------------------------------------------------------------


class TestLeaderboardAggregation(PanelCase):
    def test_cross_host_sum_two_fresh_hosts_each_5_is_10(self):
        # Spec scenario "Cross-host sum".
        self.put("alpha", agents=make_agents(agent_row("core-researcher", 5, 100)))
        self.put("beta", agents=make_agents(agent_row("core-researcher", 5, 50)))
        self.assertEqual(self.board(), {"core-researcher": 10})
        self.assertEqual(self.hubstate().agents[0].output, 150)  # output tokens sum too

    def test_main_is_included_and_ranked_like_any_other_type(self):
        self.put("alpha", agents=make_agents(agent_row("main", 38), agent_row("core-researcher", 12)))
        self.assertEqual(self.board_order(), ["main", "core-researcher"])

    def test_sorted_by_invocations_desc_regardless_of_input_order(self):
        self.put("alpha", agents=make_agents(agent_row("a", 1), agent_row("b", 9), agent_row("c", 4)))
        self.assertEqual(self.board_order(), ["b", "c", "a"])

    def test_ties_break_by_type_name_ascending(self):
        # The spec only says "invocations desc": the tie-break is ours, and must be stable.
        self.put("alpha", agents=make_agents(agent_row("zeta", 3), agent_row("alpha-agent", 3),
                                             agent_row("main", 3), agent_row("mid", 3)))
        self.assertEqual(self.board_order(), ["alpha-agent", "main", "mid", "zeta"])

    def test_tie_break_applies_after_summing_across_hosts(self):
        self.put("alpha", agents=make_agents(agent_row("b-type", 2), agent_row("a-type", 5)))
        self.put("beta", agents=make_agents(agent_row("b-type", 3)))  # b-type 5 == a-type 5
        self.assertEqual(self.board_order(), ["a-type", "b-type"])

    def test_top_8_cut_drops_the_lowest(self):
        rows = [agent_row("t%02d" % i, 100 - i) for i in range(12)]
        self.put("alpha", agents=make_agents(*rows))
        order = self.board_order()
        self.assertEqual(len(order), 8)
        self.assertEqual(order, ["t%02d" % i for i in range(8)])

    def test_top_8_is_cut_after_the_cross_host_sum(self):
        # Eight big types, plus a ninth that only wins once the two hosts are added up.
        big = [agent_row("big%d" % i, 10) for i in range(8)]
        self.put("alpha", agents=make_agents(*(big + [agent_row("split", 6)])))
        self.put("beta", agents=make_agents(agent_row("split", 6)))
        self.assertEqual(self.board_order()[0], "split")
        self.assertEqual(len(self.board_order()), 8)

    def test_stale_host_is_excluded(self):
        self.put("alpha", agents=make_agents(agent_row("core-researcher", 5)))
        self.put("beta", age_s=41 * 60, agents=make_agents(agent_row("core-researcher", 5),
                                                           agent_row("stale-only", 99)))
        self.assertEqual(self.board(), {"core-researcher": 5})

    def test_null_agents_contributes_nothing_and_is_not_an_error(self):
        self.put("alpha", agents=NULL)
        self.put("beta", agents=make_agents(agent_row("main", 4)))
        self.assertEqual(self.board(), {"main": 4})

    def test_no_host_reports_agents_gives_empty_board(self):
        self.put("alpha", agents=NULL)
        self.assertEqual(self.hubstate().agents, ())

    def test_agents_of_the_wrong_shape_are_ignored(self):
        self.put("alpha", agents={"last_7d": "lots"})
        self.put("beta", agents=["main"])
        self.put("gamma", agents={"last_7d": [1, "main", None, ["main", 3]]})
        self.put("delta", agents=make_agents(agent_row("main", 2)))
        self.assertEqual(self.board(), {"main": 2})

    def test_missing_agents_key_is_tolerated(self):
        snap = make_snapshot("alpha")
        del snap["agents"]
        write_inbox_file(self.state_dir, "alpha.json", snap, NOW - 60)
        self.assertEqual(self.hubstate().agents, ())

    def test_output_is_none_when_no_host_promised_it(self):
        self.put("alpha", agents={"last_7d": [{"type": "main", "invocations": 3}]})
        self.assertIsNone(self.hubstate().agents[0].output)


class TestLeaderboardHostileInput(PanelCase):
    def test_hostile_invocation_counts_are_ignored_not_fatal(self):
        bad = [10 ** 400, hub.MAX_COUNT + 1, -5, 2.5, float("nan"), float("inf"), True, "7", None,
               [3], {"n": 3}, 0]
        rows = [agent_row("bad%d" % i, v) for i, v in enumerate(bad)]
        rows.append(agent_row("good", 4))
        self.put("alpha", agents=make_agents(*rows))
        self.assertEqual(self.board(), {"good": 4})  # and read_hub did not raise

    def test_hostile_counts_do_not_skew_a_valid_sum_for_the_same_type(self):
        self.put("alpha", agents=make_agents(agent_row("main", 5)))
        self.put("beta", agents=make_agents(agent_row("main", 10 ** 400)))
        self.put("gamma", agents=make_agents(agent_row("main", -100)))
        self.assertEqual(self.board(), {"main": 5})

    def test_count_at_the_cap_is_believed(self):
        self.put("alpha", agents=make_agents(agent_row("main", hub.MAX_COUNT)))
        self.assertEqual(self.board(), {"main": hub.MAX_COUNT})

    def test_hostile_output_is_dropped_but_the_row_stays(self):
        self.put("alpha", agents=make_agents(agent_row("main", 4, float("nan"))))
        self.put("beta", agents=make_agents(agent_row("main", 4, -1)))
        self.put("gamma", agents=make_agents(agent_row("main", 4, 10 ** 400)))
        row = self.hubstate().agents[0]
        self.assertEqual((row.type, row.invocations, row.output), ("main", 12, None))

    def test_agent_type_escape_sequences_are_neutralised(self):
        evil = "\x1b[2J\x1b]0;pwned\x07core"
        self.put("alpha", agents=make_agents(agent_row(evil, 5)))
        label = self.hubstate().agents[0].type
        self.assertTrue(all(" " <= c <= "~" for c in label), repr(label))
        self.assertNotIn("\x1b", label)
        # And nothing control-like reaches the rendered screen.
        for t in self.text():
            self.assertTrue(all(c == "\n" or c >= " " and c != "\x7f" for c in t), repr(t))
            self.assertNotIn("\x1b", t)

    def test_agent_type_non_ascii_and_control_become_question_marks(self):
        self.put("alpha", agents=make_agents(agent_row("café\x00\x7f\n‮", 5)))
        self.assertEqual(self.hubstate().agents[0].type, "caf?????")

    def test_agent_type_is_truncated(self):
        self.put("alpha", agents=make_agents(agent_row("x" * 5000, 5)))
        self.assertEqual(len(self.hubstate().agents[0].type), hub.AGENT_TYPE_MAX)

    def test_long_names_sharing_a_prefix_stay_distinct_rows(self):
        # QA finding 1: labels used to be cut to 24 chars BEFORE summing, so these 11 types
        # (same 24-char prefix) silently merged into one row. They aggregate on the full label.
        names = ["agent-with-a-rather-long-name-%d" % i for i in range(1, 12)]
        self.assertEqual(len(set(n[:24] for n in names)), 1)  # the premise: one shared prefix
        self.put("alpha", agents=make_agents(*[agent_row(n, 20 - i) for i, n in enumerate(names)]))
        self.put("beta", agents=make_agents(agent_row(names[0], 5)))  # sums per FULL name
        board = self.board()
        # The top-8 cut applies after aggregation: 8 distinct rows, none merged.
        self.assertEqual(len(board), 8)
        self.assertEqual(board[names[0]], 25)
        self.assertEqual(self.board_order(), names[:8])

    def test_long_name_ties_break_deterministically_on_the_full_label(self):
        a, b = "x" * 30 + "-b", "x" * 30 + "-a"  # identical first 30 chars
        self.put("alpha", agents=make_agents(agent_row(a, 3), agent_row(b, 3)))
        self.assertEqual(self.board_order(), [b, a])

    def test_agent_type_that_cleans_to_nothing_is_unknown(self):
        self.put("alpha", agents=make_agents(agent_row("   ", 5)))
        self.assertEqual(self.board(), {"unknown": 5})

    def test_non_text_type_row_is_skipped(self):
        self.put("alpha", agents=make_agents(agent_row(42, 5), agent_row(None, 5),
                                             agent_row(["main"], 5), agent_row("main", 1)))
        self.assertEqual(self.board(), {"main": 1})

    def test_a_flood_of_rows_per_host_is_bounded(self):
        rows = [agent_row("t%d" % i, 1) for i in range(hub.MAX_AGENT_ROWS_PER_HOST + 500)]
        self.put("alpha", agents=make_agents(*rows))
        self.assertLessEqual(len(self.hubstate().agents), hub.LEADERBOARD_SIZE)

    def test_rows_beyond_the_per_host_cap_are_ignored_even_if_they_would_win(self):
        # H17. The old check used equal-weight rows, so an uncapped read changed nothing
        # visible. Here the row just past the cap would outrank everything if it were read.
        cap = hub.MAX_AGENT_ROWS_PER_HOST
        rows = [agent_row("t%d" % i, 1) for i in range(cap - 1)]
        rows.append(agent_row("last-read", 50))        # index cap-1: the last row that counts
        rows.append(agent_row("beyond-cap", 10 ** 6))  # index cap: must be ignored
        self.put("alpha", agents=make_agents(*rows))
        self.assertNotIn("beyond-cap", self.board())
        self.assertEqual(self.board_order()[0], "last-read")  # the boundary row still counts


class TestByDayAggregation(PanelCase):
    def test_sums_across_fresh_hosts_aligned_by_date(self):
        self.put("alpha", tokens=make_tokens(by_day_rows(FIRST_DAY, [1, 2, 3, 4, 5, 6, 7])))
        self.put("beta", tokens=make_tokens(by_day_rows(FIRST_DAY, [10, 20, 30, 40, 50, 60, 70])))
        self.assertEqual([o for _, o in self.days()], [11, 22, 33, 44, 55, 66, 77])
        self.assertEqual(self.days()[0][0], FIRST_DAY)
        self.assertEqual(self.days()[-1][0], "2026-10-21")

    def test_hosts_ending_on_different_dates_align_by_date_not_position(self):
        self.put("alpha", tokens=make_tokens(by_day_rows("2026-10-15", [1, 1, 1, 1, 1, 1, 1])))
        self.put("beta", tokens=make_tokens(by_day_rows("2026-10-16", [5, 5, 5, 5, 5, 5, 5])))
        d = dict(self.days())
        self.assertEqual(len(d), 7)  # 8 distinct dates in total: only the latest 7 are shown
        self.assertNotIn("2026-10-15", d)
        self.assertEqual(d["2026-10-16"], 6)
        self.assertEqual(d["2026-10-22"], 5)

    def test_null_and_short_by_day_contribute_what_they_have(self):
        self.put("alpha", tokens=make_tokens(by_day_rows("2026-10-19", [4, 5, 6])))
        self.put("beta", tokens=NULL)
        self.put("gamma", tokens=make_tokens([]))
        self.assertEqual(self.days(), [("2026-10-19", 4), ("2026-10-20", 5), ("2026-10-21", 6)])

    def test_no_by_day_anywhere_is_empty(self):
        self.put("alpha", tokens=NULL)
        self.assertEqual(self.hubstate().by_day, ())

    def test_stale_host_is_excluded(self):
        self.put("alpha", tokens=make_tokens(by_day_rows(FIRST_DAY, [1] * 7)))
        self.put("beta", age_s=41 * 60, tokens=make_tokens(by_day_rows(FIRST_DAY, [100] * 7)))
        self.assertEqual([o for _, o in self.days()], [1] * 7)

    def test_invalid_days_and_hostile_outputs_are_ignored(self):
        rows = by_day_rows(FIRST_DAY, [5, 5, 5])
        rows += [{"day": "2026-13-45", "output": 999}, {"day": "not a date", "output": 999},
                 {"day": "\x1b[31m2026-10-21", "output": 999}, {"day": 20261021, "output": 999},
                 {"day": "2026-10-21", "output": 10 ** 400}, {"day": "2026-10-22", "output": -3},
                 {"day": "2026-10-23", "output": float("nan")}, {"day": "2026-10-24", "output": "9"},
                 {"day": "2026-10-25"}, "junk", None, 7]
        self.put("alpha", tokens=make_tokens(rows))
        self.assertEqual(self.days(), [(FIRST_DAY, 5), ("2026-10-16", 5), ("2026-10-17", 5)])

    def test_unicode_digits_are_not_a_date(self):
        # `\d` would accept these; the date check is ASCII only.
        self.put("alpha", tokens=make_tokens([{"day": "٢026-10-21", "output": 5}]))
        self.assertEqual(self.hubstate().by_day, ())

    def test_a_repeated_date_within_one_host_counts_once(self):
        rows = by_day_rows(FIRST_DAY, [5]) * 3
        self.put("alpha", tokens=make_tokens(rows))
        self.assertEqual(self.days(), [(FIRST_DAY, 5)])

    def test_more_than_seven_days_keeps_the_latest_seven(self):
        self.put("alpha", tokens=make_tokens(by_day_rows("2026-10-10", list(range(1, 12)))))
        got = self.days()
        self.assertEqual(len(got), 7)
        self.assertEqual(got[-1][0], "2026-10-20")

    def test_by_day_rows_beyond_the_per_host_cap_are_ignored(self):
        # H18. Rows past MAX_BY_DAY_PER_HOST would win the "latest seven" cut if they were
        # read, so the surviving days prove the cap (and that the last allowed row counts).
        cap = hub.MAX_BY_DAY_PER_HOST
        early = by_day_rows("2026-01-01", [1] * cap)   # the rows that are read
        late = by_day_rows("2026-10-15", [100] * 7)    # beyond the cap: must be ignored
        self.put("alpha", tokens=make_tokens(early + late))
        got = self.days()
        self.assertEqual([d for d, _ in got], ["2026-01-%02d" % n for n in range(cap - 6, cap + 1)])
        self.assertTrue(all(o == 1 for _, o in got))

    def test_by_day_of_the_wrong_shape_is_ignored(self):
        self.put("alpha", tokens={"last_24h": {}, "last_7d": {}, "by_day": "lots"})
        self.put("beta", tokens={"by_day": {"day": FIRST_DAY, "output": 5}})
        self.assertEqual(self.hubstate().by_day, ())

    def test_huge_valid_values_sum_without_trouble(self):
        self.put("alpha", tokens=make_tokens(by_day_rows(FIRST_DAY, [hub.MAX_COUNT])))
        self.put("beta", tokens=make_tokens(by_day_rows(FIRST_DAY, [hub.MAX_COUNT])))
        self.assertEqual(self.days(), [(FIRST_DAY, 2 * hub.MAX_COUNT)])


# ---------------------------------------------------------------------------
# view: sparkline
# ---------------------------------------------------------------------------


UNI_SPARK = "▁▂▃▄▅▆▇█"
ASCII_SPARK = "_.,-=+*#"


def spark_row(texts):
    """The panel row holding the sparkline glyphs (the one with the `max` label)."""
    for t in texts:
        if " max " in t:
            return t
    raise AssertionError("no sparkline row in:\n" + "\n".join(texts))


def glyphs_of(row, alphabet):
    return [c for c in row if c in alphabet]


class TestSparkline(PanelCase):
    def put_week(self, values):
        self.put("alpha", tokens=make_tokens(by_day_rows(FIRST_DAY, values)))

    def test_scales_to_the_peak_and_states_it_in_words(self):
        self.put_week(WEEK)
        row = spark_row(self.text())
        g = glyphs_of(row, UNI_SPARK)
        self.assertEqual(len(g), 7)
        self.assertEqual(g[3], "█")  # the peak day
        self.assertEqual(g.index(max(g)), 3)
        self.assertIn("max 900k", row)
        self.assertLess(UNI_SPARK.index(g[0]), UNI_SPARK.index(g[1]))  # monotone with the data

    def test_date_labels_show_first_and_last_day(self):
        self.put_week(WEEK)
        out = "\n".join(self.text())
        self.assertRegex(out, r"10-15 \.+ 10-21")

    def test_all_zero_is_a_flat_line_with_max_zero(self):
        self.put_week([0] * 7)
        row = spark_row(self.text())
        self.assertEqual(set(glyphs_of(row, UNI_SPARK)), {"▁"})
        self.assertIn("max 0", row)

    def test_one_non_zero_day_stands_alone(self):
        self.put_week([0, 0, 0, 5, 0, 0, 0])
        g = glyphs_of(spark_row(self.text()), UNI_SPARK)
        self.assertEqual(g, ["▁", "▁", "▁", "█", "▁", "▁", "▁"])

    def test_a_tiny_non_zero_day_is_distinct_from_a_zero_day(self):
        self.put_week([0, 1, 10 ** 12, 0, 0, 0, 0])
        g = glyphs_of(spark_row(self.text()), UNI_SPARK)
        self.assertEqual(g[0], "▁")
        self.assertNotEqual(g[1], "▁")  # 1 token next to a trillion still shows as non-zero
        self.assertEqual(g[2], "█")

    def test_huge_values_are_scaled_without_float_trouble(self):
        self.put_week([hub.MAX_COUNT, hub.MAX_COUNT // 2, 1, 0, hub.MAX_COUNT, 3, 7])
        row = spark_row(self.text())
        g = glyphs_of(row, UNI_SPARK)
        self.assertEqual((g[0], g[4]), ("█", "█"))
        self.assertIn("max ", row)

    def test_every_glyph_is_one_of_the_eight_levels(self):
        self.put_week([3, 1, 4, 1, 5, 9, 2])
        row = spark_row(self.text())
        self.assertTrue(all(c in UNI_SPARK for c in glyphs_of(row, UNI_SPARK)))

    def test_fewer_than_seven_days_show_what_there_is(self):
        self.put("alpha", tokens=make_tokens(by_day_rows("2026-10-20", [2, 4])))
        g = glyphs_of(spark_row(self.text()), UNI_SPARK)
        self.assertEqual(len(g), 2)

    def test_a_single_day_has_one_label(self):
        self.put("alpha", tokens=make_tokens(by_day_rows("2026-10-21", [2])))
        out = "\n".join(self.text())
        self.assertIn("10-21", out)
        self.assertNotRegex(out, r"10-21 \.+")

    def test_no_data_says_so_not_a_row_of_zeros(self):
        self.put("alpha", tokens=NULL)
        texts = self.text()
        self.assertIn("OUTPUT TOKENS / DAY", "\n".join(texts))
        self.assertTrue(any("no data" in t and "│" in t[1:-1] for t in texts), texts)
        self.assertFalse(any(" max " in t for t in texts))

    def test_ascii_mode_uses_ascii_levels_only(self):
        self.put_week(WEEK)
        texts = self.text(ascii_mode=True)
        self.assertTrue(all(ord(c) < 128 for c in "\n".join(texts)))
        row = spark_row(texts)
        g = glyphs_of(row, ASCII_SPARK)
        self.assertIn("#", g)
        self.assertIn("max 900k", row)

    def test_ascii_zero_and_tiny_are_distinct(self):
        self.put_week([0, 1, 10 ** 12, 0, 0, 0, 0])
        # Restrict to the sparkline cells: the row also carries the border and the label.
        row = spark_row(self.text(ascii_mode=True))
        cells = row.split("|")[1].split("max")[0].split()
        self.assertEqual(cells[0], "_")
        self.assertNotEqual(cells[1], "_")
        self.assertEqual(cells[2], "#")

    def test_no_color_keeps_meaning_in_text(self):
        self.put_week(WEEK)
        lines = self.lines(color=False)
        for ln in lines:
            for text, tag in ln:
                self.assertIn(tag, (view.PLAIN, view.BOLD))
        self.assertIn("max 900k", spark_row(view.plain_text(lines)))


# ---------------------------------------------------------------------------
# view: leaderboard
# ---------------------------------------------------------------------------


def board_rows(texts):
    """Rendered leaderboard rows as (rank, type, invocations) for rows that look like one."""
    out = []
    for t in texts:
        m = re.search(r"\s(\d+) ([A-Za-z0-9_.~-]+)\s+[#█]*\s+(\d+[kMG]?)(?:\s+[\d.]+[kMG]?|\s+-)?\s*[│|]$", t)
        if m:
            out.append((int(m.group(1)), m.group(2), m.group(3)))
    return out


class TestLeaderboardView(PanelCase):
    def setUp(self):
        super().setUp()
        self.put("alpha", agents=make_agents(agent_row("main", 38, 900000),
                                             agent_row("core-researcher", 5, 100000),
                                             agent_row("grid-backend-dev", 7, 120000)),
                 tokens=make_tokens(by_day_rows(FIRST_DAY, WEEK)))
        self.put("beta", agents=make_agents(agent_row("core-researcher", 5, 110000)))

    def test_rows_are_ranked_and_cross_host_summed(self):
        rows = board_rows(self.text())
        self.assertEqual(rows, [(1, "main", "38"), (2, "core-researcher", "10"),
                                (3, "grid-backend-dev", "7")])

    def test_panel_titles(self):
        out = "\n".join(self.text())
        self.assertIn("SUBAGENTS (7d, invocations)", out)
        self.assertIn("OUTPUT TOKENS / DAY (7d)", out)

    def test_output_tokens_are_a_dim_second_column_when_width_allows(self):
        lines = self.lines(width=120, height=40)
        main = [ln for ln in lines if any("main" in t for t, _ in ln)][0]
        self.assertIn(("  900k", view.DIM), main)

    def test_bars_are_proportional_to_the_top_row(self):
        texts = self.text(width=120, height=40)
        bars = {}
        for t in texts:
            for name in ("main", "core-researcher", "grid-backend-dev"):
                if " %s " % name in t or (name in t and "█" in t):
                    bars[name] = t.count("█")
        self.assertGreater(bars["main"], bars["core-researcher"])
        self.assertGreater(bars["core-researcher"], bars["grid-backend-dev"])
        self.assertGreaterEqual(bars["grid-backend-dev"], 1)

    def test_ascii_mode_bars_are_hashes(self):
        texts = self.text(ascii_mode=True, width=120, height=40)
        self.assertTrue(all(ord(c) < 128 for c in "\n".join(texts)))
        self.assertTrue(any("main" in t and "#" in t for t in texts))

    def test_no_agents_says_so(self):
        for h in ("alpha", "beta"):
            self.put(h, agents=NULL)
        out = "\n".join(self.text())
        self.assertIn("no subagent data", out)

    def test_hostile_type_never_reaches_the_screen_as_a_control_character(self):
        self.put("gamma", agents=make_agents(agent_row("\x1b[31mred\x1b[0m\x07", 50)))
        for ln in self.lines(width=120, height=40):
            for text, _ in ln:
                self.assertFalse(any(ord(c) < 32 or ord(c) == 127 for c in text), repr(text))

    def test_long_type_is_clipped_to_the_column(self):
        self.put("gamma", agents=make_agents(agent_row("y" * 100, 99)))
        for w in (80, 120):
            texts = self.text(width=w, height=40)
            self.assertTrue(all(len(t) == w for t in texts))
            self.assertTrue(any("yyyy" in t for t in texts))

    def test_huge_invocations_do_not_widen_the_row(self):
        self.put("gamma", agents=make_agents(agent_row("busy", hub.MAX_COUNT)))
        self.put("delta", agents=make_agents(agent_row("busy", hub.MAX_COUNT)))
        for w in (60, 80, 120):
            texts = self.text(width=w, height=40)
            self.assertTrue(all(len(t) == w for t in texts), w)
            self.assertTrue(any("busy" in t for t in texts))

    def test_stale_host_agents_are_not_shown(self):
        self.put("gamma", age_s=41 * 60, agents=make_agents(agent_row("ghost-agent", 500)))
        self.assertNotIn("ghost-agent", "\n".join(self.text()))


# ---------------------------------------------------------------------------
# view: layout and degradation
# ---------------------------------------------------------------------------


class TestPanelSizes(PanelCase):
    def fleet(self, hosts=2):
        for i in range(hosts):
            self.put("host%d" % i, agents=make_agents(*[agent_row("agent%d" % k, 20 - k, 1000 * k)
                                                        for k in range(10)]),
                     tokens=make_tokens(by_day_rows(FIRST_DAY, WEEK)))

    def check(self, width, height, ascii_mode=False):
        texts = self.text(width=width, height=height, ascii_mode=ascii_mode)
        self.assertLessEqual(len(texts), height)
        self.assertTrue(all(len(t) == width for t in texts), [len(t) for t in texts])
        return texts

    def test_80x24_and_120x40_fit_and_keep_hosts_limits_and_footer(self):
        self.fleet(2)
        for w, h in ((80, 24), (120, 40)):
            for ascii_mode in (False, True):
                texts = self.check(w, h, ascii_mode)
                out = "\n".join(texts)
                self.assertIn("LIMITS", out)
                self.assertIn("HOSTS", out)
                self.assertIn("host0", out)
                self.assertTrue(any(t.strip("│| ").startswith("total") for t in texts))
                self.assertIn("2 of 2 hosts fresh", out)
                self.assertIn("OUTPUT TOKENS / DAY", out)
                self.assertEqual(len(board_rows(texts)), 8)  # the full top 8 fits

    def test_vertical_divider_lines_up_with_its_junctions(self):
        self.fleet(2)
        texts = self.check(80, 24)
        head = [t for t in texts if "OUTPUT TOKENS" in t][0]
        col = head.index("┬")
        body = [t for t in texts if "SUBAGENTS" not in t and t.startswith("│") and t[col] == "│"]
        self.assertGreaterEqual(len(body), 5)
        closing = texts[texts.index(head) + 1 + 8]  # 8 content rows, then the closing rule
        self.assertEqual(closing[col], "┴")

    def test_ascii_junctions(self):
        self.fleet(2)
        texts = self.check(80, 24, ascii_mode=True)
        self.assertTrue(all(ord(c) < 128 for c in "\n".join(texts)))

    def test_text_capture_120x40_and_80x24_have_no_overlong_lines(self):
        self.fleet(3)
        for w, h in ((80, 24), (120, 40), (100, 30), (72, 20)):
            for t in self.text(width=w, height=h):
                self.assertLessEqual(len(t), w)

    def test_narrow_terminal_drops_the_sparkline_and_keeps_the_leaderboard(self):
        self.fleet(1)
        for w in (60, 66, 71):
            texts = self.check(w, 30)
            out = "\n".join(texts)
            self.assertNotIn("OUTPUT TOKENS", out)
            self.assertFalse(any(" max " in t for t in texts))
            self.assertIn("SUBAGENTS", out)
            self.assertGreaterEqual(len(board_rows(texts)), 5)

    def test_side_by_side_starts_at_the_threshold_width(self):
        self.fleet(1)
        self.assertIn("OUTPUT TOKENS", "\n".join(self.check(view.SIDE_BY_SIDE_MIN_WIDTH, 30)))
        self.assertNotIn("OUTPUT TOKENS", "\n".join(self.check(view.SIDE_BY_SIDE_MIN_WIDTH - 1, 30)))

    def test_short_terminal_truncates_the_leaderboard_not_the_hosts(self):
        self.fleet(1)  # 80x16: 6 free rows, the host takes 2, the panel gets header + 3
        texts = self.check(80, 16)
        out = "\n".join(texts)
        self.assertIn("host0", out)
        self.assertIn("1 of 1 hosts fresh", out)
        rows = board_rows(texts)
        self.assertEqual([r[0] for r in rows], [1, 2, 3])  # the top of the board survives
        self.assertIn("OUTPUT TOKENS", out)

    def test_one_free_content_row_drops_the_sparkline_first(self):
        self.fleet(2)  # 80x16: 2 hosts x 2 rows leaves header + 1 row
        texts = self.check(80, 16)
        out = "\n".join(texts)
        self.assertNotIn("OUTPUT TOKENS", out)
        self.assertEqual([r[:2] for r in board_rows(texts)], [(1, "agent0")])
        self.assertIn("host1", out)

    def test_no_free_rows_means_no_panel_and_the_hosts_table_is_untouched(self):
        self.fleet(3)  # 80x16: 3 hosts x 2 rows exactly fill the table; nothing is left over
        out = "\n".join(self.check(80, 16))
        self.assertNotIn("SUBAGENTS", out)
        self.assertNotIn("OUTPUT TOKENS", out)
        self.assertIn("host2", out)
        self.assertNotIn("more hosts", out)
        self.put("host3", agents=make_agents(agent_row("main", 1)))  # now the table truncates
        out = "\n".join(self.check(80, 16))
        self.assertRegex(out, r"\d+ more hosts not shown")
        self.assertNotIn("SUBAGENTS", out)

    def test_hosts_are_never_sacrificed_for_the_panel(self):
        for n in range(1, 8):
            self.put("h%d" % n, agents=make_agents(agent_row("main", 5)))
            texts = self.text(width=80, height=24)
            out = "\n".join(texts)
            shown = len([t for t in texts if re.match(r"│ h\d ", t)])
            if "more hosts" in out:
                self.assertNotIn("SUBAGENTS", out)  # truncated table => no panel at all
            else:
                self.assertEqual(shown, n)

    def test_too_small_message_is_unchanged(self):
        self.fleet(1)
        for w, h in ((59, 40), (80, 15), (50, 10)):
            texts = self.text(width=w, height=h)
            self.assertEqual(len(texts), 1)
            self.assertIn("terminal too small", texts[0])

    def test_empty_inbox_still_renders_a_sane_panel(self):
        texts = self.check(80, 24)
        out = "\n".join(texts)
        self.assertIn("no snapshots in the inbox yet", out)
        self.assertIn("no subagent data", out)

    def test_no_color_has_no_colour_tags_anywhere(self):
        self.fleet(2)
        for ln in self.lines(width=120, height=40, color=False):
            for _, tag in ln:
                self.assertIn(tag, (view.PLAIN, view.BOLD))


class TestPurity(PanelCase):
    def test_render_is_deterministic_and_does_not_mutate_the_state(self):
        self.put("alpha", agents=make_agents(agent_row("main", 3)),
                 tokens=make_tokens(by_day_rows(FIRST_DAY, WEEK)))
        state = self.hubstate()
        a = view.render(state, 80, 24)
        b = view.render(state, 80, 24)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
