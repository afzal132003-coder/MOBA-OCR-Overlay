"""What a Free Fire replay JSON gives us, measured on a real match.

The fixture is a real ReplayInfo_*.json with the highlight-reel lists
emptied -- the parser does not read them, and they were five sixths of
the file. Everything the parser does use is untouched.

Run: python ocr/freefire/test_replay_json.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import replay_json as rj

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "replay_sample.json")

failures = []
checks = 0


def check(name, ok, detail=""):
    global checks
    checks += 1
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  " + detail) if detail else ""))
    if not ok:
        failures.append(name)


def main():
    data = rj.load(FIXTURE)

    print("\nthe file itself")
    check("reads despite the BOM the client writes", isinstance(data, dict))
    check("carries match metadata",
          bool(data.get("MatchID")) and data.get("PlayerCount") == 48,
          "map %s, %s players, %.0fs" % (data.get("MapID"), data.get("PlayerCount"),
                                         data.get("GameTotalTime") or 0))

    print("\nnames, derived from the kill pairs")
    names = rj.player_names(data)
    check("every player named", len(names) == data["PlayerCount"],
          "%d of %d" % (len(names), data["PlayerCount"]))
    check("names are non-empty", all(n.strip() for n in names.values()))

    # The reason this is derived rather than read: SParam names the OTHER
    # party, so collecting it per PlayerID directly gives one player
    # several contradictory names. That naive read is what this guards.
    naive = {}
    clashes = 0
    for e in data["Events"]:
        if isinstance(e.get("SParam"), str) and e["SParam"].strip():
            prev = naive.setdefault(e["PlayerID"], e["SParam"])
            if prev != e["SParam"]:
                clashes += 1
    check("the naive read really does contradict itself", clashes > 0,
          "%d conflicting entries -- which is why the pairs are crossed over" % clashes)

    print("\nsquads, from the player id")
    squads = rj.squads(data, names)
    check("twelve squads", len(squads) == 12, "got %d" % len(squads))
    check("four players in each", all(len(v) == 4 for v in squads.values()),
          "sizes %s" % sorted({len(v) for v in squads.values()}))
    check("squad numbers are 1..12", sorted(squads) == list(range(1, 13)))

    print("\nthe kill feed")
    feed = rj.kills(data, names)
    check("kills found", len(feed) > 50, "%d kills" % len(feed))
    check("every kill names both sides",
          all(k["killer"] and k["victim"] for k in feed))
    check("nobody kills themselves",
          all(k["killerId"] != k["victimId"] for k in feed))
    check("kills run in time order",
          all(a["time"] <= b["time"] for a, b in zip(feed, feed[1:])))

    print("\npositions")
    placed = [k for k in feed if k["x"] is not None]
    check("most kills carry a position", len(placed) >= len(feed) * 0.8,
          "%d of %d (%.0f%%)" % (len(placed), len(feed), 100.0 * len(placed) / len(feed)))

    # The gap between a kill and its death event is a CONSTANT three
    # seconds, not noise -- the client removes the body on a timer. The
    # join window was first set to 2.0s on the assumption the timestamps
    # would nearly agree, and joined precisely nothing.
    check("the join window straddles that three-second gap",
          rj.DEATH_JOIN_SECONDS > 3.0,
          "window is %.1fs" % rj.DEATH_JOIN_SECONDS)

    bounds = rj._bounds(placed)
    span_x = bounds["maxX"] - bounds["minX"]
    span_z = bounds["maxZ"] - bounds["minZ"]
    check("positions span a plausible map", 200 < span_x < 3000 and 200 < span_z < 3000,
          "%.0f x %.0f world units" % (span_x, span_z))

    print("\nsummary(), the shape the dashboard will read")
    s = rj.summary(FIXTURE)
    for key in ("matchId", "map", "players", "squads", "kills", "killsBySquad", "bounds"):
        check("summary has %s" % key, key in s and s[key] is not None)
    check("kills attributed to every squad that got one",
          set(s["killsBySquad"]) <= set(range(1, 13)) and sum(s["killsBySquad"].values()) == len(feed))

    print("\nNOT IN THIS FILE, and no amount of parsing will add it:")
    print("  continuous player movement. The path a squad walks between")
    print("  fights is in the .bin, which this does not read. What is here")
    print("  is every kill, who, when, and where the victim fell.")

    print("\nsquad eliminations -- placement without the result file")
    el = rj.squad_eliminations(data)
    places = [e["place"] for e in el]
    check("every squad gets a place, each one once",
          sorted(places) == list(range(1, len(el) + 1)), str(sorted(places)))
    survivors = [e for e in el if e["time"] is None]
    check("exactly one squad is left standing", len(survivors) == 1,
          "%d" % len(survivors))
    check("and it takes first place", survivors[0]["place"] == 1)
    wipes = [e for e in el if e["time"] is not None]
    check("the wipes are in time order",
          all(a["time"] <= b["time"] for a, b in zip(wipes, wipes[1:])))
    check("first squad wiped finishes last",
          wipes[0]["place"] == len(el), "%d" % wipes[0]["place"])
    check("a wipe carries the team name the room used",
          all(w["teamName"] for w in wipes),
          str([w["teamName"] for w in wipes[:3]]))
    check("no squad is wiped twice",
          len({w["squad"] for w in wipes}) == len(wipes))

    print("\n%d checks, %d failed" % (checks, len(failures)))
    if failures:
        print("failed: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
