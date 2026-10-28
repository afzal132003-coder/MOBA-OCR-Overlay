"""Text logos (settings.textLogos): an event with no logos, every team
without one shows its short name -- its name when it has none -- as its
logo. Only empty logos are filled, a real one always stays, and switched
off only what was drawn is taken back, even after a dashboard save has
carried it round as an ordinary logo.

Run: python ocr/freefire/test_text_logos.py
"""
import copy
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


REAL = "data:image/png;base64,REALLOGO"


def main():
    keep = {k: copy.deepcopy(ff.server_state.get(k)) for k in ("roster", "settings", "textLogoHashes")}
    try:
        ff.server_state["roster"] = {"teams": [
            {"name": "ATINGLE NOVAS", "shortName": "AN", "logo": ""},
            {"name": "X BEES", "shortName": "XB", "logo": REAL},
            {"name": "NO TAG TEAM", "shortName": "", "logo": ""},
        ]}
        ff.server_state["textLogoHashes"] = []
        teams = ff.server_state["roster"]["teams"]

        print("\nswitched on")
        ff.server_state["settings"] = dict(keep["settings"] or {}, textLogos=True)
        changed = ff.apply_text_logos()
        check("an empty logo gets its short name", changed and teams[0]["logo"] == ff.make_text_logo("AN"))
        check("a real logo stays", teams[1]["logo"] == REAL)
        check("no short name: its name", teams[2]["logo"] == ff.make_text_logo("NO TAG TEAM"))
        check("a second pass changes nothing", ff.apply_text_logos() is False)
        teams[0]["shortName"] = "ANV"
        ff.apply_text_logos()
        check("a new tag, a new logo", teams[0]["logo"] == ff.make_text_logo("ANV"))

        print("\nafter a dashboard Save Roster")
        saved = copy.deepcopy(ff.server_state["roster"])
        ff.server_state["roster"] = saved                      # the logos came back as plain data
        teams = saved["teams"]
        upd = ff.without_engine_owned({"textLogoHashes": [], "roster": saved})
        check("a dashboard save cannot wipe what was drawn", "textLogoHashes" not in upd)
        check("the pages are not sent it", "textLogoHashes" not in ff.lean_state(ff.server_state))

        print("\nswitched off")
        ff.server_state["settings"]["textLogos"] = False
        changed = ff.apply_text_logos()
        check("the drawn logos go", changed and teams[0]["logo"] == "" and teams[2]["logo"] == "")
        check("the real one stays", teams[1]["logo"] == REAL)
        teams[0]["logo"] = REAL
        ff.server_state["settings"]["textLogos"] = True
        ff.apply_text_logos()
        check("on again, a logo added meanwhile is left alone", teams[0]["logo"] == REAL)
    finally:
        for k, v in keep.items():
            if v is None:
                ff.server_state.pop(k, None)
            else:
                ff.server_state[k] = v

    print("\n%d checks, %d failed" % (checks, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
