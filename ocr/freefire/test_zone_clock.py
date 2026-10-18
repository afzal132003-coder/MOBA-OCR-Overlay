"""The zone timer bug's clock (zone_clock): up only while the zone counts --
about to shrink, shrinking, about to move, moving -- with the seconds left
on the engine's estimate, and down while the circle waits or the match is
over.

Run: python ocr/freefire/test_zone_clock.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import freefire_engine as ff

checks, failures = 0, []


def check(label, ok, detail=""):
    global checks
    checks += 1
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", label, ("  " + detail) if detail else ""))
    if not ok:
        failures.append(label)


def live(stage, phase, started, **extra):
    return dict({"zone": {"stage": stage, "phase": phase}, "zonePhaseAt": started}, **extra)


def main():
    keep = ff.server_state.get("zoneTimings")
    # timings of its own: the saved ones are whatever matches ran last
    ff.server_state["zoneTimings"] = {"2:PRE_SHRINK": [40, 50, 50], "2:SHRINK": [90, 110, 90]}
    try:
        print("\nwhen it is up")
        c = ff.zone_clock(live(2, "PRE_SHRINK", 1000), now=1012)
        check("about to shrink: up, counting the median down",
              c == {"stage": 2, "phase": "PRE_SHRINK", "label": "ZONE SHRINKS IN", "left": 38, "expected": 50}, str(c))
        c = ff.zone_clock(live(2, "SHRINK", 1000), now=1030)
        check("shrinking: the time until it stops", c["label"] == "ZONE SHRINKING" and c["left"] == 60, str(c))
        c = ff.zone_clock(live(2, "PRE_SHRINK", 1000), now=1200)
        check("an overrun reads 0, never negative", c["left"] == 0, str(c))
        c = ff.zone_clock(live(5, "RANDOM_MOVE", 1000), now=1010)
        check("moving, with no timing seen or known: up, saying so, without a time",
              c["label"] == "ZONE MOVING" and c["left"] is None and c["expected"] is None, str(c))

        print("\nwhen it is down")
        check("the circle waiting", ff.zone_clock(live(2, "STABLE", 1000), now=1010) is None)
        check("no zone yet", ff.zone_clock({}, now=1010) is None)
        check("the match over", ff.zone_clock(live(2, "SHRINK", 1000, ended=True), now=1010) is None)
    finally:
        if keep is None:
            ff.server_state.pop("zoneTimings", None)
        else:
            ff.server_state["zoneTimings"] = keep

    print("\n%d checks, %d failed" % (checks, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
