"""
Watches the Free Fire OB client's Replays/ folder for ReplayInfo_*.json
files and prints their Events/ReplayEvents content whenever a file
appears or changes -- a one-off investigation tool to see whether a
REAL match (actual players, actual kills) populates those arrays with
usable data, not a permanent part of the production pipeline.

Read-only: this only opens files the game itself already wrote to
disk, on a timer. It does not touch the running game, its memory, or
any other files.

Usage: python watch_replay_events.py "D:\\path\\to\\...\\Free Fire_64_Data\\Replays"
"""

import json
import sys
import time
from pathlib import Path

POLL_SECONDS = 2


def load_json_lenient(path):
    try:
        # utf-8-sig strips the BOM these files are written with (confirmed
        # via the earlier manual read -- plain utf-8 would leave a stray
        # \ufeff character at the start of the parsed string).
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # File may be mid-write (game still has it open) -- just skip
        # this poll cycle and try again next time, not a real error.
        return None


def summarize(data):
    events = data.get("Events", [])
    replay_events = data.get("ReplayEvents", [])
    print(f"  MatchID: {data.get('MatchID')}")
    print(f"  PlayerCount: {data.get('PlayerCount')}  GameTotalTime: {data.get('GameTotalTime')}")
    print(f"  MapID: {data.get('MapID')}  MatchMode: {data.get('MatchMode')}  GameMode: {data.get('GameMode')}")
    print(f"  Events: {len(events)} entries")
    for e in events[:10]:
        print(f"    {e}")
    if len(events) > 10:
        print(f"    ... and {len(events) - 10} more")
    print(f"  ReplayEvents: {len(replay_events)} entries")
    for e in replay_events[:10]:
        print(f"    {e}")
    if len(replay_events) > 10:
        print(f"    ... and {len(replay_events) - 10} more")
    highlights = data.get("PlayerHighlightInfos", [])
    if highlights:
        print(f"  PlayerHighlightInfos: {highlights}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python watch_replay_events.py <path to Replays folder>")
        sys.exit(1)

    folder = Path(sys.argv[1])
    if not folder.is_dir():
        print(f"Not a directory: {folder}")
        sys.exit(1)

    # Force line-buffered output -- otherwise Python holds print() output
    # in a buffer indefinitely whenever stdout isn't an interactive
    # terminal (e.g. piped to a log file), which would mean nothing
    # appears until the process exits -- no good for a long-running
    # watcher meant to show updates as they happen.
    sys.stdout.reconfigure(line_buffering=True)

    print(f"Watching {folder} for ReplayInfo_*.json changes (Ctrl+C to stop)...")
    last_seen_mtime = {}

    while True:
        for path in sorted(folder.glob("ReplayInfo_*.json")):
            mtime = path.stat().st_mtime
            if last_seen_mtime.get(path.name) == mtime:
                continue
            last_seen_mtime[path.name] = mtime
            data = load_json_lenient(path)
            if data is None:
                continue
            print(f"\n[{time.strftime('%H:%M:%S')}] {path.name} (updated)")
            summarize(data)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
