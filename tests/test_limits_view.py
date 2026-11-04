"""Limits strip, view half of task 7.4 (spec: hub-view-tui "Limits shown once per account").

Two layers, both driven by SYNTHETIC snapshots carrying a `limits` section shaped exactly as
snapshot-collect / limits-capture define it (helpers.synthetic_snapshot.make_limits):

  - hub.build_limits / build_state: per account, per window, which capture wins, expiry,
    unknown-not-zero, stale hosts excluded, hostile numbers.
  - view.render: the strip's text, colour tags, NO_COLOR, ASCII, sizes.

Nothing here touches the collect/tap side (limits.py, snapshot.py); that is the other half
of 7.4. All names are invented. These tests verify my own work only; independent
verification is qa-engineer's.
"""

import os
import tempfile
import unittest

from grid_fleet import hub, view
from helpers.synthetic_snapshot import (NOW, NULL, agent_row, by_day_rows, limit_window,
                                        make_agents, make_limits, make_snapshot, make_tokens,
                                        write_inbox_file)

# Reset instants relative to the hub clock: 5h window in 2h07m, 7d window in 4d19h.
FIVE_RESET = NOW + 2 * 3600 + 7 * 60
SEVEN_RESET = NOW + 4 * 86400 + 19 * 3600


def both(five_pct=42.0, seven_pct=18.0, captured_at=NOW - 120):
    """A limits section with both windows live."""
    return make_limits(captured_at, limit_window(five_pct, FIVE_RESET),
                       limit_window(seven_pct, SEVEN_RESET))


class LimitsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = self.tmp.name

    def put(self, host, age_s=180, **kw):
        write_inbox_file(self.state_dir, host + ".json", make_snapshot(host, **kw), NOW - age_s)

    def hubstate(self):
        return hub.read_hub(self.state_dir, NOW)

    def host_status(self, name):
        return [h for h in self.hubstate().hosts if h.name == name][0].status

    def block(self, account="default"):
        return [b for b in self.hubstate().limits if b.account == account][0]

    def text(self, width=80, height=24, ascii_mode=False, color=True):
        return view.plain_text(view.render(self.hubstate(), width, height, ascii_mode, color))

    def lines(self, **kw):
        return view.render(self.hubstate(), kw.pop("width", 80), kw.pop("height", 24),
                           kw.pop("ascii_mode", False), kw.pop("color", True))

    def row(self, label, texts=None, nth=0):
        """The nth rendered strip row for `label` (5h / 7d), border stripped."""
        texts = texts if texts is not None else self.text()
        rows = [t.strip("│| ") for t in texts if t.strip("│| ").startswith(label + " ")]
        return rows[nth]


# ---------------------------------------------------------------------------
# hub: aggregation
# ---------------------------------------------------------------------------


class TestAggregation(LimitsCase):
    def test_three_hosts_one_account_latest_capture_wins(self):
        self.put("alpha", limits=both(30.0, 10.0, captured_at=NOW - 3000))
        self.put("beta", limits=both(55.0, 20.0, captured_at=NOW - 120))  # the latest
        self.put("gamma", limits=both(48.0, 15.0, captured_at=NOW - 600))
        s = self.hubstate()
        self.assertEqual(len(s.limits), 1)
        b = s.limits[0]
        self.assertEqual((b.account, b.five_hour.pct, b.seven_day.pct), ("default", 55.0, 20.0))
        self.assertEqual(b.five_hour.age_s, 120)  # measured on the hub clock

    def test_windows_are_chosen_independently(self):
        # alpha has the newest capture but only the 5h window; the 7d value must come from beta.
        self.put("alpha", limits=make_limits(NOW - 60, limit_window(70.0, FIVE_RESET)))
        self.put("beta", limits=make_limits(NOW - 900, limit_window(10.0, FIVE_RESET),
                                            limit_window(33.0, SEVEN_RESET)))
        b = self.block()
        self.assertEqual((b.five_hour.pct, b.five_hour.age_s), (70.0, 60))
        self.assertEqual((b.seven_day.pct, b.seven_day.age_s), (33.0, 900))

    def test_expired_window_is_unknown(self):
        self.put("alpha", limits=make_limits(NOW - 60, limit_window(42.0, NOW - 1),
                                             limit_window(18.0, SEVEN_RESET)))
        b = self.block()
        self.assertIsNone(b.five_hour.pct)
        self.assertTrue(b.five_hour.expired)
        self.assertEqual(b.seven_day.pct, 18.0)  # the other window is unaffected

    def test_reset_exactly_now_counts_as_expired(self):
        # design.md: unknown once now >= resets_at.
        self.put("alpha", limits=make_limits(NOW - 60, limit_window(42.0, NOW)))
        self.assertTrue(self.block().five_hour.expired)

    def test_no_limits_anywhere_is_unknown_for_both(self):
        self.put("alpha")  # limits null
        self.put("beta", limits=NULL)
        b = self.hubstate().limits
        self.assertEqual(len(b), 1)
        for w in (b[0].five_hour, b[0].seven_day):
            self.assertEqual((w.pct, w.age_s, w.expired), (None, None, False))

    def test_empty_inbox_still_has_one_unknown_block(self):
        s = self.hubstate()
        self.assertEqual(s.limits, (hub.AccountLimits(""),))

    def test_two_accounts_get_a_block_each(self):
        self.put("alpha", account="team-a", limits=both(40.0, 11.0))
        self.put("beta", account="team-b", limits=both(90.0, 77.0))
        s = self.hubstate()
        self.assertEqual([b.account for b in s.limits], ["team-a", "team-b"])
        self.assertEqual([b.five_hour.pct for b in s.limits], [40.0, 90.0])

    def test_absent_window_is_unknown_not_zero(self):
        self.put("alpha", limits=make_limits(NOW - 60, limit_window(42.0, FIVE_RESET)))
        b = self.block()
        self.assertEqual(b.five_hour.pct, 42.0)
        self.assertIsNone(b.seven_day.pct)  # None, never 0.0
        self.assertFalse(b.seven_day.expired)

    # -- stale hosts (maintainer decision: limits belong to the account, not the host) ------

    def test_stale_host_holding_the_newest_capture_wins(self):
        # The stale host holds the NEWEST capture: it wins, and its age is the capture age
        # (hub now - captured_at), not the age of its inbox file.
        self.put("alpha", limits=both(30.0, 10.0, captured_at=NOW - 900))
        self.put("beta", age_s=41 * 60, limits=both(95.0, 90.0, captured_at=NOW - 30))
        self.assertEqual(self.host_status("beta"), hub.STALE)  # the premise of the test
        b = self.block()
        self.assertEqual((b.five_hour.pct, b.seven_day.pct), (95.0, 90.0))
        self.assertEqual((b.five_hour.age_s, b.seven_day.age_s), (30, 30))

    def test_fresh_host_with_a_newer_capture_still_beats_a_stale_one(self):
        # Staleness is irrelevant to the contest: only captured_at decides.
        self.put("alpha", limits=both(30.0, 10.0, captured_at=NOW - 30))
        self.put("beta", age_s=41 * 60, limits=both(95.0, 90.0, captured_at=NOW - 900))
        b = self.block()
        self.assertEqual((b.five_hour.pct, b.seven_day.pct), (30.0, 10.0))

    def test_stale_host_can_supply_one_window_while_a_fresh_host_supplies_the_other(self):
        self.put("alpha", limits=make_limits(NOW - 60, limit_window(70.0, FIVE_RESET)))
        self.put("beta", age_s=41 * 60, limits=make_limits(NOW - 900, limit_window(10.0, FIVE_RESET),
                                                           limit_window(33.0, SEVEN_RESET)))
        b = self.block()
        self.assertEqual((b.five_hour.pct, b.five_hour.age_s), (70.0, 60))
        self.assertEqual((b.seven_day.pct, b.seven_day.age_s), (33.0, 900))

    def test_stale_only_account_shows_a_block_with_its_capture_age(self):
        self.put("alpha", account="team-a", limits=both(40.0, 11.0))
        self.put("beta", account="team-b", age_s=41 * 60,
                 limits=both(61.0, 22.0, captured_at=NOW - 2400))
        s = self.hubstate()
        self.assertEqual([b.account for b in s.limits], ["team-a", "team-b"])
        b = self.block("team-b")
        self.assertEqual((b.five_hour.pct, b.seven_day.pct), (61.0, 22.0))
        self.assertEqual((b.five_hour.age_s, b.seven_day.age_s), (2400, 2400))

    def test_only_stale_hosts_gives_a_named_block_not_the_unnamed_unknown_one(self):
        self.put("alpha", age_s=41 * 60, limits=both(61.0, 22.0))
        s = self.hubstate()
        self.assertEqual(s.fresh, 0)
        self.assertEqual([b.account for b in s.limits], ["default"])
        self.assertEqual((s.limits[0].five_hour.pct, s.limits[0].seven_day.pct), (61.0, 22.0))

    def test_stale_host_without_limits_still_creates_an_all_unknown_block(self):
        # Same rule as for a fresh host: a snapshot without limits names its account.
        self.put("alpha", account="team-a", age_s=41 * 60, limits=NULL)
        b = self.block("team-a")
        self.assertEqual((b.five_hour, b.seven_day), (hub.LimitWindow(), hub.LimitWindow()))

    def test_stale_hosts_expired_window_renders_unknown(self):
        # The expiry rule keeps a stale capture honest: its 5h window has reset since.
        self.put("alpha", age_s=41 * 60, limits=make_limits(
            NOW - 2400, limit_window(42.0, NOW - 1), limit_window(18.0, SEVEN_RESET)))
        b = self.block()
        self.assertIsNone(b.five_hour.pct)
        self.assertTrue(b.five_hour.expired)
        self.assertEqual((b.seven_day.pct, b.seven_day.age_s), (18.0, 2400))

    def test_a_three_day_old_stale_capture_with_a_future_reset_is_kept(self):
        # A 7d window captured 3d04h ago still resets in the future: shown, with its age.
        age = 3 * 86400 + 4 * 3600
        self.put("alpha", age_s=age, limits=make_limits(NOW - age, None, limit_window(64.0, SEVEN_RESET)))
        b = self.block()
        self.assertEqual((b.seven_day.pct, b.seven_day.age_s), (64.0, age))
        self.assertIsNone(b.five_hour.pct)  # nothing captured for 5h: unknown, not 0

    def test_future_dated_capture_on_a_stale_host_is_still_ignored(self):
        self.put("alpha", limits=both(30.0, 10.0, captured_at=NOW - 900))
        self.put("beta", age_s=41 * 60,
                 limits=both(95.0, 90.0, captured_at=NOW + hub.MAX_CAPTURE_SKEW_S + 1))
        b = self.block()
        self.assertEqual((b.five_hour.pct, b.seven_day.pct), (30.0, 10.0))

    def test_stale_host_is_still_excluded_from_totals_leaderboard_and_sparkline(self):
        # Only the limits strip changes: the other aggregates keep dropping stale hosts.
        self.put("alpha", sessions={"busy": 2, "waiting": 1}, limits=both(30.0, 10.0),
                 agents=make_agents(agent_row("core-researcher", 5)),
                 tokens=make_tokens(by_day_rows("2026-10-20", [1] * 7)))
        self.put("beta", age_s=41 * 60, sessions={"busy": 9, "waiting": 9},
                 limits=both(95.0, 90.0, captured_at=NOW - 30),
                 agents=make_agents(agent_row("core-researcher", 5), agent_row("stale-only", 99)),
                 tokens=make_tokens(by_day_rows("2026-10-20", [100] * 7)))
        s = self.hubstate()
        self.assertEqual(s.limits[0].five_hour.pct, 95.0)  # the strip includes beta ...
        self.assertEqual((s.fresh, s.busy.value, s.waiting.value), (1, 2, 1))  # ... nothing else does
        self.assertEqual(s.out_24h, hub.Total(412000, True))
        self.assertEqual([(a.type, a.invocations) for a in s.agents], [("core-researcher", 5)])
        self.assertEqual([d.output for d in s.by_day], [1] * 7)

    def test_clock_skew_between_hosts_does_not_matter_for_age(self):
        # Age is hub now - captured_at; a capture stamped slightly in the future (within the
        # hub's skew allowance, S5) clamps to 0. One far in the future is ignored: see
        # test_hub.TestHostileFreshnessFields.
        self.put("alpha", limits=both(captured_at=NOW + 60))
        self.assertEqual(self.block().five_hour.age_s, 0)

    def test_account_label_is_terminal_safe(self):
        self.put("alpha", account="\x1b[31mred\x07\n" + "x" * 100, limits=both())
        label = self.hubstate().limits[0].account
        self.assertTrue(all(" " <= c <= "~" for c in label), repr(label))
        self.assertLessEqual(len(label), hub.ACCOUNT_LABEL_MAX)

    def test_missing_or_non_string_account_falls_back_to_default(self):
        snap = make_snapshot("alpha", limits=both())
        del snap["account"]
        write_inbox_file(self.state_dir, "alpha.json", snap, NOW - 180)
        self.put("beta", account=7, limits=both())
        self.assertEqual([b.account for b in self.hubstate().limits], ["default"])


class TestHostileLimits(LimitsCase):
    """Whatever a (compromised or buggy) spoke sends, the hub state builds and the view renders."""

    BAD_NUMBERS = [10 ** 400, -5, "50", None, True, [], {}, float("nan"), float("inf"),
                   float("-inf"), 5000.0, 1e308]

    def test_bad_used_pct_is_unknown(self):
        for bad in self.BAD_NUMBERS:
            lim = make_limits(NOW - 60, limit_window(bad, FIVE_RESET), limit_window(20.0, SEVEN_RESET))
            self.put("alpha", limits=lim)
            b = self.block()
            self.assertIsNone(b.five_hour.pct, repr(bad))
            self.assertEqual(b.seven_day.pct, 20.0, repr(bad))
            self.assertIn("hosts fresh", "\n".join(self.text()))  # renders in full, footer included

    def test_bad_resets_at_is_unknown(self):
        for bad in self.BAD_NUMBERS + [0, 10 ** 12]:
            self.put("alpha", limits=make_limits(NOW - 60, limit_window(42.0, bad)))
            self.assertIsNone(self.block().five_hour.pct, repr(bad))
            self.text()

    def test_bad_captured_at_drops_the_whole_limits_section(self):
        # 5000.0 is a believable (if ancient) epoch for captured_at, so it is not "bad" here.
        for bad in [b for b in self.BAD_NUMBERS if b != 5000.0] + [0, 10 ** 12]:
            self.put("alpha", limits=make_limits(bad, limit_window(42.0, FIVE_RESET)))
            self.assertIsNone(self.block().five_hour.pct, repr(bad))
            self.text()

    def test_limits_or_windows_of_the_wrong_shape(self):
        for bad in ("oops", 7, [1, 2], True):
            self.put("alpha", limits=bad)
            self.assertIsNone(self.block().five_hour.pct)
            self.text()
            lim = {"captured_at": NOW - 60, "five_hour": bad, "seven_day": bad}
            self.put("alpha", limits=lim)
            self.assertIsNone(self.block().seven_day.pct)
            self.text()

    def test_percentage_slightly_over_100_is_clamped_to_full(self):
        self.put("alpha", limits=make_limits(NOW - 60, limit_window(100.4, FIVE_RESET)))
        self.assertEqual(self.block().five_hour.pct, 100.0)

    def test_negative_percentage_is_not_shown_as_zero(self):
        self.put("alpha", limits=make_limits(NOW - 60, limit_window(-5, FIVE_RESET)))
        self.assertIsNone(self.block().five_hour.pct)
        self.assertNotIn("0%", self.row("5h"))

    def test_one_bad_host_does_not_hide_a_good_one(self):
        # The bad host has the newest captured_at but an unusable 5h window.
        self.put("alpha", limits=make_limits(NOW - 10, limit_window("lots", FIVE_RESET)))
        self.put("beta", limits=both(33.0, 12.0, captured_at=NOW - 600))
        self.assertEqual(self.block().five_hour.pct, 33.0)


# ---------------------------------------------------------------------------
# view: rendering
# ---------------------------------------------------------------------------


class TestStripText(LimitsCase):
    def test_one_bar_per_window_for_three_hosts_one_account(self):
        self.put("alpha", limits=both(30.0, 10.0, captured_at=NOW - 3000))
        self.put("beta", limits=both(55.0, 20.0, captured_at=NOW - 120))
        self.put("gamma", limits=both(48.0, 15.0, captured_at=NOW - 600))
        texts = self.text()
        out = "\n".join(texts)
        self.assertEqual(out.count("LIMITS"), 1)
        self.assertEqual(len([t for t in texts if t.strip("│ ").startswith("5h ")]), 1)
        self.assertEqual(len([t for t in texts if t.strip("│ ").startswith("7d ")]), 1)
        self.assertIn("55%", self.row("5h", texts))
        self.assertIn("20%", self.row("7d", texts))
        self.assertIn("as of 2m ago", self.row("5h", texts))
        self.assertIn("account default", out)

    def test_reset_time_and_countdown_are_shown(self):
        self.put("alpha", limits=both())
        texts = self.text()
        self.assertRegex(self.row("5h", texts), r"resets \d\d:\d\d \(in 2h07m\)")
        self.assertRegex(self.row("7d", texts), r"resets (Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d\d:\d\d \(in 4d19h\)")

    def test_strip_sits_above_the_hosts_table(self):
        self.put("alpha", limits=both())
        texts = self.text()
        idx = lambda needle: [i for i, t in enumerate(texts) if needle in t][0]  # noqa: E731
        self.assertLess(idx("LIMITS"), idx("5h "))
        self.assertLess(idx("7d "), idx("HOSTS"))

    def test_window_expired_renders_unknown(self):
        self.put("alpha", limits=make_limits(NOW - 60, limit_window(42.0, NOW - 1),
                                             limit_window(18.0, SEVEN_RESET)))
        texts = self.text()
        self.assertIn("unknown", self.row("5h", texts))
        self.assertNotIn("42%", "\n".join(texts))  # the stale percentage must not leak
        self.assertIn("18%", self.row("7d", texts))

    def test_no_limits_anywhere_shows_unknown_for_both(self):
        self.put("alpha")
        texts = self.text()
        self.assertIn("unknown", self.row("5h", texts))
        self.assertIn("unknown", self.row("7d", texts))

    def test_empty_inbox_still_draws_the_strip(self):
        texts = self.text()
        self.assertTrue(any("LIMITS" in t for t in texts))
        self.assertIn("unknown", self.row("5h", texts))
        self.assertFalse(any("account" in t for t in texts))  # nothing names an account

    def test_two_accounts_two_blocks(self):
        self.put("alpha", account="team-a", limits=both(40.0, 11.0))
        self.put("beta", account="team-b", limits=both(90.0, 77.0))
        texts = self.text()
        out = "\n".join(texts)
        self.assertEqual(out.count("LIMITS"), 2)
        self.assertIn("account team-a", out)
        self.assertIn("account team-b", out)
        self.assertIn("40%", self.row("5h", texts, 0))
        self.assertIn("90%", self.row("5h", texts, 1))

    def test_absent_window_is_unknown_never_zero_percent(self):
        self.put("alpha", limits=make_limits(NOW - 60, limit_window(42.0, FIVE_RESET)))
        texts = self.text()
        self.assertIn("42%", self.row("5h", texts))
        self.assertIn("unknown", self.row("7d", texts))
        self.assertNotIn("0%", self.row("7d", texts))

    def test_stale_host_holding_the_newest_limits_is_shown_with_its_capture_age(self):
        self.put("alpha", limits=both(30.0, 10.0, captured_at=NOW - 900))
        self.put("beta", age_s=41 * 60, limits=both(95.0, 90.0, captured_at=NOW - 30))
        texts = self.text()
        self.assertIn("95%", self.row("5h", texts))
        self.assertIn("90%", self.row("7d", texts))
        self.assertIn("as of <1m ago", self.row("5h", texts))
        self.assertNotIn("30%", "\n".join(texts))

    def test_stale_only_account_gets_a_named_block(self):
        self.put("alpha", account="team-a", limits=both(40.0, 11.0))
        self.put("beta", account="team-b", age_s=41 * 60,
                 limits=both(61.0, 22.0, captured_at=NOW - 2400))
        texts = self.text()
        out = "\n".join(texts)
        self.assertEqual(out.count("LIMITS"), 2)
        self.assertIn("account team-b", out)
        self.assertIn("61%", self.row("5h", texts, 1))
        self.assertIn("as of 40m ago", self.row("5h", texts, 1))

    def test_three_day_old_capture_label_stays_one_short_token(self):
        # fmt_age gives '<days>d<hh>h' from 24 h up: one unspaced token, so the row keeps its
        # `as of <age> ago` shape and fits the narrow layouts.
        age = 3 * 86400 + 4 * 3600
        self.put("alpha", age_s=age, limits=make_limits(NOW - age, None, limit_window(64.0, SEVEN_RESET)))
        row = self.row("7d")
        self.assertIn("64%", row)
        self.assertIn("as of 3d04h ago", row)
        self.assertIn("unknown", self.row("5h"))

    def test_80x24_capture_with_a_stale_host_holding_the_newest_limits(self):
        # Fixture for the hand-off: alpha is fresh with an older capture, beta went quiet
        # 41 min ago but holds the newest capture. The strip shows beta's numbers; the hosts
        # table still greys beta out and keeps it out of the totals.
        self.put("alpha", limits=both(30.0, 10.0, captured_at=NOW - 900))
        self.put("beta", age_s=41 * 60, limits=both(95.0, 90.0, captured_at=NOW - 30))
        texts = self.text(80, 24)
        self.assertLessEqual(len(texts), 24)  # render draws only what it has, not padding
        self.assertTrue(all(len(t) <= 80 for t in texts))
        self.assertIn("stale 41m", "\n".join(texts))
        if os.environ.get("GRID_FLEET_PRINT_CAPTURE"):  # opt-in: print the capture for reports
            print("\n".join(texts))

    def test_old_capture_age_is_printed(self):
        self.put("alpha", limits=both(captured_at=NOW - 2 * 3600 - 300))
        self.assertIn("as of 2h05m ago", self.row("5h"))


class TestStripColour(LimitsCase):
    def bar_tag(self, lines, label):
        """Tag of the bar+percent spans on the `label` row."""
        for ln in lines:
            spans = [(t, tag) for t, tag in ln]
            if any(t.strip().startswith(label) and tag == view.BOLD for t, tag in spans):
                return spans[2][1]  # border, prefix, bar
        raise AssertionError("no %s row" % label)

    def test_thresholds_green_yellow_red(self):
        cases = [(0.0, view.OK), (42.0, view.OK), (59.9, view.OK), (60.0, view.WARN),
                 (84.9, view.WARN), (85.0, view.CRIT), (100.0, view.CRIT)]
        for pct, want in cases:
            self.put("alpha", limits=both(pct, 10.0))
            self.assertEqual(self.bar_tag(self.lines(), "5h"), want, pct)

    def test_capture_older_than_an_hour_is_dim(self):
        self.put("alpha", limits=both(90.0, 10.0, captured_at=NOW - 3601))
        self.assertEqual(self.bar_tag(self.lines(), "5h"), view.DIM)
        self.put("alpha", limits=both(90.0, 10.0, captured_at=NOW - 3600))
        self.assertEqual(self.bar_tag(self.lines(), "5h"), view.CRIT)  # exactly 1 h is not "over"

    def test_unknown_is_grey(self):
        self.put("alpha")
        self.assertEqual(self.bar_tag(self.lines(), "7d"), view.GREY)

    def test_no_color_emits_only_plain_and_bold_but_keeps_the_meaning(self):
        self.put("alpha", limits=both(90.0, 10.0))
        self.put("beta", account="other")  # an unknown block too
        lines = self.lines(color=False)
        self.assertTrue({tag for ln in lines for _, tag in ln} <= {view.PLAIN, view.BOLD})
        out = "\n".join(view.plain_text(lines))
        self.assertIn("90%", out)       # severity is the number
        self.assertIn("as of 2m ago", out)
        self.assertIn("unknown", out)
        self.assertEqual(view.plain_text(lines), self.text(color=True))  # same text either way


class TestStripGlyphs(LimitsCase):
    def test_unicode_bar_proportions(self):
        self.put("alpha", limits=both(50.0, 10.0))
        row = self.row("5h")
        self.assertEqual(row.count("█"), 10)  # 50 % of a 20-cell bar
        self.assertEqual(row.count("░"), 10)

    def test_partial_cells_use_eighth_blocks(self):
        self.put("alpha", limits=both(42.0, 10.0))  # 42 % of 160 eighths = 67 = 8 full + 3/8
        row = self.row("5h")
        self.assertEqual(row.count("█"), 8)
        self.assertIn("▍", row)

    def test_ascii_mode_is_pure_ascii_with_hash_and_dot_bars(self):
        self.put("alpha", limits=both(50.0, 10.0))
        self.put("beta", account="other")
        for width in (60, 80, 120):
            texts = self.text(ascii_mode=True, width=width)
            out = "\n".join(texts)
            self.assertTrue(all(ord(c) < 128 for c in out), out)
        row = self.row("5h", self.text(ascii_mode=True))
        self.assertEqual(row.count("#"), 10)
        self.assertEqual(row.count("."), 10)

    def test_ascii_unknown_bar_is_an_empty_track(self):
        self.put("alpha")
        row = self.row("5h", self.text(ascii_mode=True))
        self.assertNotIn("#", row)
        self.assertIn("unknown", row)


class TestStripSizes(LimitsCase):
    def check_layout(self, width, height):
        texts = self.text(width=width, height=height)
        self.assertLessEqual(len(texts), height)
        self.assertTrue(all(len(t) == width for t in texts), [len(t) for t in texts])
        return texts

    def test_80x24_and_120x40_with_two_accounts(self):
        self.put("alpha", account="team-a", limits=both(40.0, 11.0))
        self.put("beta", account="team-b", limits=both(90.0, 77.0))
        for w, h in ((80, 24), (120, 40)):
            texts = self.check_layout(w, h)
            out = "\n".join(texts)
            self.assertEqual(out.count("LIMITS"), 2)
            self.assertRegex(out, r"resets \d\d:\d\d \(in 2h07m\)")  # full detail fits
            self.assertIn("2 of 2 hosts fresh", out)

    def test_narrow_60_columns_keeps_percentage_and_age(self):
        self.put("alpha", limits=both(42.0, 18.0))
        texts = self.check_layout(60, 16)
        for label in ("5h", "7d"):
            row = self.row(label, texts)
            self.assertRegex(row, r"\d+%")
            self.assertIn("as of 2m ago", row)
            self.assertIn("in ", row)  # shorter reset text, not dropped entirely
            self.assertGreaterEqual(len([c for c in row if c in "█░▍▏▎▌▋▊▉"]), view.LIMIT_BAR_MIN)

    def test_too_small_message_unchanged_below_60x16(self):
        self.put("alpha", limits=both())
        for w, h in ((59, 24), (80, 15), (50, 10)):
            texts = self.text(width=w, height=h)
            self.assertEqual(len(texts), 1)
            self.assertIn("terminal too small", texts[0])

    def test_minimum_size_with_two_accounts_still_has_hosts_total_and_footer(self):
        self.put("alpha", account="team-a", limits=both())
        self.put("beta", account="team-b", limits=both())
        texts = self.check_layout(60, 16)
        self.assertTrue(any(t.strip("│ ").startswith("total") for t in texts))
        self.assertTrue(any("alpha" in t for t in texts))
        self.assertTrue(any("2 of 2 hosts fresh" in t for t in texts))

    def test_many_accounts_collapse_to_a_notice_and_keep_the_hosts_table(self):
        for i in range(5):
            self.put("host%d" % i, account="acct%d" % i, limits=both())
        texts = self.check_layout(80, 16)
        out = "\n".join(texts)
        self.assertRegex(out, r"\d+ more accounts not shown")
        self.assertIn("LIMITS", out)
        self.assertTrue(any(t.strip("│ ").startswith("total") for t in texts))
        self.assertIn("5 of 5 hosts fresh", out)
        self.assertGreaterEqual(len([t for t in texts if "host" in t and "in+cache" not in t]),
                                view.MIN_HOST_ROWS)

    def test_a_huge_age_or_countdown_does_not_overflow_the_row(self):
        lim = make_limits(1, limit_window(42.0, hub.MAX_EPOCH))  # captured in 1970, resets in year 5138
        self.put("alpha", limits=lim)
        self.check_layout(60, 16)
        self.check_layout(80, 24)


if __name__ == "__main__":
    unittest.main()
