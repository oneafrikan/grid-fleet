#!/bin/sh
# try-local.sh - one-command local try-out of grid-fleet.
#
# Usage: scripts/try-local.sh [--no-view]
#
# Intent: run collect -> collect (warm) -> push (hub "local", no ssh) -> inbox
# listing -> view, entirely inside a throwaway directory, so trying the tool
# leaves nothing behind. The only thing read from outside the temp dir is the
# Claude dir (the real ~/.claude, read-only) unless the caller exported
# GRID_FLEET_CLAUDE_DIR / GRID_FLEET_CLAUDE_BIN (tests point those at synthetic
# data). Nothing is written outside the temp dir and ssh is never run.
#
# Plain POSIX sh: no bash-isms (macOS /bin/sh and Linux dash both work).

NO_VIEW=0
case "${1:-}" in
  "") ;;
  --no-view) NO_VIEW=1 ;;
  *) echo "usage: scripts/try-local.sh [--no-view]" >&2; exit 2 ;;
esac

# --- 1. repo root from this script's own location --------------------------
# Follow symlinks by hand (readlink -f is not portable to older macOS), so the
# script works when invoked through a symlink and from any cwd.
src="$0"
while [ -L "$src" ]; do
  link="$(readlink "$src")"
  case "$link" in
    /*) src="$link" ;;
    *)  src="$(dirname "$src")/$link" ;;
  esac
done
root="$(cd "$(dirname "$src")/.." && pwd -P)" || { echo "try-local: cannot resolve repo root" >&2; exit 1; }
cli="$root/bin/grid-fleet"
[ -f "$cli" ] || { echo "try-local: $cli not found" >&2; exit 1; }

# --- 2. pick the interpreter ------------------------------------------------
# bin/grid-fleet's shebang uses whatever python3 is first on PATH, but stock
# macOS /usr/bin/python3 (3.9) is the supported floor, so prefer it when it is
# >= 3.9 and run the CLI through the chosen interpreter explicitly.
py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; }
if [ -x /usr/bin/python3 ] && py_ok /usr/bin/python3; then
  py=/usr/bin/python3
elif command -v python3 >/dev/null 2>&1 && py_ok python3; then
  py=python3
else
  echo "try-local: need python3 >= 3.9" >&2; exit 1
fi
echo "python: $py ($("$py" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])'))"

# --- 3. throwaway state + config -------------------------------------------
tmp=""
# Always remove the temp dir, however we exit. INT/TERM route through `exit`
# so the EXIT trap does the one cleanup.
cleanup() {
  if [ -n "$tmp" ] && [ -d "$tmp" ]; then
    rm -rf -- "$tmp"
    echo "temp dir removed: $tmp"
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

tmp="$(mktemp -d "$(printf %s "${TMPDIR:-/tmp}" | sed 's|/*$||')/grid-fleet-try.XXXXXX")" || { echo "try-local: mktemp failed" >&2; exit 1; }
export GRID_FLEET_STATE_DIR="$tmp"
export GRID_FLEET_CONFIG="$tmp/config.json"
# GRID_FLEET_CLAUDE_DIR / GRID_FLEET_CLAUDE_BIN are deliberately left as the
# caller has them (unset = real ~/.claude, read-only).
printf '%s\n' '{"schema":1,"host_name":"try-local","hub":"local"}' > "$GRID_FLEET_CONFIG" \
  || { echo "try-local: cannot write config" >&2; exit 1; }
echo "temp dir: $tmp"

# --- 4. collect x2, push, inbox listing -------------------------------------
# Millisecond clock via the chosen interpreter: `date +%N` is not portable to
# macOS, and `time` output formats differ between shells.
ms() { "$py" -c 'import time; print(int(time.time() * 1000))'; }

# timed_collect LABEL: run collect, print wall time and the summary line(s).
timed_collect() {
  t0="$(ms)"
  out="$("$py" "$cli" collect 2>&1)"; rc=$?
  t1="$(ms)"
  d=$((t1 - t0))
  printf '%s: %d.%03d s\n' "$1" $((d / 1000)) $((d % 1000))
  [ -n "$out" ] && printf '  %s\n' "$out"
  if [ "$rc" -ne 0 ]; then
    echo "try-local: collect failed (exit $rc) on $1" >&2
    exit 1
  fi
}
timed_collect "first run"
timed_collect "second run (warm)"

echo "push (hub: local):"
out="$("$py" "$cli" push 2>&1)"; rc=$?
[ -n "$out" ] && printf '  %s\n' "$out"
if [ "$rc" -ne 0 ]; then
  echo "try-local: push failed (exit $rc)" >&2
  exit 1
fi

# Names and sizes only, never contents.
echo "inbox:"
for f in "$tmp"/inbox/*; do
  [ -f "$f" ] || continue
  printf '  %s  %s bytes\n' "$(basename "$f")" "$(wc -c < "$f" | tr -d ' ')"
done

# --- 5. view ----------------------------------------------------------------
if [ "$NO_VIEW" -eq 1 ]; then
  echo "skipping view: --no-view"
elif [ ! -t 0 ] || [ ! -t 1 ]; then
  echo "skipping view: no tty"
else
  echo "view: press q to quit"
  "$py" "$cli" view
fi
# Exit status is that of the last command; cleanup runs via the EXIT trap.
