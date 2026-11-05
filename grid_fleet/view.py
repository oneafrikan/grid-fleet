"""Curses dashboard (render function separate from curses I/O), and the `view` command.

Owner: Group 4 unit (grid-frontend-dev). Entry point used by cli.py:
view_main(config, args) -> int.

Two layers, kept apart so the interesting part is testable without a terminal:

  1. render(state, width, height, ...) -> lines      PURE, no curses, no I/O
  2. run_loop(stdscr, config, api)                   the only curses code

`curses` is imported lazily inside view_main, so importing this module (and starting
the CLI for other subcommands) stays cheap.

Attribute-tag scheme (the contract between the two layers)
----------------------------------------------------------
render() returns `List[Line]`; a Line is a list of Span = (text, tag). Concatenating
the span texts of a line gives exactly what is shown on that row. Tags:

    plain     default attributes
    bold      headings, host names (not a colour; allowed under NO_COLOR)
    busy      busy-session counts above zero (cyan)
    waiting   waiting-session counts above zero, "files skipped" (yellow)
    idle      idle-session counts above zero (dim, never cyan or yellow: an open session at
              the prompt is presence, not activity, so it must not look like busy or waiting)
    grey      `unknown`, stale/never-reported rows (grey)
    dim       muted secondary lines such as in+cache tokens (dim); limit bars whose
              capture is more than an hour old
    ok        limit bar below 60 % (green)
    warn      limit bar 60-84 % (yellow)
    crit      limit bar 85 % and above (red)

With color=False (NO_COLOR, or a terminal without colour) render() emits ONLY
`plain` and `bold`, so "no colour attributes" is checkable on the render output.
Meaning never depends on colour: stale rows say `unknown` and `stale 41m`, never-
reported rows say `(never reported)`; limit bars always print their percentage, the
reset time and `as of <age> ago`, and an unknown window says `unknown`.

The sparkline + subagent leaderboard panel (task 6.2) sits between the total row and the
footer, in whatever rows the hosts table leaves free (hosts have priority; see _panel_lines).
The limits strip (task 7.4, view half) sits between the title and the HOSTS rule: one block
of three rows (header, 5h, 7d) per account, fed by hub.HubState.limits.
"""

import os
import sys
import time
from typing import Dict, List, Mapping, Optional, Tuple

from grid_fleet import config as cfg
from grid_fleet import hub

# ---------------------------------------------------------------------------
# Tags, glyphs, sizes
# ---------------------------------------------------------------------------

PLAIN, BOLD, BUSY, WAITING, GREY, DIM = "plain", "bold", "busy", "waiting", "grey", "dim"
OK, WARN, CRIT = "ok", "warn", "crit"  # limit bar severity (design.md: green/yellow/red)
_COLOUR_FREE_TAGS = (PLAIN, BOLD)

Span = Tuple[str, str]
Line = List[Span]

MIN_WIDTH, MIN_HEIGHT = 60, 16  # design.md: below this, one-line message instead


class _Glyphs(object):
    def __init__(self, tl, tr, bl, br, h, v, lt, rt, sep, ell, bar_full, bar_empty, bar_part,
                 td, tu, spark):
        self.tl, self.tr, self.bl, self.br = tl, tr, bl, br
        self.h, self.v, self.lt, self.rt = h, v, lt, rt
        self.sep, self.ell = sep, ell
        # Limit bar: filled cell, empty cell, and 1/8..7/8 partial cells (none in ASCII).
        self.bar_full, self.bar_empty, self.bar_part = bar_full, bar_empty, bar_part
        # T junctions (rule above / below a vertical divider) and the 8 sparkline levels,
        # lowest first. Level 0 is reserved for a zero day; non-zero days use levels 1-7.
        self.td, self.tu, self.spark = td, tu, spark


UNICODE = _Glyphs("┌", "┐", "└", "┘", "─", "│", "├", "┤", "·", "…",
                  "█", "░", ("▏", "▎", "▍", "▌", "▋", "▊", "▉"),
                  "┬", "┴", "▁▂▃▄▅▆▇█")
ASCII = _Glyphs("+", "+", "+", "+", "-", "|", "+", "+", "|", "~", "#", ".", (),
                "+", "+", "_.,-=+*#")

# Fixed column widths (characters). Counts are 7 wide so `unknown` fits in a cell.
W_COUNT, W_TOK = 7, 8
# busy + wait + out24 + out7 and the three single-space gaps between them.
W_REGION = W_COUNT + 1 + W_COUNT + 1 + W_TOK + 1 + W_TOK
# The idle column sits after wait: one more count cell plus its gap. It is the only column
# that can be dropped (see render): at the minimum width there is no room for it.
W_IDLE_EXTRA = 1 + W_COUNT
HOST_NAME_MIN = 5  # narrowest host-name column (also the width of the `total` label)
W_AGE_MIN = 11  # room for "stale 2d04h"
AGE_GAP = "  "  # a little more air before the age column
# Lines taken by everything except host rows: top rule, HOSTS rule, column heads,
# total row, divider, footer, bottom rule.
FIXED_LINES = 7

# Limits strip geometry. Each account block is 3 rows (header, 5h, 7d).
LIMIT_BLOCK_LINES = 3
MIN_HOST_ROWS = 3  # the strip never squeezes the hosts table below this many body rows
LIMIT_BAR_MAX, LIMIT_BAR_MIN = 20, 8  # bar width range; below the min, tail text is dropped
W_PCT = 7  # percentage cell; wide enough for `unknown`
LIMIT_OLD_S = 3600  # a capture older than this renders dim (design.md "Limits freshness")
LIMIT_LABELS = (("5h", "five_hour"), ("7d", "seven_day"))

# Sparkline / leaderboard panel geometry (task 6.2).
PANEL_MIN_ROWS = 2  # header rule + one content row; below this the panel is not drawn
SPARK_ROWS = 2  # glyph row + date-label row
SIDE_BY_SIDE_MIN_WIDTH = 72  # narrower than this: leaderboard only, full width
SPARK_COL_MIN = 30  # inner width of the sparkline column
W_RANK, W_INV, W_OUT = 2, 6, 6  # rank, invocations and output-token cell widths
BOARD_BAR_MAX, BOARD_BAR_MIN = 16, 4  # proportional bar width range; below the min, no bar
BOARD_TYPE_MAX, BOARD_TYPE_MIN = 20, 8  # type column width range
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")  # fixed English, not locale


# ---------------------------------------------------------------------------
# Small formatters
# ---------------------------------------------------------------------------


def fmt_tokens(n: Optional[int]) -> str:
    """k / M / G with one decimal, dropping a trailing '.0' (400000 -> '400k', 3100000 -> '3.1M').

    Below 1000 the plain integer. Rounding that reaches 1000 of a unit moves up
    (999_950 -> '1M', not '1000k').
    """
    if n is None:
        return "unknown"
    if n < 1000:
        return str(n)
    units = (("k", 1e3), ("M", 1e6), ("G", 1e9))
    i = 0
    while i + 1 < len(units) and n >= units[i + 1][1]:
        i += 1
    while True:
        text = "%.1f" % (n / units[i][1])
        if float(text) >= 1000 and i + 1 < len(units):
            i += 1  # 999.95k rounds to 1000.0k: show 1M instead
            continue
        break
    if text.endswith(".0"):
        text = text[:-2]
    return text + units[i][0]


def fmt_age(seconds: int) -> str:
    """'<1m', '41m', '2h05m', '3d04h'. Coarse on purpose: this is a glance, not a log."""
    if seconds < 60:
        return "<1m"
    minutes = seconds // 60
    if minutes < 60:
        return "%dm" % minutes
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "%dh%02dm" % (hours, minutes)
    days, hours = divmod(hours, 24)
    return "%dd%02dh" % (days, hours)


def locale_is_utf8(env: Mapping[str, str]) -> bool:
    """POSIX precedence: LC_ALL, then LC_CTYPE, then LANG; the first non-empty one decides.

    No locale variable at all means the C locale, which is not UTF-8.
    """
    for var in ("LC_ALL", "LC_CTYPE", "LANG"):
        val = env.get(var)
        if val:
            low = val.lower().replace("-", "")
            return "utf8" in low
    return False


def use_ascii(config_ascii: bool, env: Mapping[str, str]) -> bool:
    return bool(config_ascii) or not locale_is_utf8(env)


def use_color(env: Mapping[str, str]) -> bool:
    """no-color.org: NO_COLOR present and non-empty disables colour."""
    return not env.get("NO_COLOR")


# ---------------------------------------------------------------------------
# Rendering (pure)
# ---------------------------------------------------------------------------


def _clip(text: str, width: int, ell: str) -> str:
    if len(text) <= width:
        return text
    if width <= len(ell):
        return text[:width]
    return text[: width - len(ell)] + ell


def _fit(spans: List[Span], width: int) -> List[Span]:
    """Clip or pad a row of spans to exactly `width` characters."""
    out = []  # type: List[Span]
    used = 0
    for text, tag in spans:
        if used >= width:
            break
        if used + len(text) > width:
            text = text[: width - used]
        out.append((text, tag))
        used += len(text)
    if used < width:
        out.append((" " * (width - used), PLAIN))
    return out


def _boxed(spans: List[Span], inner: int, g: _Glyphs) -> Line:
    return [(g.v, PLAIN)] + _fit(spans, inner) + [(g.v, PLAIN)]


def _rule(g: _Glyphs, left: str, right: str, width: int, label: str = "", rlabel: str = "") -> Line:
    """A horizontal rule `left h h label h h rlabel right`, exactly `width` wide."""
    lead = [(left + " ", PLAIN), (label, BOLD), (" ", PLAIN)] if label else [(left, PLAIN)]
    tail = [(" " + rlabel + " ", PLAIN), (right, PLAIN)] if rlabel else [(right, PLAIN)]
    used = sum(len(t) for t, _ in lead + tail)
    if used >= width:  # no room for the right-hand label: drop it rather than overflow
        tail = [(right, PLAIN)]
        used = sum(len(t) for t, _ in lead + tail)
    return _fit(lead + [(g.h * max(0, width - used), PLAIN)] + tail, width)


def _right(text: str, width: int) -> str:
    return text.rjust(width)[:width]


def _region_w(show_idle: bool) -> int:
    """Width of the numeric region (busy .. out 7d), with or without the idle column."""
    return W_REGION + (W_IDLE_EXTRA if show_idle else 0)


def _cell(value: Optional[int], width: int, complete: bool = True, tag_if_positive: str = PLAIN,
          as_tokens: bool = False) -> Span:
    """A right-aligned numeric cell; None renders as grey `unknown`, never 0.

    A trailing `+` marks a total that is a lower bound (some fresh host had a null section).
    """
    if value is None:
        return (_right("unknown", width), GREY)
    text = fmt_tokens(value) if as_tokens else str(value)
    if not complete:
        text += "+"
    return (_right(text, width), tag_if_positive if value > 0 else PLAIN)


def _row(name: Span, busy: Span, wait: Span, idle: Optional[Span], out24: Span, out7: Span,
         age: Span, hw: int, age_w: int) -> List[Span]:
    """One table row. `idle` is None when the idle column is dropped for width."""
    gap = (" ", PLAIN)
    cells = [busy, gap, wait, gap] + ([idle, gap] if idle is not None else [])
    return [
        (" ", PLAIN), (name[0].ljust(hw)[:hw], name[1]), gap,
    ] + cells + [
        out24, gap, out7, (AGE_GAP, PLAIN),
        (age[0].ljust(age_w)[:age_w], age[1]),
    ]


def _host_lines(h: hub.HostState, hw: int, age_w: int, g: _Glyphs,
                show_idle: bool = True) -> List[List[Span]]:
    """One or two content rows for a host (second row is the muted in+cache line)."""
    name = (_clip(h.name, hw, g.ell), BOLD)
    if h.status == hub.NEVER:
        # No inbox file at all: say so in words, no age to show.
        region = ("unknown (never reported)".ljust(_region_w(show_idle)), GREY)
        return [[(" ", PLAIN), (name[0].ljust(hw), BOLD), (" ", PLAIN), region]]
    if h.status == hub.STALE:
        region = ("unknown".ljust(_region_w(show_idle)), GREY)
        age = ("stale " + fmt_age(h.age_s or 0), GREY)
        return [[(" ", PLAIN), (name[0].ljust(hw), BOLD), (" ", PLAIN), region, (AGE_GAP, PLAIN), age]]
    rows = [_row(
        name,
        _cell(h.busy, W_COUNT, tag_if_positive=BUSY),
        _cell(h.waiting, W_COUNT, tag_if_positive=WAITING),
        _cell(h.idle, W_COUNT, tag_if_positive=DIM) if show_idle else None,
        _cell(h.out_24h, W_TOK, as_tokens=True),
        _cell(h.out_7d, W_TOK, as_tokens=True),
        (fmt_age(h.age_s or 0), PLAIN),
        hw, age_w,
    )]
    if h.cache_24h is not None or h.cache_7d is not None:
        # Muted secondary line: everything that is NOT output tokens.
        text = "  in+cache 24h %s %s 7d %s" % (
            fmt_tokens(h.cache_24h), g.sep, fmt_tokens(h.cache_7d))
        rows.append([(" ", PLAIN), (text, DIM)])
    return rows


def _total_line(state: hub.HubState, hw: int, age_w: int, show_idle: bool = True) -> List[Span]:
    return _row(
        ("total", BOLD),
        _cell(state.busy.value, W_COUNT, state.busy.complete, BUSY),
        _cell(state.waiting.value, W_COUNT, state.waiting.complete, WAITING),
        _cell(state.idle.value, W_COUNT, state.idle.complete, DIM) if show_idle else None,
        _cell(state.out_24h.value, W_TOK, state.out_24h.complete, as_tokens=True),
        _cell(state.out_7d.value, W_TOK, state.out_7d.complete, as_tokens=True),
        ("", PLAIN), hw, age_w,
    )


def _head_line(hw: int, age_w: int, show_idle: bool = True) -> List[Span]:
    gap = (" ", PLAIN)
    idle = [(_right("idle", W_COUNT), BOLD), gap] if show_idle else []
    return [
        (" ", PLAIN), ("host".ljust(hw), BOLD), gap,
        (_right("busy", W_COUNT), BOLD), gap, (_right("wait", W_COUNT), BOLD), gap,
    ] + idle + [
        (_right("out 24h", W_TOK), BOLD), gap, (_right("out 7d", W_TOK), BOLD), (AGE_GAP, PLAIN),
        ("age".ljust(age_w), BOLD),
    ]


def _footer_line(state: hub.HubState, inner: int, g: _Glyphs) -> List[Span]:
    left = "q quit   r refresh"
    right = [("%d of %d hosts fresh" % (state.fresh, state.host_count), PLAIN)]
    if state.skipped:
        word = "file" if state.skipped == 1 else "files"
        right += [(" %s " % g.sep, PLAIN), ("%d %s skipped" % (state.skipped, word), WAITING)]
    rlen = sum(len(t) for t, _ in right)
    avail = inner - 2  # one space of padding either side
    left = left[: max(0, avail - rlen - 1)]
    pad = max(1, avail - len(left) - rlen)
    return [(" ", PLAIN), (left, PLAIN), (" " * pad, PLAIN)] + right


def _limit_tag(pct: float) -> str:
    """design.md thresholds: green < 60 %, yellow 60-84 %, red >= 85 %."""
    if pct >= 85:
        return CRIT
    if pct >= 60:
        return WARN
    return OK


def _bar(pct: Optional[float], width: int, g: _Glyphs) -> str:
    """A bar exactly `width` cells wide. None (unknown) is an empty track."""
    if pct is None or width <= 0:
        return g.bar_empty * max(0, width)
    if g.bar_part:
        eighths = int(round(pct / 100.0 * width * 8))
        full, part = divmod(eighths, 8)
        text = g.bar_full * full + (g.bar_part[part - 1] if part else "")
    else:
        text = g.bar_full * int(round(pct / 100.0 * width))
    return text.ljust(width, g.bar_empty)[:width]


def _clock(resets_at: int, now: int) -> str:
    """Hub-local reset time: `16:10` within a day, `Mon 09:00` beyond. '' if unrepresentable."""
    try:
        t = time.localtime(resets_at)
    except (OverflowError, ValueError, OSError):
        return ""
    hhmm = "%02d:%02d" % (t.tm_hour, t.tm_min)
    return hhmm if resets_at - now < 86400 else "%s %s" % (_WEEKDAYS[t.tm_wday], hhmm)


def _limit_tails(w: hub.LimitWindow, now: int) -> List[str]:
    """Candidate tail texts for a window, most detailed first; the renderer takes the first
    that leaves room for a bar. Every known window always keeps `as of <age> ago`."""
    if w.pct is None:
        if w.expired:
            return ["window reset"]  # says why it is unknown; the stored % no longer applies
        return ["no data"]
    asof = "as of %s ago" % fmt_age(w.age_s or 0)
    left = fmt_age(max(0, (w.resets_at or 0) - now))
    out = []
    clock = _clock(w.resets_at or 0, now)
    if clock:
        out.append("resets %s (in %s)  %s" % (clock, left, asof))
    out.append("resets in %s  %s" % (left, asof))
    out.append(asof)
    return out


def _limit_row(label: str, w: hub.LimitWindow, now: int, inner: int, g: _Glyphs) -> List[Span]:
    prefix = " %s  " % label
    pct_text = "unknown" if w.pct is None else "%d%%" % int(round(w.pct))
    # 1 = gap between bar and pct, 2 = gap between pct and tail.
    room = inner - len(prefix) - 1 - W_PCT - 2
    tails = _limit_tails(w, now)
    tail = tails[-1]
    for cand in tails:  # first candidate that still leaves a usable bar
        if room - len(cand) >= LIMIT_BAR_MIN:
            tail = cand
            break
    bar_w = max(0, min(LIMIT_BAR_MAX, room - len(tail)))
    if w.pct is None:
        tag = GREY
    elif w.age_s is not None and w.age_s > LIMIT_OLD_S:
        tag = DIM  # old capture: dim whatever the severity; the age text still says how old
    else:
        tag = _limit_tag(w.pct)
    return [
        (prefix, BOLD),
        (_bar(w.pct, bar_w, g), tag), (" ", PLAIN),
        (pct_text.ljust(W_PCT), tag), ("  ", PLAIN),
        (tail, GREY if w.pct is None else PLAIN),
    ]


def _limit_lines(blocks, now: int, inner: int, g: _Glyphs, max_lines: int) -> List[List[Span]]:
    """The limits strip rows (unboxed). Blocks beyond the row budget collapse to one notice."""
    show = len(blocks)
    if show * LIMIT_BLOCK_LINES > max_lines:
        show = max(1, (max_lines - 1) // LIMIT_BLOCK_LINES)
    rows = []  # type: List[List[Span]]
    for b in blocks[:show]:
        head = [(" ", PLAIN), ("LIMITS", BOLD)]
        if b.account:
            head.append(("  account %s" % b.account, PLAIN))
        rows.append(head)
        for label, attr in LIMIT_LABELS:
            rows.append(_limit_row(label, getattr(b, attr), now, inner, g))
    if show < len(blocks):
        rows.append([(" ", PLAIN), ("%s %d more accounts not shown" % (g.ell, len(blocks) - show), DIM)])
    return rows


# ---------------------------------------------------------------------------
# Sparkline + subagent leaderboard panel (task 6.2)
# ---------------------------------------------------------------------------


def _compact(n: int, width: int) -> str:
    """fmt_tokens(n) right-aligned in a `width` cell. A value too wide for the cell (only
    possible with absurd input) shows as 9999+ rather than being cut to a wrong number."""
    text = fmt_tokens(n)
    if len(text) > width:
        text = "9" * (width - 1) + "+"
    return text.rjust(width)


def _spark_glyphs(values: List[int], g: _Glyphs) -> str:
    """One glyph per value, scaled to the largest value in the window.

    Integer maths only (a huge value must not go through float). A zero day is level 0 and
    any non-zero day is at least level 1, so "tiny" and "nothing" never look the same. All
    zero gives a flat line of level 0.
    """
    top = max(values) if values else 0
    steps = len(g.spark) - 2  # 6: non-zero days spread over levels 1..7
    out = []
    for v in values:
        out.append(g.spark[0] if v <= 0 or top <= 0 else g.spark[1 + (v * steps) // top])
    return " ".join(out)


def _spark_rows(by_day, lw: int, g: _Glyphs) -> List[List[Span]]:
    """The sparkline column: glyph row with the peak value in words, then first/last date.

    The `max` label means the chart is readable without colour or even without the glyphs.
    """
    if not by_day:
        return [[(" ", PLAIN), ("no data", DIM)]]
    values = [d.output for d in by_day]
    row1 = [(" ", PLAIN), (_spark_glyphs(values, g), PLAIN),
            ("  max %s" % fmt_tokens(max(values)), PLAIN)]
    first, last = by_day[0].day[5:], by_day[-1].day[5:]  # MM-DD (the year is noise at a glance)
    if len(by_day) == 1:
        label = first
    else:
        # first ........ last, as wide as the column allows (capped so it stays compact)
        dots = max(1, min(lw - 2, 24) - len(first) - len(last) - 2)
        label = "%s %s %s" % (first, "." * dots, last)
    return [row1, [(" ", PLAIN), (label, DIM)]]


def _board_rows(board, n: int, rw: int, g: _Glyphs) -> List[List[Span]]:
    """At most `n` leaderboard rows in a column `rw` wide: rank, type, [bar], invocations,
    [output tokens]. The bar is proportional to the top row; the dim output column and then
    the bar are dropped as the column narrows (the invocation number is never dropped).
    """
    if not board:
        return [[(" ", PLAIN), ("no subagent data", DIM)]]
    rows = board[:n]
    top = board[0].invocations  # sorted desc, so the first row is the scale
    tw = max(BOARD_TYPE_MIN, min(BOARD_TYPE_MAX, max(len(r.type) for r in rows)))
    # " rr type inv": lead space, rank, gap, type, gap, invocations.
    base = 1 + W_RANK + 1 + tw + 1 + W_INV
    if rw < base:  # pathological narrowness: shrink the type column, never below 4
        tw = max(4, tw - (base - rw))
        base = 1 + W_RANK + 1 + tw + 1 + W_INV
    extra = rw - base
    use_out = any(r.output is not None for r in rows) and extra - (1 + W_OUT) >= 1 + BOARD_BAR_MIN
    room = extra - ((1 + W_OUT) if use_out else 0) - 1  # -1: the gap before the bar
    bar_w = min(BOARD_BAR_MAX, room) if room >= BOARD_BAR_MIN else 0
    out = []
    for i, r in enumerate(rows):
        spans = [(" ", PLAIN), (_right(str(i + 1), W_RANK), PLAIN), (" ", PLAIN),
                 (_clip(r.type, tw, g.ell).ljust(tw), BOLD), (" ", PLAIN)]
        if bar_w:
            # At least one cell for any non-zero row, so a small type is still visible.
            filled = max(1, r.invocations * bar_w // top)
            spans += [((g.bar_full * filled).ljust(bar_w), PLAIN), (" ", PLAIN)]
        spans.append((_compact(r.invocations, W_INV), BOLD))
        if use_out:
            spans += [(" ", PLAIN),
                      (_compact(r.output, W_OUT) if r.output is not None else _right("-", W_OUT), DIM)]
        out.append(spans)
    return out


def _panel_lines(state: hub.HubState, width: int, inner: int, g: _Glyphs, rows: int):
    """The panel in `rows` free rows (header rule included): (lines, split_x or None).

    Priority, set by the caller: the hosts table first, the panel gets what is left. Then:
      - fewer than PANEL_MIN_ROWS rows: no panel;
      - wide enough and at least SPARK_ROWS content rows: sparkline left, leaderboard right,
        the leaderboard truncated to the rows available (split_x is the divider column, so
        the caller can draw the matching T junction in the rule below);
      - otherwise (narrow terminal, or a single content row): the sparkline is dropped and
        the leaderboard takes the full width, still truncated to fit.
    """
    if rows < PANEL_MIN_ROWS:
        return [], None
    content = rows - 1
    board = state.agents
    if width >= SIDE_BY_SIDE_MIN_WIDTH and content >= SPARK_ROWS:
        lw = max(SPARK_COL_MIN, (inner - 1) * 2 // 5)
        rw = inner - lw - 1
        n = min(content, max(SPARK_ROWS, len(board)))
        left = _spark_rows(state.by_day, lw, g)
        right = _board_rows(board, n, rw - 1, g)  # -1: keep a margin before the border
        head = (_rule(g, g.lt, g.td, lw + 2, "OUTPUT TOKENS / DAY (7d)")
                + _rule(g, g.h, g.rt, rw + 1, "SUBAGENTS (7d, invocations)"))
        body = []
        for i in range(n):
            body.append([(g.v, PLAIN)]
                        + _fit(left[i] if i < len(left) else [], lw)
                        + [(g.v, PLAIN)]
                        + _fit(right[i] if i < len(right) else [], rw)
                        + [(g.v, PLAIN)])
        return [head] + body, lw + 1
    n = min(content, max(1, len(board)))
    head = _rule(g, g.lt, g.rt, width, "SUBAGENTS (7d, invocations)")
    return [head] + [_boxed(r, inner, g) for r in _board_rows(board, n, inner - 1, g)], None


def _stamp(now: int) -> str:
    # Hub-local wall time; the format is fixed so output is stable apart from the zone.
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(now))


def render(state: hub.HubState, width: int, height: int, ascii_mode: bool = False,
           color: bool = True) -> List[Line]:
    """Turn a HubState into at most `height` lines of at most `width` characters.

    Below MIN_WIDTH x MIN_HEIGHT a single "terminal too small" line replaces the layout.
    Shows no cwd, session name or per-session list (privacy contract): only host names
    and aggregate figures.
    """
    g = ASCII if ascii_mode else UNICODE
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        msg = "terminal too small (need %dx%d)" % (MIN_WIDTH, MIN_HEIGHT)
        return [[(msg[: max(0, width)], PLAIN)]]

    inner = width - 2
    # 5 = len("total"), the widest fixed label in the first column.
    longest = max([len(h.name) for h in state.hosts] + [5])
    # Idle column: shown only if the host-name column can still be at its minimum and the
    # age column at its minimum beside it. Otherwise it is dropped (the 60x16 floor has no
    # room), rather than squeezing names or the stale age below readability.
    show_idle = (inner - 1 - 1 - _region_w(True) - len(AGE_GAP) - W_AGE_MIN) >= HOST_NAME_MIN
    region = _region_w(show_idle)
    hw = max(HOST_NAME_MIN, min(longest, 16, inner - 1 - 1 - region - len(AGE_GAP) - W_AGE_MIN))
    age_w = inner - 1 - hw - 1 - region - len(AGE_GAP)

    # Limits strip first: it takes its rows off the top, hosts get what is left (at least
    # MIN_HOST_ROWS, enforced by collapsing surplus accounts into a notice).
    limit_rows = _limit_lines(state.limits, state.now, inner, g,
                              max(LIMIT_BLOCK_LINES, height - FIXED_LINES - MIN_HOST_ROWS))

    # Host blocks, truncated to the rows that fit; the total row and footer always stay.
    blocks = [_host_lines(h, hw, age_w, g, show_idle) for h in state.hosts]
    avail = height - FIXED_LINES - len(limit_rows)
    body = []  # type: List[List[Span]]
    if not blocks:
        body.append([(" ", PLAIN), ("no snapshots in the inbox yet", DIM)])
    for i, block in enumerate(blocks):
        last = i == len(blocks) - 1
        need = len(block) + (0 if last else 1)  # keep a row free for the "more" notice
        if len(body) + need > avail:
            body.append([(" ", PLAIN), ("%s %d more hosts not shown" % (g.ell, len(blocks) - i), DIM)])
            break
        body.extend(block)

    lines = [_rule(g, g.tl, g.tr, width, "grid-fleet", _stamp(state.now))]
    lines += [_boxed(r, inner, g) for r in limit_rows]
    lines += [_rule(g, g.lt, g.rt, width, "HOSTS"),
              _boxed(_head_line(hw, age_w, show_idle), inner, g)]
    lines += [_boxed(b, inner, g) for b in body]
    lines += [_boxed(_total_line(state, hw, age_w, show_idle), inner, g)]

    # Whatever the hosts table left free goes to the sparkline/leaderboard panel. The hosts
    # come first: a truncated table ("N more hosts") means no panel at all.
    free = avail - len(body)
    panel, split = _panel_lines(state, width, inner, g, free if len(body) < avail else 0)
    lines += panel
    if split is None:
        divider = _rule(g, g.lt, g.rt, width)
    else:  # the vertical divider ends here: close it with a T junction
        divider = _fit(_rule(g, g.lt, g.tu, split + 1) + _rule(g, g.h, g.rt, width - split - 1), width)
    lines += [divider,
              _boxed(_footer_line(state, inner, g), inner, g),
              _rule(g, g.bl, g.br, width)]

    if not color:
        lines = [[(t, tag if tag in _COLOUR_FREE_TAGS else PLAIN) for t, tag in ln] for ln in lines]
    return lines


def plain_text(lines: List[Line]) -> List[str]:
    """The rows as plain strings (tags dropped). Handy for tests and a future --once."""
    return ["".join(t for t, _ in ln) for ln in lines]


# ---------------------------------------------------------------------------
# Curses layer (everything below touches the terminal)
# ---------------------------------------------------------------------------


def _build_attrs(api, color: bool) -> Dict[str, int]:
    """Map render tags to curses attributes. Only called once curses is initialised."""
    attrs = {PLAIN: 0, BOLD: api.A_BOLD}
    if not color:
        return attrs
    api.start_color()
    bg = -1
    try:
        api.use_default_colors()  # keep the user's terminal background
    except api.error:
        bg = api.COLOR_BLACK

    def pair(n: int, fg: int) -> int:
        api.init_pair(n, fg, bg)
        return api.color_pair(n)

    attrs[BUSY] = pair(1, api.COLOR_CYAN)
    attrs[WAITING] = pair(2, api.COLOR_YELLOW)
    # Bright black (8) is the usual "grey" on 16+ colour terminals; otherwise dim.
    attrs[GREY] = pair(3, 8) if getattr(api, "COLORS", 0) >= 16 else api.A_DIM
    attrs[DIM] = api.A_DIM
    attrs[OK] = pair(4, api.COLOR_GREEN)
    attrs[WARN] = pair(5, api.COLOR_YELLOW)
    attrs[CRIT] = pair(6, api.COLOR_RED)
    return attrs


class Dashboard(object):
    """Holds the cached inbox scan and re-reads it only when the inbox signature changes.

    Read-only: it only calls hub.inbox_signature / hub.scan_inbox.
    """

    def __init__(self, state_dir: str, expected_hosts):
        self.state_dir = state_dir
        self.expected_hosts = tuple(expected_hosts)
        self._sig = None  # type: Optional[tuple]
        self._scan = hub.InboxScan()

    def poll(self, force: bool = False) -> None:
        # Take the signature BEFORE scanning: a file replaced mid-scan then differs from
        # the stored signature and is picked up on the next tick.
        sig = hub.inbox_signature(self.state_dir)
        if force or sig != self._sig:
            self._scan = hub.scan_inbox(self.state_dir)
            self._sig = sig

    def state(self, now: int) -> hub.HubState:
        # Staleness depends on the clock, so it is recomputed every tick from the cached scan.
        return hub.build_state(self._scan, now, self.expected_hosts)


def _draw(stdscr, lines: List[Line], attrs: Dict[str, int], api) -> None:
    stdscr.erase()
    for y, line in enumerate(lines):
        x = 0
        for text, tag in line:
            try:
                stdscr.addstr(y, x, text, attrs.get(tag, 0))
            except api.error:
                pass  # writing the bottom-right cell raises after drawing; harmless
            x += len(text)
    stdscr.refresh()


def run_loop(stdscr, config, api, env: Optional[Mapping[str, str]] = None) -> None:
    """The UI loop. `api` is the curses module (or a fake in tests).

    Per tick: poll the inbox (stat only; re-read only on change), redraw, wait for a key
    for up to refresh_s. Keys: q quit, r re-read now. KEY_RESIZE and timeouts just redraw.
    Ctrl-C raises KeyboardInterrupt; view_main turns that into a normal exit.
    The cursor is restored in `finally`, so it comes back on exceptions too.
    """
    env = os.environ if env is None else env
    ascii_mode = use_ascii(config.ascii, env)
    color = use_color(env) and api.has_colors()
    attrs = _build_attrs(api, color)
    try:
        api.curs_set(0)
    except api.error:
        pass  # some terminals cannot hide the cursor
    stdscr.timeout(config.refresh_s * 1000)  # getch returns -1 on timeout = our poll tick
    dash = Dashboard(config.state_dir, config.expected_hosts)
    force = True
    try:
        while True:
            dash.poll(force)
            force = False
            height, width = stdscr.getmaxyx()
            lines = render(dash.state(cfg.now()), width, height, ascii_mode, color)
            _draw(stdscr, lines, attrs, api)
            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                return
            if key in (ord("r"), ord("R")):
                force = True
            elif key == api.KEY_RESIZE and hasattr(api, "update_lines_cols"):
                api.update_lines_cols()
    finally:
        try:
            api.curs_set(1)
        except api.error:
            pass


def _wrapper_without_color(api, func, *args):
    """curses.wrapper minus start_color(), for NO_COLOR.

    curses.wrapper calls start_color() before our code can decide there is no colour, and
    start_color() makes the terminal emit colour-initialisation codes. Setup and restore
    otherwise mirror the stdlib wrapper exactly, including restoring the terminal
    (keypad, echo, cbreak, endwin) when `func` raises.
    """
    stdscr = api.initscr()
    try:
        api.noecho()
        api.cbreak()
        stdscr.keypad(1)
        return func(stdscr, *args)
    finally:
        stdscr.keypad(0)
        api.echo()
        api.nocbreak()
        api.endwin()


def view_main(config, args) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.stderr.write("grid-fleet view: needs an interactive terminal\n")
        return 1
    import curses
    import locale

    try:
        # Needed for curses to draw UTF-8 glyphs correctly.
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    try:
        # Both routes restore the terminal (endwin) even when run_loop raises. With
        # NO_COLOR the stdlib wrapper is avoided because it would call start_color().
        if use_color(os.environ):
            curses.wrapper(run_loop, config, curses)
        else:
            _wrapper_without_color(curses, run_loop, config, curses)
    except KeyboardInterrupt:
        pass  # Ctrl-C is a normal way to quit
    return 0
