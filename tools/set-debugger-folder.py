"""Points the engine's debugger-log reader somewhere harmless.

The engine builds the alive table from up to three sources, and they do
not agree. The log reader is the problem one: when the engine starts into
a match already in progress it skips the log's history on purpose (so it
does not fire seventy old eliminations onto air), which leaves it
believing the match is a fresh lobby -- every squad alive, nobody with
any kills. It then publishes that picture on any poll where the alive
grid does not resolve, and the table flips between two truths several
times a minute.

Pointing it at a folder with no debugger-*.log files stops it publishing
a table at all. The alive grid becomes the only writer, and on a poll it
misses, the last good reading simply stands.

What is lost while it is set this way: the kill feed, automatic
match-end result fetching, and log-driven elimination signals. The alive
table, the tick, the elimination card and the sheet push all come from
the grid and are unaffected.

Undo by clearing the Debugger folder box in the dashboard, or running
this with no argument.

Run with the engine CLOSED -- it holds settings in memory and would
write its own copy back over this on the way out.
"""

import io
import json
import sys
from pathlib import Path

STATE = Path(__file__).resolve().parent.parent / "ocr" / "freefire" / "freefire_state.json"


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else ""

    if folder:
        p = Path(folder)
        p.mkdir(parents=True, exist_ok=True)
        stray = list(p.glob("debugger-*.log"))
        if stray:
            print(f"  {p} already has {len(stray)} debugger log(s) in it -- "
                  f"the reader would just use those. Pick an empty folder.")
            return 1

    try:
        state = json.loads(io.open(STATE, encoding="utf-8").read())
    except OSError as e:
        print(f"  couldn't read {STATE}: {e}")
        return 1

    settings = state.setdefault("settings", {})
    before = settings.get("debuggerFolder", "")
    settings["debuggerFolder"] = folder

    io.open(STATE, "w", encoding="utf-8", newline="").write(
        json.dumps(state, indent=2, ensure_ascii=False))

    print(f"        was: {before!r}")
    print(f"        now: {folder!r}")
    if folder:
        print("        the log reader will find nothing there, which is the point.")
    else:
        print("        cleared -- the log reader is back on.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
