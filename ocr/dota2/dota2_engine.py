"""
Dota 2 post-match scoreboard reader.

Lives in ocr/dota2/, its own folder with its own config/state -- a
different game from moba/ (Mobile Legends, despite the shared "MOBA"
project name), valorant/ and freefire/, each isolated the same way for
the same reason: nothing here should be able to step on another game's
calibration or state.

SCOPE, deliberately narrow for now: the post-match Overview screen only
(kills/deaths/assists, gold, team score, game duration). No live in-game
capture, no IGN OCR -- player identity is assigned by the operator
afterwards, same "pick who this is" pattern Free Fire's pre-match roster
already uses, because an in-game name carries clan tags and symbols
("Verlin Raed [ZGØ]") that OCR reads unreliably and a human reads
correctly at a glance. This file only answers "what did row N show",
never "whose row N is".

The digit pipeline (preprocess/ocr_number/KDA regex) is carried over
verbatim from valorant_engine.py, not reinvented: that file's own history
records a specific, confirmed-against-real-captures fix for loop-shaped
digits (0/8 collapsing under naive thresholding) -- CUBIC upscale of the
grayscale crop BEFORE OTSU, border added AFTER. Same shapes of digit,
same fix applies here.
"""

import asyncio
import base64
import json
import re
from pathlib import Path

import cv2
import mss
import numpy as np
import pytesseract

CONFIG_PATH = Path(__file__).parent / "dota2_config.json"

TEAM_SLOTS = 5
CROP_PREVIEW_MAX_DIMENSION = 500


def crop_to_data_url(img_bgr, scale=3):
    """Same technique as valorant_engine.py's own crop_to_data_url() --
    upscale with NEAREST (crisp blocky pixels, so the operator sees the
    actual captured pixels, not a smoothed guess) and encode as a PNG data
    URL, so a captured row can show what OCR actually saw next to the
    number it read, letting the operator judge a misread at a glance
    instead of guessing blind."""
    h, w = img_bgr.shape[:2]
    longest = max(h, w)
    effective_scale = min(scale, CROP_PREVIEW_MAX_DIMENSION / longest)
    big = cv2.resize(img_bgr, None, fx=effective_scale, fy=effective_scale,
                      interpolation=cv2.INTER_AREA if effective_scale < 1 else cv2.INTER_NEAREST)
    ok, buf = cv2.imencode(".png", big)
    if not ok:
        return None
    b64 = base64.b64encode(buf).decode("ascii")
    return f"data:image/png;base64,{b64}"


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    tpath = cfg.get("tesseract_path")
    if tpath:
        pytesseract.pytesseract.tesseract_cmd = tpath
    return cfg


def crop_to_bgr(sct, region):
    shot = sct.grab({
        "left": region["x"], "top": region["y"],
        "width": region["w"], "height": region["h"],
    })
    return cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)


# ---------------------------------------------------------------------------
# Digit OCR -- carried over from valorant_engine.py's preprocess(), see the
# file-level note above for why this exact pipeline and not a simpler one.
# ---------------------------------------------------------------------------

def preprocess(img_bgr, upscale=4):
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.medianBlur(gray, 3)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if thresh.mean() < 127:
        thresh = cv2.bitwise_not(thresh)
    thresh = cv2.copyMakeBorder(thresh, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)
    return thresh


TESS_CONFIG_NUMBER = (
    "--oem 1 --psm 7 "
    "-c tessedit_char_whitelist=0123456789 "
    "-c load_system_dawg=0 -c load_freq_dawg=0"
)
TESS_CONFIG_DURATION = (
    "--oem 1 --psm 7 "
    "-c tessedit_char_whitelist=0123456789: "
    "-c load_system_dawg=0 -c load_freq_dawg=0"
)
TESS_CONFIG_KDA = (
    "--oem 1 --psm 7 "
    "-c tessedit_char_whitelist=0123456789/ "
    "-c load_system_dawg=0 -c load_freq_dawg=0"
)
# Net worth is comma-grouped ("6,260"), so its whitelist needs the comma
# the plain digit-only NUMBER config above doesn't carry.
TESS_CONFIG_NETWORTH = (
    "--oem 1 --psm 7 "
    "-c tessedit_char_whitelist=0123456789, "
    "-c load_system_dawg=0 -c load_freq_dawg=0"
)

KDA_TRIPLE_REGEX = re.compile(r"(\d+)\s*/\s*(\d+)\s*/\s*(\d+)")
DURATION_REGEX = re.compile(r"(\d{1,3}):(\d{2})")


def ocr_number(img_bgr):
    processed = preprocess(img_bgr)
    text = pytesseract.image_to_string(processed, config=TESS_CONFIG_NUMBER).strip()
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else None


def ocr_duration(img_bgr):
    processed = preprocess(img_bgr)
    text = pytesseract.image_to_string(processed, config=TESS_CONFIG_DURATION).strip()
    m = DURATION_REGEX.search(text)
    if not m:
        return None
    minutes, seconds = int(m.group(1)), int(m.group(2))
    return {"minutes": minutes, "seconds": seconds, "totalSeconds": minutes * 60 + seconds,
            "raw": text}


def ocr_kda(img_bgr):
    """One player's K/D/A cell -> (kills, deaths, assists), or None.
    Calibrated as its own box now -- see calibrate.py's "postgame-net-kda"
    category -- rather than combined with net worth, which didn't crop
    cleanly on the real screen the way it first looked like it might."""
    processed = preprocess(img_bgr)
    text = pytesseract.image_to_string(processed, config=TESS_CONFIG_KDA).strip()
    m = KDA_TRIPLE_REGEX.search(text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def ocr_networth(img_bgr):
    """One player's Net Worth cell -> int, or None."""
    processed = preprocess(img_bgr)
    text = pytesseract.image_to_string(processed, config=TESS_CONFIG_NETWORTH).strip()
    m = re.search(r"\d[\d,]*", text)
    return int(m.group(0).replace(",", "")) if m else None


# ---------------------------------------------------------------------------
# Whole-scoreboard capture.
# ---------------------------------------------------------------------------

def blank_player():
    return {"kills": None, "deaths": None, "assists": None, "networth": None, "damage": None,
            "crops": {}}


def capture_postmatch(config=None, category=None):
    """Grabs and OCRs the calibrated regions for one screen at a time --
    "net-kda" (K/D/A, Net Worth, team score, duration -- all on the Overview
    tab) or "damage" (the Damage tab). category=None reads everything, for
    the standalone manual check at the bottom of this file; the live server
    below always passes one or the other, because reading BOTH in the same
    pass would OCR whichever tab is actually on screen for keys that belong
    to the OTHER tab -- garbage, not None, since a calibrated region has
    real pixels under it regardless of which screen is showing. Returns a
    dict shaped for the operator to then assign names onto -- slot order is
    top-to-bottom, matching the scoreboard's own row order, same as the 5
    fixed player rows Free Fire's roster already assumes per squad."""
    config = config or load_config()
    regions = config.get("regions", {})
    read_net_kda = category in (None, "net-kda")
    read_damage = category in (None, "damage")

    def region_crop(sct, key):
        r = regions.get(key)
        if not r or r.get("w", 0) <= 0 or r.get("h", 0) <= 0:
            return None
        return crop_to_bgr(sct, r)

    result = {
        "team1": {"score": None, "players": [None] * TEAM_SLOTS},
        "team2": {"score": None, "players": [None] * TEAM_SLOTS},
        "duration": None,
    }

    with mss.mss() as sct:
        for team_key in ("team1", "team2"):
            for i in range(TEAM_SLOTS):
                player = blank_player()
                got_anything = False

                if read_net_kda:
                    kda_crop = region_crop(sct, f"dota2_{team_key}_p{i}_kda")
                    if kda_crop is not None:
                        got_anything = True
                        kda = ocr_kda(kda_crop)
                        if kda:
                            player["kills"], player["deaths"], player["assists"] = kda
                        player["crops"]["kda"] = crop_to_data_url(kda_crop)

                    nw_crop = region_crop(sct, f"dota2_{team_key}_p{i}_networth")
                    if nw_crop is not None:
                        got_anything = True
                        player["networth"] = ocr_networth(nw_crop)
                        player["crops"]["networth"] = crop_to_data_url(nw_crop)

                if read_damage:
                    dmg_crop = region_crop(sct, f"dota2_{team_key}_p{i}_damage")
                    if dmg_crop is not None:
                        got_anything = True
                        player["damage"] = ocr_number(dmg_crop)
                        player["crops"]["damage"] = crop_to_data_url(dmg_crop)

                result[team_key]["players"][i] = player if got_anything else None

            if read_net_kda:
                score_crop = region_crop(sct, f"dota2_{team_key}_score")
                if score_crop is not None:
                    result[team_key]["score"] = ocr_number(score_crop)

        if read_net_kda:
            duration_crop = region_crop(sct, "dota2_duration")
            if duration_crop is not None:
                result["duration"] = ocr_duration(duration_crop)

    return result


def merge_capture(old, new):
    """Folds a category-scoped capture into whatever was already captured,
    field by field, so capturing Damage after Net Worth/KDA (or vice versa)
    doesn't blank out the other category's numbers -- each capture only
    populates the fields its own screen actually showed, leaving the rest
    None, and None never overwrites a real value here."""
    if old is None:
        return new
    merged = {
        "team1": {"score": new["team1"]["score"] if new["team1"]["score"] is not None else old["team1"]["score"],
                  "players": []},
        "team2": {"score": new["team2"]["score"] if new["team2"]["score"] is not None else old["team2"]["score"],
                  "players": []},
        "duration": new["duration"] if new["duration"] is not None else old["duration"],
    }
    for team_key in ("team1", "team2"):
        old_players = old[team_key]["players"] or [None] * TEAM_SLOTS
        new_players = new[team_key]["players"] or [None] * TEAM_SLOTS
        for i in range(TEAM_SLOTS):
            op, np = old_players[i], new_players[i]
            if op is None and np is None:
                merged[team_key]["players"].append(None)
                continue
            base = dict(op) if op else blank_player()
            if np:
                for k, v in np.items():
                    if k == "crops":
                        # A dict itself, always present (possibly {}) --
                        # merge its keys individually rather than
                        # replacing the whole thing, or a damage-only
                        # capture's crops={"damage": ...} would wipe out
                        # the kda/networth thumbnails an earlier net-kda
                        # capture already put there.
                        base["crops"] = {**base.get("crops", {}), **v}
                    elif v is not None:
                        base[k] = v
            merged[team_key]["players"].append(base)
    return merged


# ---------------------------------------------------------------------------
# Roster + a tiny local server. Deliberately minimal, not a copy of Free
# Fire's engine -- there is no live loop here to hang this off of, so it
# is its own small asyncio server: hold the pre-match roster (2 teams x 5
# names, entered once before the match the same way Free Fire's operator
# picks names rather than trusting OCR on them), persist it, and answer a
# "capture now" request by running capture_postmatch() and labelling each
# slot with whichever roster name occupies it -- same top-to-bottom slot
# order the calibration boxes were drawn in.
# ---------------------------------------------------------------------------

STATE_PATH = Path(__file__).parent / "dota2_state.json"

PORT = 8766  # separate from freefire (8765) / moba+valorant's port, its own process


def default_state():
    return {
        "roster": {
            "team1": {"name": "", "logo": "", "players": ["", "", "", "", ""], "heroes": ["", "", "", "", ""]},
            "team2": {"name": "", "logo": "", "players": ["", "", "", "", ""], "heroes": ["", "", "", "", ""]},
        },
        "lastCapture": None,
        # Manual, operator-typed series/match score (e.g. "2 - 0") -- there
        # is no OCR source for this (it isn't shown anywhere on the Dota 2
        # post-match screen itself, it's the broadcast's own Bo3/Bo5 tally),
        # so unlike team1Score/team2Score above it is entered by hand and
        # simply displayed as-is, no parsing.
        "matchScore": "",
        # Per-page {elementId: {dx, dy, scale}} nudges from the dashboard's
        # Graphic tab -- same additive-transform mechanism MOBA's
        # postmatch.html already reads via applyGraphicOverrides().
        "graphicOverrides": {},
    }


def load_state():
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            merged = default_state()
            merged.update(data)
            return merged
        except (json.JSONDecodeError, OSError):
            pass
    return default_state()


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def swap_team_sides(state):
    """Interchange everything currently tagged team1 <-> team2 -- same
    full-identity-swap approach as MOBA/Valorant's own swap_team_sides(),
    so the overlay needs no changes at all, it just keeps reading
    team1/team2 as always, now holding the other side's data. matchScore
    (the manually-typed "2 - 0" series tally) is deliberately left alone
    -- it's freeform text, not a team1/team2 pair, so there's nothing safe
    to swap automatically; the operator retypes it if the side swap
    changes who's actually ahead. graphicOverrides also isn't touched --
    those are LEFT/RIGHT screen positions, not team identity, and stay
    correct regardless of which team is currently on which side."""
    roster = state["roster"]
    roster["team1"], roster["team2"] = roster["team2"], roster["team1"]
    cap = state.get("lastCapture")
    if cap:
        cap["team1"], cap["team2"] = cap["team2"], cap["team1"]


def label_with_roster(capture, roster):
    """Merges a capture_postmatch() result with the roster by slot order
    -- capture supplies the numbers, roster supplies who they belong to."""
    for team_key in ("team1", "team2"):
        team_roster = roster.get(team_key, {})
        names = team_roster.get("players", [""] * TEAM_SLOTS)
        capture[team_key]["teamName"] = team_roster.get("name", "")
        for i, player in enumerate(capture[team_key]["players"]):
            if player is not None:
                player["name"] = names[i] if i < len(names) else ""
    return capture


CONNECTED = set()


async def broadcast(msg):
    """Pushes to every connected client (dashboard + the postmatch overlay
    browser source alike) -- the overlay never presses Capture itself, it
    just listens, same push-on-change pattern the MOBA relay already uses."""
    if not CONNECTED:
        return
    payload = json.dumps(msg)
    for ws in list(CONNECTED):
        try:
            await ws.send(payload)
        except Exception:
            pass


def build_overlay_state(state):
    """Shapes server_state into what dota2_postmatch.html actually binds to
    -- summed team Net Worth/Damage (only per-player figures are captured,
    the overlay wants one team-level number) and each player's K/D/A as a
    single string, since the postmatch card has no separate KDA slot."""
    roster = state["roster"]

    def team_block(key):
        r = roster[key]
        return {
            "name": r.get("name", ""),
            "logo": r.get("logo", ""),
            "players": r.get("players", ["", "", "", "", ""]),
            "heroes": r.get("heroes", ["", "", "", "", ""]),
        }

    out = {"team1": team_block("team1"), "team2": team_block("team2"), "postMatch": None,
           "matchScore": state.get("matchScore", ""),
           "graphicOverrides": state.get("graphicOverrides", {})}

    cap = state.get("lastCapture")
    if not cap:
        return out

    def sum_field(team, field):
        vals = [p[field] for p in cap[team]["players"] if p and p.get(field) is not None]
        return sum(vals) if vals else 0

    def player_rows(team):
        rows = []
        for p in cap[team]["players"]:
            if not p:
                rows.append({"damage": None, "networth": None, "kdaText": "", "name": ""})
                continue
            kda = (f"{p['kills']}/{p['deaths']}/{p['assists']}"
                   if p.get("kills") is not None else "")
            rows.append({"damage": p.get("damage"), "networth": p.get("networth"),
                         "kdaText": kda, "name": p.get("name", "")})
        return rows

    d = cap.get("duration") or {}
    duration_str = f"{d.get('minutes', 0)}:{str(d.get('seconds', 0)).zfill(2)}" if d else "00:00"

    out["postMatch"] = {
        "duration": duration_str,
        "team1Score": cap["team1"]["score"],
        "team2Score": cap["team2"]["score"],
        "stats": {
            "team1": {"networth": sum_field("team1", "networth"),
                      "damage": sum_field("team1", "damage"),
                      "kills": cap["team1"]["score"]},
            "team2": {"networth": sum_field("team2", "networth"),
                      "damage": sum_field("team2", "damage"),
                      "kills": cap["team2"]["score"]},
        },
        "players": {"team1": player_rows("team1"), "team2": player_rows("team2")},
    }
    return out


# Shared across every connection (dashboard tab(s), the postmatch overlay,
# the relay uplink) rather than loaded fresh per-connection -- a
# connection-local copy meant two simultaneously-open clients could each
# hold a stale snapshot and silently clobber each other's save on write.
# Loaded once below, mutated in place, persisted on every change.
server_state = load_state()


def reorder_capture_team(cap, team_key, order):
    """order[rosterIndex] = which raw captured row (0-4, on-screen top to
    bottom) belongs at that roster position -- the post-match screen sorts
    by net worth/kills, not by whatever order the roster was entered in, so
    row position alone can't be trusted to mean "this roster player"; the
    operator's per-row "who" pick in the dashboard supplies this mapping.
    None in a slot leaves that roster position empty (no row assigned)."""
    old_players = cap[team_key]["players"] or [None] * TEAM_SLOTS
    cap[team_key]["players"] = [
        (old_players[idx] if idx is not None and 0 <= idx < len(old_players) else None)
        for idx in order
    ]


async def handle_client(websocket):
    CONNECTED.add(websocket)
    try:
        await websocket.send(json.dumps({"type": "state", "data": server_state}))
        await websocket.send(json.dumps({"type": "state_sync", "data": build_overlay_state(server_state)}))
        async for raw in websocket:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if payload.get("type") == "save_roster":
                server_state["roster"] = payload.get("roster", server_state["roster"])
                save_state(server_state)
                await broadcast({"type": "state", "data": server_state})
                await broadcast({"type": "state_sync", "data": build_overlay_state(server_state)})

            elif payload.get("type") == "capture":
                cfg = load_config()
                if not cfg.get("regions"):
                    await websocket.send(json.dumps({
                        "type": "capture_result", "error": "No regions calibrated yet -- run calibrate.py first.",
                    }))
                    continue
                category = payload.get("category")  # "net-kda" | "damage" | None (both)
                fresh = capture_postmatch(cfg, category=category)
                merged = merge_capture(server_state.get("lastCapture"), fresh)
                merged = label_with_roster(merged, server_state["roster"])
                server_state["lastCapture"] = merged
                save_state(server_state)
                await websocket.send(json.dumps({"type": "capture_result", "data": merged}))
                await broadcast({"type": "state_sync", "data": build_overlay_state(server_state)})

            elif payload.get("type") == "swap_sides":
                swap_team_sides(server_state)
                save_state(server_state)
                await broadcast({"type": "state", "data": server_state})
                await broadcast({"type": "state_sync", "data": build_overlay_state(server_state)})

            elif payload.get("type") == "save_manual_score":
                # Overrides for the two score displays: team1Score/team2Score
                # (normally OCR'd off the post-match screen, but the
                # operator can correct a misread here) and matchScore (the
                # Bo3/Bo5 series tally, which has no OCR source at all --
                # always manual).
                if "matchScore" in payload:
                    server_state["matchScore"] = payload["matchScore"]
                if "team1Score" in payload or "team2Score" in payload:
                    cap = server_state.get("lastCapture") or {
                        "team1": {"score": None, "players": [None] * TEAM_SLOTS},
                        "team2": {"score": None, "players": [None] * TEAM_SLOTS},
                        "duration": None,
                    }
                    if "team1Score" in payload:
                        cap["team1"]["score"] = payload["team1Score"]
                    if "team2Score" in payload:
                        cap["team2"]["score"] = payload["team2Score"]
                    server_state["lastCapture"] = cap
                save_state(server_state)
                await broadcast({"type": "state_sync", "data": build_overlay_state(server_state)})

            elif payload.get("type") == "save_graphic_overrides":
                page = payload.get("page", "postmatch")
                server_state.setdefault("graphicOverrides", {})[page] = payload.get("overrides", {})
                save_state(server_state)
                await broadcast({"type": "state_sync", "data": build_overlay_state(server_state)})

            elif payload.get("type") == "map_capture":
                cap = server_state.get("lastCapture")
                if not cap:
                    continue
                # "edits" (optional): operator-corrected kills/deaths/
                # assists/networth/damage per RAW screen row (before
                # reorder) -- the capture table lets the operator fix an
                # OCR misread directly instead of only re-running OCR.
                # Applied first, indexed by the same on-screen row order
                # the table showed them in, then "who" reordering (below)
                # moves the (now-corrected) rows into roster position.
                edits = payload.get("edits") or {}
                for team_key in ("team1", "team2"):
                    team_edits = edits.get(team_key)
                    if team_edits:
                        for i, e in enumerate(team_edits):
                            if not e:
                                continue
                            if i >= len(cap[team_key]["players"]):
                                continue
                            if cap[team_key]["players"][i] is None:
                                cap[team_key]["players"][i] = blank_player()
                            for k, v in e.items():
                                if v is not None:
                                    cap[team_key]["players"][i][k] = v
                for team_key in ("team1", "team2"):
                    order = payload.get(team_key)
                    if order is not None:
                        reorder_capture_team(cap, team_key, order)
                cap = label_with_roster(cap, server_state["roster"])
                server_state["lastCapture"] = cap
                save_state(server_state)
                await websocket.send(json.dumps({"type": "capture_result", "data": cap}))
                await broadcast({"type": "state_sync", "data": build_overlay_state(server_state)})
    finally:
        CONNECTED.discard(websocket)


async def relay_client_loop():
    """Optional, additive: if dota2_config.json has a "relay" section with
    enabled=true, also connect OUT to the cloud relay as a client, so the
    postmatch overlay (running on a different PC -- OBS/vMix box, not the
    OCR PC) and a remote dashboard tab get the same live state. Reuses
    handle_client() unchanged, same pattern as freefire_engine.py's own
    relay_client_loop() -- no-op if relay isn't configured."""
    import websockets
    cfg = load_config()
    relay_cfg = cfg.get("relay", {})
    if not relay_cfg.get("enabled"):
        return
    url = relay_cfg.get("url", "")
    token = relay_cfg.get("token", "")
    if not url or not token:
        print("Relay is enabled in dota2_config.json but 'url'/'token' aren't both set -- skipping relay connection.")
        return
    separator = "&" if "?" in url else "?"
    connect_url = f"{url}{separator}token={token}&page=dota2_engine"
    while True:
        try:
            async with websockets.connect(connect_url) as relay_ws:
                print(f"Connected to cloud relay at {url}")
                await handle_client(relay_ws)
        except Exception as e:
            print(f"Relay connection lost/failed ({e}); retrying in 3s...")
        await asyncio.sleep(3)


async def main():
    import websockets
    print(f"Dota 2 post-match server on ws://localhost:{PORT}")
    print("Open dota2_dashboard.html in a browser.")
    async with websockets.serve(handle_client, "localhost", PORT):
        await asyncio.gather(relay_client_loop(), asyncio.Future())


if __name__ == "__main__":
    import sys
    if "--serve" in sys.argv:
        asyncio.run(main())
    else:
        # Quick manual check without the server: have the Dota 2
        # post-match Overview screen up, run `python dota2_engine.py`,
        # read the printed numbers against the real screen.
        cfg = load_config()
        if not cfg.get("regions"):
            print("No regions calibrated yet. Run calibrate.py first.")
        else:
            data = capture_postmatch(cfg)
            print(json.dumps(data, indent=2))
