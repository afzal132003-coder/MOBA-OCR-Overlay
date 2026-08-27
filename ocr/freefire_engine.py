"""
Free Fire Max Broadcast OCR Engine.

Standalone from ocr_engine.py (the MOBA engine) on purpose -- a Free Fire
broadcast day has zero use for MOBA's kills/gold/objectives HUD capture,
turtle/lord detection, or pick/ban draft state, so running that engine
just to get Free Fire's file ingest + live-ops OCR would be pure dead
weight. This has its own config (freefire_config.json), its own state
file (freefire_state.json), and its own WebSocket server -- same default
port (8765) as ocr_engine.py since the two are meant to be run one at a
time, never both, so overlay/dashboard pages don't need different URLs
depending on which engine is up.

Run dashboard/freefire_dashboard.html in a browser and overlay/
freefire_scoreboard.html + overlay/freefire_booyah.html in OBS as
Browser Sources, same as the MOBA setup.
"""

import asyncio
import base64
import difflib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import cv2
import numpy as np
import mss
import pytesseract
import websockets

CONFIG_PATH = Path(__file__).parent / "freefire_config.json"
STATE_PATH = Path(__file__).parent / "freefire_state.json"


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def deep_merge_defaults(loaded, defaults):
    """Fill in any keys missing from a saved state with defaults, recursively.
    Lets the schema grow without breaking an old freefire_state.json."""
    if not isinstance(loaded, dict) or not isinstance(defaults, dict):
        return loaded
    merged = dict(defaults)
    for key, value in loaded.items():
        if key in defaults:
            merged[key] = deep_merge_defaults(value, defaults[key])
        else:
            merged[key] = value
    return merged


config = load_config()

if config.get("tesseract_path"):
    pytesseract.pytesseract.tesseract_cmd = config["tesseract_path"]

# Raw text OCR only for both -- killfeed events and side-table rows both
# need real captured sample text before structured parsing can be written
# against them (same reasoning as the MOBA turtle toast originally).
FREEFIRE_KILLFEED_REGION_KEY = "freefire_killfeed"
FREEFIRE_SIDETABLE_REGION_KEY = "freefire_sidetable"

MAX_OCR_DIMENSION = 1920

connected_clients = set()
connected_pages = {}
ocr_executor = ThreadPoolExecutor(max_workers=2)


def default_state():
    return {
        "settings": {"matchResultFolder": "", "safezoneFolder": ""},
        "currentMatchId": None,
        "currentContext": "",
        "knownContexts": [],
        "currentSafezone": None,
        "matches": [],
        "standings": [],
        # Which view freefire_scoreboard.html shows -- "match" (latest
        # committed match's own results) or "overall" (cumulative
        # standings). freefire_booyah.html has no mode: it always shows
        # the latest committed match's rank-1 team.
        "display": {"scoreboardMode": "match"},
        # Pre-match roster, uploaded once per event as a CSV (team, ign,
        # uid per row). Each player's "loadout" starts empty and is filled
        # in manually for now (active/passive x3/pet/equipment) --
        # automatic icon-recognition needs an icon reference library this
        # doesn't have yet, so this is direct operator entry.
        "roster": {"teams": []},
        # Raw OCR text only, refreshed every capture cycle once the
        # freefire_killfeed/freefire_sidetable regions are calibrated.
        "liveOps": {"killfeedLastText": "", "sidetableLastText": ""},
    }


def load_state():
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            return deep_merge_defaults(loaded, default_state())
        except (json.JSONDecodeError, OSError):
            pass
    return default_state()


def save_state():
    try:
        tmp_path = STATE_PATH.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(server_state, f, indent=2)
        tmp_path.replace(STATE_PATH)
    except OSError:
        pass


server_state = load_state()
locked_fields = set()


# ---------------------------------------------------------------------------
# Low-level capture/OCR helpers (copied from ocr_engine.py's generic image
# pipeline -- these have no MOBA-specific coupling).
# ---------------------------------------------------------------------------

def crop_to_bgr(sct, region):
    shot = sct.grab({
        "left": region["x"], "top": region["y"],
        "width": region["w"], "height": region["h"],
    })
    img = np.array(shot)
    return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)


def crop_to_data_url(img_bgr, scale=3):
    big = cv2.resize(img_bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    ok, buf = cv2.imencode(".png", big)
    if not ok:
        return None
    b64 = base64.b64encode(buf).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _image_to_data(img_bgr):
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    scale = 1.0
    longest = max(h, w)
    if longest > MAX_OCR_DIMENSION:
        scale = MAX_OCR_DIMENSION / longest
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    data = pytesseract.image_to_data(
        gray, config="--oem 1 --psm 11", output_type=pytesseract.Output.DICT,
    )
    if scale != 1.0:
        inv = 1.0 / scale
        for key in ("left", "top", "width", "height"):
            data[key] = [int(round(v * inv)) for v in data[key]]
    return data


def _words_and_lines_from_data(data):
    words = []
    lines = {}
    n = len(data["text"])
    for i in range(n):
        text = data["text"][i].strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1
        if conf < 25:
            continue
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        words.append({"text": text, "x": x, "y": y, "w": w, "h": h, "cx": x + w / 2, "cy": y + h / 2})
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        entry = lines.setdefault(key, {"words": [], "x0": x, "y0": y, "x1": x + w, "y1": y + h})
        entry["words"].append(text)
        entry["x0"] = min(entry["x0"], x)
        entry["y0"] = min(entry["y0"], y)
        entry["x1"] = max(entry["x1"], x + w)
        entry["y1"] = max(entry["y1"], y + h)
    line_list = []
    for entry in lines.values():
        line_list.append({
            "text": " ".join(entry["words"]),
            "x": entry["x0"], "y": entry["y0"],
            "w": entry["x1"] - entry["x0"], "h": entry["y1"] - entry["y0"],
            "cx": (entry["x0"] + entry["x1"]) / 2, "cy": (entry["y0"] + entry["y1"]) / 2,
        })
    return words, line_list


def ocr_lines(img_bgr):
    _, lines = _words_and_lines_from_data(_image_to_data(img_bgr))
    return lines


def ocr_text(img_bgr):
    """Reads whatever text is in the crop, letting Tesseract's own layout
    analysis handle it (no hard black/white digit threshold -- that's
    tuned for tiny plain numbers, not full lines of game-UI text)."""
    return " ".join(line["text"] for line in ocr_lines(img_bgr)).strip()


# ---------------------------------------------------------------------------
# Free Fire Max -- post-match result & safezone log file parsing. Both file
# types are written directly by the game client, not screenshots, so
# there's no OCR involved here -- just reading and parsing plain text.
# Fields are label-delimited ("TeamName:", "Rank:", ...) with variable-width
# space padding depending on name length, so parsing anchors on the labels
# themselves rather than fixed character columns.
#
# Example team block (one per squad, followed by exactly 4 player lines):
#   TeamName: Team Tufan     Rank: 1     KillScore: 48     RankScore: 12     TotalScore: 60
#   NAME: SAIKYO.01          ID: 2281027273     KILL: 7
#
# RankScore already matches Free Fire's own placement-points table
# (1st=12, 2nd=9, 3rd=8, 4th=7, 5th=6, 6th=5, 7th=4, 8th=3, 9th=2, 10th=1,
# 11th/12th=0) -- the game client bakes it into the file, so it's read
# directly rather than recomputed.
# ---------------------------------------------------------------------------

FREEFIRE_TEAM_LINE_REGEX = re.compile(
    r"TeamName:\s*(?P<name>.*?)\s*Rank:\s*(?P<rank>\d+)\s*"
    r"KillScore:\s*(?P<killscore>\d+)\s*RankScore:\s*(?P<rankscore>\d+)\s*"
    r"TotalScore:\s*(?P<totalscore>\d+)\s*$"
)
FREEFIRE_PLAYER_LINE_REGEX = re.compile(
    r"NAME:\s*(?P<name>.*?)\s*ID:\s*(?P<id>\d+)\s*KILL:\s*(?P<kill>\d+)\s*$"
)
FREEFIRE_MATCH_FILENAME_REGEX = re.compile(
    r"^MatchId_(?P<match_id>\d+)_(?P<timestamp>\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})\.log\.txt$",
    re.IGNORECASE,
)
FREEFIRE_SAFEZONE_FILENAME_REGEX = re.compile(
    r"^SafeZone_(?P<match_id>\d+)_(?P<timestamp>\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})\.log\.txt$",
    re.IGNORECASE,
)
FREEFIRE_SAFEZONE_COORD_REGEX = re.compile(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)")


def parse_freefire_match_result(text):
    teams = []
    current = None
    for raw_line in text.split("\n"):
        line = raw_line.lstrip("\ufeff").strip()
        if not line:
            continue
        team_match = FREEFIRE_TEAM_LINE_REGEX.match(line)
        if team_match:
            current = {
                "teamName": team_match.group("name").strip(),
                "rank": int(team_match.group("rank")),
                "killScore": int(team_match.group("killscore")),
                "rankScore": int(team_match.group("rankscore")),
                "totalScore": int(team_match.group("totalscore")),
                "players": [],
            }
            teams.append(current)
            continue
        player_match = FREEFIRE_PLAYER_LINE_REGEX.match(line)
        if player_match and current is not None:
            current["players"].append({
                "name": player_match.group("name").strip(),
                "uid": player_match.group("id"),
                "kills": int(player_match.group("kill")),
            })
    teams.sort(key=lambda t: t["rank"])
    return teams


def parse_freefire_safezone(text):
    text = text.lstrip("\ufeff")
    m = FREEFIRE_SAFEZONE_COORD_REGEX.search(text)
    if not m:
        return None
    return (float(m.group(1)), float(m.group(2)))


def find_freefire_latest_match_file(folder):
    folder_path = Path(folder) if folder else None
    if not folder_path or not folder_path.is_dir():
        return (None, None)
    best = None
    for f in folder_path.iterdir():
        m = FREEFIRE_MATCH_FILENAME_REGEX.match(f.name)
        if m and (best is None or m.group("timestamp") > best[0]):
            best = (m.group("timestamp"), f, m)
    return (best[1], best[2]) if best else (None, None)


def find_freefire_latest_safezone_file(folder, match_id):
    folder_path = Path(folder) if folder else None
    if not folder_path or not folder_path.is_dir():
        return (None, None)
    best = None
    for f in folder_path.iterdir():
        m = FREEFIRE_SAFEZONE_FILENAME_REGEX.match(f.name)
        if not m or m.group("match_id") != str(match_id):
            continue
        if best is None or m.group("timestamp") > best[0]:
            best = (m.group("timestamp"), f, m)
    return (best[1], best[2]) if best else (None, None)


def compute_freefire_standings(matches):
    agg = {}
    for match in matches:
        for team in match.get("teams", []):
            name = (team.get("teamName") or "").strip()
            if not name:
                continue
            row = agg.setdefault(name, {
                "teamName": name, "matches": 0, "totalKills": 0,
                "totalPoints": 0, "bestRank": None,
            })
            row["matches"] += 1
            row["totalKills"] += team.get("killScore", 0)
            row["totalPoints"] += team.get("totalScore", 0)
            rank = team.get("rank")
            if rank is not None and (row["bestRank"] is None or rank < row["bestRank"]):
                row["bestRank"] = rank
    standings = list(agg.values())
    standings.sort(key=lambda r: (-r["totalPoints"], -r["totalKills"]))
    return standings


async def broadcast(message):
    if not connected_clients:
        return
    data = json.dumps(message)
    await asyncio.gather(*[c.send(data) for c in connected_clients], return_exceptions=True)


def presence_counts():
    counts = {}
    for page in connected_pages.values():
        counts[page] = counts.get(page, 0) + 1
    return counts


async def broadcast_presence():
    await broadcast({"type": "presence", "pages": presence_counts()})


async def handle_client(websocket, path=None):
    connected_clients.add(websocket)
    try:
        query = parse_qs(urlparse(websocket.request.path).query)
        page = (query.get("page") or ["unknown"])[0]
    except Exception:
        page = "unknown"
    connected_pages[websocket] = page
    await broadcast_presence()
    await websocket.send(json.dumps({
        "type": "state_sync", "data": server_state, "locked": list(locked_fields),
    }))
    try:
        async for message in websocket:
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                continue

            if payload.get("type") == "manual_update":
                data = payload.get("data", {})
                if "freefire" in data:
                    server_state.update(data["freefire"])
                    server_state["standings"] = compute_freefire_standings(
                        server_state.get("matches", [])
                    )
                for field in payload.get("lock", []):
                    locked_fields.add(field)
                for field in payload.get("unlock", []):
                    locked_fields.discard(field)
                save_state()
                await broadcast({
                    "type": "state_sync", "data": server_state, "locked": list(locked_fields),
                })
            elif payload.get("type") == "freefire_fetch_match":
                # Read-only lookup -- does NOT touch server_state. The
                # dashboard reviews the parsed result and commits it via a
                # normal manual_update (data.freefire.matches) only once
                # the operator confirms it.
                folder = payload.get("folder") or server_state.get("settings", {}).get("matchResultFolder", "")
                try:
                    file_path, name_match = find_freefire_latest_match_file(folder)
                    if not file_path:
                        await websocket.send(json.dumps({
                            "type": "freefire_match_result", "teams": [], "matchId": None,
                            "error": f"No MatchId_*.log.txt found in '{folder}'.",
                        }))
                    else:
                        text = file_path.read_text(encoding="utf-8-sig")
                        teams = parse_freefire_match_result(text)
                        await websocket.send(json.dumps({
                            "type": "freefire_match_result",
                            "matchId": name_match.group("match_id"),
                            "timestamp": name_match.group("timestamp"),
                            "fileName": file_path.name,
                            "teams": teams,
                            "error": None if teams else "File found but no team blocks could be parsed from it.",
                        }))
                except Exception as e:
                    await websocket.send(json.dumps({
                        "type": "freefire_match_result", "teams": [], "matchId": None, "error": str(e),
                    }))
            elif payload.get("type") == "freefire_fetch_safezone":
                folder = payload.get("folder") or server_state.get("settings", {}).get("safezoneFolder", "")
                match_id = payload.get("matchId") or server_state.get("currentMatchId")
                try:
                    if not match_id:
                        await websocket.send(json.dumps({
                            "type": "freefire_safezone_result",
                            "error": "No matchId yet -- fetch a match result first.",
                        }))
                    else:
                        file_path, name_match = find_freefire_latest_safezone_file(folder, match_id)
                        if not file_path:
                            await websocket.send(json.dumps({
                                "type": "freefire_safezone_result",
                                "error": f"No SafeZone_{match_id}_*.log.txt found in '{folder}'.",
                            }))
                        else:
                            text = file_path.read_text(encoding="utf-8-sig")
                            coord = parse_freefire_safezone(text)
                            await websocket.send(json.dumps({
                                "type": "freefire_safezone_result",
                                "matchId": match_id,
                                "x": coord[0] if coord else None,
                                "y": coord[1] if coord else None,
                                "timestamp": name_match.group("timestamp"),
                                "fileName": file_path.name,
                                "error": None if coord else "File found but coordinates couldn't be parsed.",
                            }))
                except Exception as e:
                    await websocket.send(json.dumps({
                        "type": "freefire_safezone_result", "error": str(e),
                    }))
    finally:
        connected_clients.discard(websocket)
        connected_pages.pop(websocket, None)
        await broadcast_presence()


async def ocr_loop():
    """Only captures the two Free Fire live-ops regions -- there's no
    numeric HUD pipeline here at all, unlike ocr_engine.py's REGION_ORDER
    loop, since none of that applies to Free Fire."""
    interval = config.get("poll_interval_seconds", 1.0)
    loop = asyncio.get_running_loop()

    with mss.mss() as sct:
        frame_counter = 0
        while True:
            regions = config.get("regions", {})

            killfeed_region = regions.get(FREEFIRE_KILLFEED_REGION_KEY)
            killfeed_crop = None
            if killfeed_region and killfeed_region.get("w", 0) > 0 and killfeed_region.get("h", 0) > 0:
                killfeed_crop = crop_to_bgr(sct, killfeed_region)

            sidetable_region = regions.get(FREEFIRE_SIDETABLE_REGION_KEY)
            sidetable_crop = None
            if sidetable_region and sidetable_region.get("w", 0) > 0 and sidetable_region.get("h", 0) > 0:
                sidetable_crop = crop_to_bgr(sct, sidetable_region)

            ocr_tasks = []
            text_region_names = []
            for name, crop in (("killfeed", killfeed_crop), ("sidetable", sidetable_crop)):
                if crop is not None:
                    ocr_tasks.append(loop.run_in_executor(ocr_executor, ocr_text, crop))
                    text_region_names.append(name)

            changed = False
            if ocr_tasks:
                results = await asyncio.gather(*ocr_tasks)
                text_results = dict(zip(text_region_names, results))
                killfeed_raw_text = text_results.get("killfeed")
                sidetable_raw_text = text_results.get("sidetable")

                ff_live = server_state["liveOps"]
                if killfeed_raw_text is not None and killfeed_raw_text != ff_live.get("killfeedLastText", ""):
                    ff_live["killfeedLastText"] = killfeed_raw_text
                    changed = True
                if sidetable_raw_text is not None and sidetable_raw_text != ff_live.get("sidetableLastText", ""):
                    ff_live["sidetableLastText"] = sidetable_raw_text
                    changed = True
            else:
                killfeed_raw_text = None
                sidetable_raw_text = None

            if frame_counter % 2 == 0:
                if killfeed_crop is not None:
                    data_url = crop_to_data_url(killfeed_crop)
                    if data_url:
                        await broadcast({
                            "type": "crop_preview", "region": FREEFIRE_KILLFEED_REGION_KEY,
                            "image": data_url, "text": killfeed_raw_text or "",
                        })
                if sidetable_crop is not None:
                    data_url = crop_to_data_url(sidetable_crop)
                    if data_url:
                        await broadcast({
                            "type": "crop_preview", "region": FREEFIRE_SIDETABLE_REGION_KEY,
                            "image": data_url, "text": sidetable_raw_text or "",
                        })

            if changed:
                save_state()
                await broadcast({"type": "state_sync", "data": server_state, "locked": list(locked_fields)})

            frame_counter += 1
            await asyncio.sleep(interval)


async def relay_client_loop():
    """Optional, additive: if freefire_config.json has a "relay" section
    with enabled=true, also connect OUT to the cloud relay as a client, so
    admins/overlays reachable over the internet get the same live state as
    anyone on localhost. Reuses handle_client() unchanged, same as
    ocr_engine.py's version -- no-op if relay isn't configured."""
    relay_cfg = config.get("relay", {})
    if not relay_cfg.get("enabled"):
        return
    url = relay_cfg.get("url", "")
    token = relay_cfg.get("token", "")
    if not url or not token:
        print("Relay is enabled in freefire_config.json but 'url'/'token' aren't both set -- skipping relay connection.")
        return
    separator = "&" if "?" in url else "?"
    connect_url = f"{url}{separator}token={token}"
    while True:
        try:
            async with websockets.connect(connect_url) as relay_ws:
                print(f"Connected to cloud relay at {url}")
                await handle_client(relay_ws)
        except Exception as e:
            print(f"Relay connection lost/failed ({e}); retrying in 3s...")
        await asyncio.sleep(3)


async def main():
    host = config.get("server_host", "localhost")
    port = config.get("server_port", 8765)
    async with websockets.serve(handle_client, host, port):
        print(f"Free Fire OCR engine running at ws://{host}:{port}")
        print("Open overlay/freefire_scoreboard.html and overlay/freefire_booyah.html in OBS")
        print("as Browser Sources, and dashboard/freefire_dashboard.html in a normal browser tab.")
        await asyncio.gather(ocr_loop(), relay_client_loop())


if __name__ == "__main__":
    asyncio.run(main())
