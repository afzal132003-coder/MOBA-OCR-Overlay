"""Following the running client (follow_running_client): the result
folder is whichever copy of the client is running, the one whose log is
being written when two are, and nothing moves when none is or the
operator has switched it off. Live, 2026-10-28 and -29: the engine read
one copy while the other was played, and the lobby, the table and Fetch
all came back empty or old.

Run: python ocr/freefire/test_follow_client.py
"""
import copy
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import freefire_engine as ff

checks, failures = 0, []


def check(label, ok, detail=""):
    global checks
    checks += 1
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", label, ("  " + detail) if detail else ""))
    if not ok:
        failures.append(label)


def client(root, name, log_age):
    """A client copy: its exe, its _Data folder, a debugger log written
    log_age seconds ago."""
    base = Path(root) / name
    data = base / "Free Fire_64_Data" / "Debugger"
    data.mkdir(parents=True)
    log = data / "debugger-2026-10-29T20-21-58.log"
    log.write_text("x")
    t = time.time() - log_age
    os.utime(log, (t, t))
    return str(base / "Free Fire_64.exe"), base / "Free Fire_64_Data"


def main():
    keep = copy.deepcopy(ff.server_state.get("settings"))
    save = ff.save_state
    ff.save_state = lambda *a, **k: None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            games_exe, games = client(tmp, "Games copy", 5)
            down_exe, down = client(tmp, "Downloads copy", 3600)
            st = ff.server_state.setdefault("settings", {})
            st.update(matchResultFolder=str(down), debuggerFolder="", followClient=None)

            print("\none client running, the setting on another copy")
            ff._follow_checked_at = 0
            moved = ff.follow_running_client(now=1000, exe_paths=[games_exe])
            check("the folder follows it", moved == games and st["matchResultFolder"] == str(games), st["matchResultFolder"])
            check("and not again while it stays put", ff.follow_running_client(now=1010, exe_paths=[games_exe]) is None)

            print("\nchecked only every few seconds")
            st["matchResultFolder"] = str(down)
            check("not inside the interval", ff.follow_running_client(now=1012, exe_paths=[games_exe]) is None
                  and st["matchResultFolder"] == str(down))
            check("after it", ff.follow_running_client(now=1020, exe_paths=[games_exe]) == games)

            print("\ntwo running")
            st["matchResultFolder"] = ""
            ff.follow_running_client(now=1030, exe_paths=[down_exe, games_exe])
            check("the one whose log is being written", st["matchResultFolder"] == str(games), st["matchResultFolder"])

            print("\nnone running")
            st["matchResultFolder"] = str(down)
            check("nothing changes", ff.follow_running_client(now=1040, exe_paths=[]) is None
                  and st["matchResultFolder"] == str(down))

            print("\nan old debugger folder set")
            st.update(matchResultFolder=str(down), debuggerFolder=str(down / "Debugger"))
            ff.follow_running_client(now=1050, exe_paths=[games_exe])
            check("is cleared with the move, so the log follows too", st["debuggerFolder"] == "", st["debuggerFolder"])

            print("\nswitched off")
            st.update(matchResultFolder=str(down), followClient=False)
            check("the operator's folder stays", ff.follow_running_client(now=1060, exe_paths=[games_exe]) is None
                  and st["matchResultFolder"] == str(down))
    finally:
        ff.save_state = save
        ff.server_state["settings"] = keep

    print("\n%d checks, %d failed" % (checks, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
