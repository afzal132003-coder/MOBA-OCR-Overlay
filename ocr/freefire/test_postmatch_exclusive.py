"""One post-match graphic at a time: the one just pushed stays up and the
rest come down -- including straight after an engine restart with
something left up, which used to keep the OLD graphic and pull the new
one straight back down.

Run: python ocr/freefire/test_postmatch_exclusive.py
"""
import copy
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import freefire_engine as ff

checks, failures = 0, []


def check(label, ok, detail=""):
    global checks
    checks += 1
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", label, ("  " + detail) if detail else ""))
    if not ok:
        failures.append(label)


def all_down():
    for name, read, write in ff.POSTMATCH_GRAPHICS:
        write(False)


def main():
    keep = {k: copy.deepcopy(ff.server_state.get(k)) for k in ("display", "booyahStats", "playerH2H")}
    try:
        print("after a restart with Team Graphs left up")
        all_down()
        ff._set_team_graph(True)
        ff._postmatch_up = ff._postmatch_visible()     # what the engine starts with
        ff.server_state.setdefault("booyahStats", {}).update(visible=True, shownAt=int(time.time() * 1000))
        ff.postmatch_one_at_a_time()
        check("Booyah stats, just pushed, stays up", ff.server_state["booyahStats"]["visible"] is True)
        check("Team Graphs comes down", not (ff._disp().get("teamGraph") or {}).get("visible"))

        print("the next push")
        ff._disp()["mvpVisible"] = True
        ff.postmatch_one_at_a_time()
        check("MVP up", ff._disp().get("mvpVisible") is True)
        check("Booyah stats down", ff.server_state["booyahStats"]["visible"] is False)

        print("two arriving together")
        all_down()
        ff._postmatch_up = set()
        ff._disp()["scoreboardVisible"] = True
        ff.server_state.setdefault("playerH2H", {}).update(visible=True, shownAt=int(time.time() * 1000))
        ff.postmatch_one_at_a_time()
        check("the one with a push time wins over list order",
              ff.server_state["playerH2H"]["visible"] is True and not ff._disp().get("scoreboardVisible"))

        print("pulling down")
        ff.server_state["playerH2H"]["visible"] = False
        ff.postmatch_one_at_a_time()
        check("nothing is put back up", ff._postmatch_visible() == set(), str(ff._postmatch_visible()))
    finally:
        for k, v in keep.items():
            ff.server_state[k] = v
        ff._postmatch_up = ff._postmatch_visible()

    print("\n%d checks, %d failed" % (checks, len(failures)))
    if failures:
        for f in failures:
            print("  FAILED:", f)
        sys.exit(1)


if __name__ == "__main__":
    main()
