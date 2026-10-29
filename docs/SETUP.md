# AeroGen setup

The whole system, end to end: the cloud relay, the broadcast PC that runs
the engine, the dashboard, the production PC (OBS / vMix) and the sheets.
No tokens or URLs with secrets belong in this file -- they live on the
machines (`ocr/.relay_token`, the VM's service environment) and in the
dashboard's own settings.

## 1. Cloud relay VM (Ubuntu, currently titanium-relay.duckdns.org)

1. Clone the repo to `/home/ubuntu/MOBA-OCR-Overlay`, make a venv and
   `pip install -r ocr/relay/requirements.txt` (websockets, pinned).
2. **relay.service** (systemd) runs `ocr/relay/server.py` on
   `127.0.0.1:8765`, with `OCR_TOKEN`, `ADMIN_TOKEN` and `VIEWER_TOKEN` in
   its environment. Make each with
   `python -c "import secrets; print(secrets.token_urlsafe(24))"`.
3. **watchdog.service** runs `ocr/relay/watchdog.py` with `VIEWER_TOKEN`,
   `DISCORD_WEBHOOK` and `ARCHIVE_DIR`: it archives every state and alerts
   Discord when the engine drops or goes quiet.
4. **Deploy webhook** runs `ocr/relay/deploy_webhook.py` on
   `127.0.0.1:9001` with `DEPLOY_SECRET`. In GitHub: Settings -> Webhooks ->
   `https://<relay host>/deploy-webhook`, JSON, push events only, the same
   secret. A push to `main` deploys (and restarts the relay) -- push at a
   match break.
5. **Caddy** serves the pages and proxies the sockets. The real Caddyfile
   is on the VM; its shape:

   ```
   titanium-relay.duckdns.org {
     @ws header Connection *Upgrade*
     reverse_proxy @ws 127.0.0.1:8765
     handle /deploy-webhook { reverse_proxy 127.0.0.1:9001 }
     root * /home/ubuntu/MOBA-OCR-Overlay
     file_server
   }
   ```

   `file_server` is what serves `/dashboard/...` and `/overlay/...`; Caddy
   gets the TLS certificate itself.
6. **Paid fonts** (Winner Sans) are not in this public repo: the engine
   uploads them to the VM when it connects. Free ones (Bricolage Grotesque
   for GamerCult) are in `overlay/assets/fonts/` and served like any file.

## 2. Broadcast PC (Windows) -- the engine

1. **Python 3.12** ("Add to PATH" ticked) -- the version the packages are
   pinned for (`.python-version`).
2. **Tesseract** (UB-Mannheim build) in `C:\Program Files\Tesseract-OCR\`,
   eng data kept.
3. `git clone` the repo, then `pip install -r ocr/requirements.txt`
   (pinned: mss, opencv-python, numpy, pillow, pytesseract, websockets,
   keyboard).
4. **Relay token**: the `OCR_TOKEN` value in `ocr/.relay_token` (git-ignored),
   and `"relay": {"enabled": true, "url": "wss://<relay host>"}` in
   `ocr/freefire/freefire_config.json`.
5. **Monitor**: `"monitor"` = the screen showing the spectator view;
   `python -c "import mss; print(mss.mss().monitors)"` lists them.
6. **Calibrate** the screen regions for this PC and resolution with the
   `.bat` files in `ocr/freefire/`: `calibrate_freefire_live.bat` (kill feed,
   side table), `calibrate_freefire_alive_grid.bat`,
   `calibrate_freefire_lobby.bat`, `calibrate_freefire_loadout.bat`.
7. **The game client**: with "Follow the running game" on (Post-Match ->
   Match Result Folder; on by default) the engine finds the running client's
   `Free Fire_64_Data` folder -- its debugger log, `MatchResult_*.log` and
   `SafeZone_*.log` -- itself, whichever copy is open. Off: set the folder by
   hand.
8. Optional DNS fix: `tools\fix-relay-dns.ps1` as Administrator pins the
   relay's name to its IP, for when DuckDNS fails.
9. **Start** with `FIX-AND-START.bat` (or `ocr\start_freefire.bat`) as
   Administrator, so the Num5 loadout hotkey works. The console should show
   "Connected to cloud relay".
10. **Update**: close the engine, run `update.bat`, start it again.
11. **Tests**: `python tools/run_tests.py` runs every test (Python and JS);
    GitHub runs the same on each push (`.github/workflows/tests.yml`).

## 3. Dashboard (any PC, any network)

1. Open `https://<relay host>/dashboard/dashboard.html`, then FREEFIRE MAX.
   Sign in with the `ADMIN_TOKEN` once -- it is kept in that browser; the
   badge at the top says "signed in", with change and log out.
2. **Pre-Match**: Event Setup (title, games, maps, room type, graphics
   package) -> Team Bank -> Roster Import or "Read from the game" -> Lobby
   Join Status.
3. **Live In-Game Ops**: Alive Table Row Assignment, Pop-up Triggers,
   Headshot Hunter / Team Eliminated, Killfeed, Loadout Capture. Squads the
   engine cannot link to a team are listed under the alive grid with a
   "which team?" pick; each pick (and each finished game's result) teaches
   the roster the players' UIDs, so later games link by themselves.
4. **Post-Match**: fetch / commit the result, Overall Standings, Team
   Graphs, Head-to-Head, Booyah Team Stats, sheet pushes.
5. **Graphic**: preview each graphic and push or pull it on air.

## 4. Production PC (OBS / vMix)

Each graphic is a 1920x1080 Browser Source / Web Browser input:
`https://<relay host>/overlay/<page>.html?token=<VIEWER_TOKEN>`, with
"shutdown when not visible" off. The dashboard's "Overlay Browser Source
Links" panel copies the exact URLs. After a push that changes a page,
refresh that source.

- In-game: `freefire_alive_status` (or the one-source `freefire_ingame`),
  `freefire_lobby_status`, `freefire_team_eliminated`,
  `freefire_headshot_hunter`.
- Post-match: `freefire_scoreboard`, `freefire_team_graphs`,
  `freefire_booyah_stats`, `freefire_player_h2h`, `freefire_mvp`,
  `freefire_game_summary`, `freefire_damage_report`, `freefire_map`.
- Sound (the SHUTDOWN call on the elimination card): tick "Control audio
  via OBS" on the in-game source.

## 5. Google Sheets

Deploy the Apps Script web app(s) and paste their URLs into the
dashboard's Broadcast Sheet Push / ARROW settings (`sheetWebhookUrl` /
`arrowWebhookUrl`). "Test push" before going live. Pushed team names that
the sheet cannot place are paired once and remembered
(`settings.resultsNameMap`); check that list when one team's row stays
empty.

## 6. Other games

| Engine | Start | Port |
|---|---|---|
| MOBA | `start_moba.bat` | 8765 |
| Valorant | `start_valorant.bat` | 8765 |
| Dota 2 | -- | 8766 |
| BGMI | `start_bgmi.bat` | 8767 |

Dota 2 and BGMI run alongside Free Fire; MOBA and Valorant share 8765 with
it, so run those on their own.

## Match-day checklist

- The engine is running and the relay is connected.
- The dashboard's Free Fire status says CONNECTED, and the Overlay Sources
  card counts your sources as live (0/3 means none are).
- The calibration crops in OCR Crop Preview line up with the game.
- Event Setup has the right current game and map.
- A test push works for each graphic and for the sheet.
