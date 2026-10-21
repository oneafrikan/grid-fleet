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
    grey      `unknown`, stale/never-reported rows (grey)
    dim       muted secondary lines such as in+cache tokens (dim)

With color=False (NO_COLOR, or a terminal without colour) render() emits ONLY
`plain` and `bold`, so "no colour attributes" is checkable on the render output.
Meaning never depends on colour: stale rows say `unknown` and `stale 41m`, never-
reported rows say `(never reported)`.

Not in this unit: the sparkline/leaderboard panel (group 6) and the limits strip
(group 7). No placeholders are drawn for them.
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
_COLOUR_FREE_TAGS = (PLAIN, BOLD)

Span = Tuple[str, str]
Line = List[Span]

MIN_WIDTH, MIN_HEIGHT = 60, 16  # design.md: below this, one-line message instead


class _Glyphs(object):
    def __init__(self, tl, tr, bl, br, h, v, lt, rt, sep, ell):
        self.tl, self.tr, self.bl, self.br = tl, tr, bl, br
        self.h, self.v, self.lt, self.rt = h, v, lt, rt
        self.sep, self.ell = sep, ell


UNICODE = _Glyphs("┌", "┐", "└", "┘", "─", "│", "├", "┤", "·", "…")
ASCII = _Glyphs("+", "+", "+", "+", "-", "|", "+", "+", "|", "~")

# Fixed column widths (characters). Counts are 7 wide so `unknown` fits in a cell.
W_COUNT, W_TOK = 7, 8
# busy + wait + out24 + out7 and the three single-space gaps between them.
W_REGION = W_COUNT + 1 + W_COUNT + 1 + W_TOK + 1 + W_TOK
W_AGE_MIN = 11  # room for "stale 2d04h"
AGE_GAP = "  "  # a little more air before the age column
# Lines taken by everything except host rows: top rule, HOSTS rule, column heads,
# total row, divider, footer, bottom rule.
FIXED_LINES = 7


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


def _row(name: Span, busy: Span, wait: Span, out24: Span, out7: Span, age: Span,
         hw: int, age_w: int) -> List[Span]:
    gap = (" ", PLAIN)
    return [
        (" ", PLAIN), (name[0].ljust(hw)[:hw], name[1]), gap,
        busy, gap, wait, gap, out24, gap, out7, (AGE_GAP, PLAIN),
        (age[0].ljust(age_w)[:age_w], age[1]),
    ]


def _host_lines(h: hub.HostState, hw: int, age_w: int, g: _Glyphs) -> List[List[Span]]:
    """One or two content rows for a host (second row is the muted in+cache line)."""
    name = (_clip(h.name, hw, g.ell), BOLD)
    if h.status == hub.NEVER:
        # No inbox file at all: say so in words, no age to show.
        region = ("unknown (never reported)".ljust(W_REGION), GREY)
        return [[(" ", PLAIN), (name[0].ljust(hw), BOLD), (" ", PLAIN), region]]
    if h.status == hub.STALE:
        region = ("unknown".ljust(W_REGION), GREY)
        age = ("stale " + fmt_age(h.age_s or 0), GREY)
        return [[(" ", PLAIN), (name[0].ljust(hw), BOLD), (" ", PLAIN), region, (AGE_GAP, PLAIN), age]]
    rows = [_row(
        name,
        _cell(h.busy, W_COUNT, tag_if_positive=BUSY),
        _cell(h.waiting, W_COUNT, tag_if_positive=WAITING),
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


def _total_line(state: hub.HubState, hw: int, age_w: int) -> List[Span]:
    return _row(
        ("total", BOLD),
        _cell(state.busy.value, W_COUNT, state.busy.complete, BUSY),
        _cell(state.waiting.value, W_COUNT, state.waiting.complete, WAITING),
        _cell(state.out_24h.value, W_TOK, state.out_24h.complete, as_tokens=True),
        _cell(state.out_7d.value, W_TOK, state.out_7d.complete, as_tokens=True),
        ("", PLAIN), hw, age_w,
    )


def _head_line(hw: int, age_w: int) -> List[Span]:
    gap = (" ", PLAIN)
    return [
        (" ", PLAIN), ("host".ljust(hw), BOLD), gap,
        (_right("busy", W_COUNT), BOLD), gap, (_right("wait", W_COUNT), BOLD), gap,
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
    hw = max(5, min(longest, 16, inner - 1 - 1 - W_REGION - len(AGE_GAP) - W_AGE_MIN))
    age_w = inner - 1 - hw - 1 - W_REGION - len(AGE_GAP)

    # Host blocks, truncated to the rows that fit; the total row and footer always stay.
    blocks = [_host_lines(h, hw, age_w, g) for h in state.hosts]
    avail = height - FIXED_LINES
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

    lines = [_rule(g, g.tl, g.tr, width, "grid-fleet", _stamp(state.now)),
             _rule(g, g.lt, g.rt, width, "HOSTS"),
             _boxed(_head_line(hw, age_w), inner, g)]
    lines += [_boxed(b, inner, g) for b in body]
    lines += [_boxed(_total_line(state, hw, age_w), inner, g),
              _rule(g, g.lt, g.rt, width),
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
        # curses.wrapper restores the terminal (endwin) even when run_loop raises.
        curses.wrapper(run_loop, config, curses)
    except KeyboardInterrupt:
        pass  # Ctrl-C is a normal way to quit
    return 0
