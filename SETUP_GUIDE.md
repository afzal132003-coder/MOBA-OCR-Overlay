# MOBA Broadcast OCR + Overlay — Setup Guide

Folder layout:
```
MOBA-OCR-Overlay/
  overlay/overlay.html      <- Broadcast graphic (add as OBS Browser Source)
  dashboard/dashboard.html  <- Control panel (open in a normal browser tab)
  ocr/moba/ocr_engine.py         <- Captures screen, runs OCR, runs the local server
  ocr/moba/calibrate_hud.py      <- One-time tool to mark where the numbers are on screen
  ocr/moba/config.json           <- All settings + crop coordinates live here
```

How the pieces talk to each other: `ocr_engine.py` starts a local WebSocket
server on `ws://localhost:8765`. `overlay.html` and `dashboard.html` are just
web pages that connect to that address — nothing needs to be uploaded
anywhere, it all stays on your PC.

**Multiple people, multiple PCs (remote team / hosted dashboard)** — if
different team members need admin access from their own machines, or the
overlay sources need to run on a separate production PC than the one doing
OCR, see `ocr/relay/README.md`. That's an optional add-on (everything
above still works local-only and unchanged by default) that connects
everything through a small cloud relay with token-based access instead of
`localhost`.

---

## Step 1 — Install software (one time)

1. **Install Python** (3.10+): https://www.python.org/downloads/
   During install, tick **"Add python.exe to PATH"**.

2. **Install Tesseract OCR for Windows**: download the installer from
   https://github.com/UB-Mannheim/tesseract/wiki and run it (default install
   location is `C:\Program Files\Tesseract-OCR\tesseract.exe`).
   If you install it somewhere else, update `"tesseract_path"` in
   `ocr/moba/config.json` to match.

3. **Install the Python packages.** Open a terminal (PowerShell) in the
   `ocr` folder and run:
   ```
   pip install -r requirements.txt
   ```

That's it for installation — no Node.js, no cloud account, no API keys.

---

## Step 2 — Find out which monitor/window to capture

The OCR reads pixels directly off your screen, so whatever shows your game
spectator feed (a capture card preview window, OBS's own preview, or the
game itself in windowed mode) needs to be visible on a monitor when you
calibrate and when you go live.

To see your monitor list and pick the right index, run in the `ocr` folder:
```
python -c "import mss; print(mss.mss().monitors)"
```
`[0]` is "all monitors combined", `[1]` is your first physical monitor,
`[2]` the second, etc. Put that number in `"monitor"` in `config.json`.

---

## Step 3 — Calibrate the crop regions (finding the X/Y coordinates)

This replaces manually figuring out pixel coordinates.

1. Get your game's spectator HUD on screen and pause on a frame where all
   the numbers are visible (kills, objectives, gold, series score).
2. In the `ocr/moba` folder, double-click **`calibrate_hud.bat`** (or run
   `python calibrate_hud.py`).
3. A screenshot window pops up, one per stat, in this order:
   Team 1 Kills → Team 1 Objectives → Team 1 Gold → Team 2 Gold →
   Team 2 Objectives → Team 2 Kills → Series Score.
4. For each one: **drag a tight box around just that number**, then press
   **ENTER** or **SPACE** to confirm and move to the next. Press **C** to
   skip a stat you don't have.
5. When it finishes, your coordinates are saved into `config.json`
   automatically. Re-run `calibrate_hud.bat` any time the game window moves,
   resizes, or you swap capture sources.

Tip: crop boxes should be tight around the digits only (no icons, no extra
background) — this is what makes OCR reliable.

---

## Step 4 — Start the OCR engine

In the `ocr` folder, double-click **`start_moba.bat`** (or run
`python moba/ocr_engine.py` from inside `ocr/`). Keep this window open — it's both the OCR reader
and the local relay server. You should see:
```
OCR relay server running at ws://localhost:8765
```
If Windows Firewall prompts you, click **Allow access** (it's only
listening on your own PC, nothing goes out to the internet).

---

## Step 5 — Open the dashboard and set up teams

Double-click `dashboard/dashboard.html` to open it in your browser. The
status badge should turn green ("CONNECTED"). From here:

- Enter **Team 1 / Team 2 names** and upload their **logos**, then
  **Save Team Info**.
- Watch the **Live Stats** cards update as OCR reads the screen.
- If OCR misreads something mid-game, tick **Override** on that stat, type
  the correct value, and press **Apply** — OCR will leave that stat alone
  until you untick Override.
- Use the **OCR Crop Preview** panel to double-check the engine is reading
  the right spot on screen (if a preview looks empty or off-target,
  re-run calibration).
- **Reset All Stats to 0** at the start of a new game.

---

## Step 6 — Add the overlay to OBS Studio

1. In OBS, click **+** under Sources → **Browser**.
2. Name it (e.g. "MOBA Overlay") → OK.
3. Set:
   - **Local file**: check this box, then browse to
     `overlay/overlay.html` on your PC.
   - **Width**: 1920, **Height**: 1080 (match your canvas).
   - Leave **Shutdown source when not visible** unchecked so it keeps
     receiving live updates.
4. Click OK. The graphic bar should appear with a transparent background —
   position it at the top of your scene like the reference layout.
5. If it shows "disconnected"/stale numbers, make sure `start_moba.bat`
   (the OCR engine) is already running before you add/refresh the source.

**vMix**: add a **Web Browser** input, point it at the same
`overlay/overlay.html` file path, and enable its transparency/alpha option.

---

## Prematch, In-Game, and Post Match: three separate overlays

The dashboard now has three tabs, and OBS needs three separate Browser
Sources (one per phase of the broadcast) — add each the same way as
before (Step 6), all at 1920x1080:

- `overlay/prematch.html` — pick/ban draft board (bans + picks + timer).
  Cut to this scene during the draft.
- `overlay/overlay.html` — the in-game live HUD bar (unchanged).
  Cut to this once the game starts.
- `overlay/postmatch.html` — the post-game recap graphic. Cut to this
  after a game ends, once you've filled in its tab and clicked
  **Push Post Match to Overlay**.

All three read from the same running `ocr_engine.py`/dashboard, so team
name/logo/series score set in the **In-Game Live Ops** tab automatically
show up on the Prematch and Post Match graphics too.

Both `prematch.html` and `postmatch.html` currently use a generic
navy/gold layout (not your exact PSD positions yet) — send me blank +
filled 1920x1080 PNG exports for each the same way you did `POPUPS.png`,
and I'll rebuild them pixel-accurate the same way the in-game bar was
upgraded. The Post Match graphic also needs a transparent **turtle icon**
PNG (alongside gold/kill/structures) once you have one.

### Hero portraits for Prematch

Drop hero portrait images into `dashboard/assets/heroes/`, one file per
hero, named by lowercasing the hero's name and stripping spaces/punctuation:

- "Miya" → `miya.png`
- "Yu Zhong" → `yuzhong.png`
- "Cecilion" → `cecilion.png`

Whichever name you type into a ban/pick slot in the dashboard is looked
up this way automatically — no need to tell me the full roster in advance,
just make sure filenames follow that pattern.

### Data persistence

`ocr_engine.py` now saves all dashboard state (team info, live stats,
prematch draft, post-match stats) to `ocr/moba/state.json` and reloads it on
startup. Restarting the engine (e.g. after editing `config.json`) no
longer wipes out manually-entered data like the 20 draft slots or 10
players' damage stats.

---

## Moving everything to a new PC

Everything in this project uses paths relative to the project folder
itself — the only thing tied to a specific machine is Tesseract's install
location, and the OCR screen-capture coordinates (since those are pixel
positions on *this* PC's monitor). That means the move is: copy the
folder, reinstall two things, then recalibrate.

1. **Copy the whole `MOBA-OCR-Overlay` folder** to the new PC — via USB
   drive, a shared network folder, or a cloud drive (OneDrive/Google
   Drive/etc). It can go anywhere on the new PC (Desktop, Documents,
   wherever) — it does not need to match this PC's folder path. This
   single copy already includes everything you've built up: the hero
   portrait library, the custom fonts, all the template PNGs, and
   `ocr/moba/state.json` (your saved team names/logos/stats — see below if you
   don't want to carry that over).

2. **On the new PC, install the prerequisites** (same as the original
   Step 1 above):
   - Python 3.10+ from python.org (tick "Add python.exe to PATH")
   - Tesseract OCR from https://github.com/UB-Mannheim/tesseract/wiki
     (default install path is fine)
   - Open a terminal in the copied project's `ocr` folder and run:
     ```
     pip install -r requirements.txt
     ```

3. **Recalibrate — don't skip this.** `ocr/moba/config.json`'s `"monitor"`
   value and every region's x/y/width/height are specific to this PC's
   monitor layout and resolution. On the new PC:
   - Run `python -c "import mss; print(mss.mss().monitors)"` in the `ocr`
     folder to see the new PC's monitor list, and update `"monitor"` in
     `config.json` if it has a different layout than this one.
   - Re-run `calibrate_hud.bat` (in `ocr/moba/`) to redraw all the crop boxes against the new
     PC's actual screen — the saved coordinates from this PC almost
     certainly won't line up on different hardware.

4. **Double-check `tesseract_path`** in `config.json` matches where
   Tesseract actually installed on the new PC (usually the same default
   `C:\Program Files\Tesseract-OCR\tesseract.exe`, but confirm).

5. **Decide about `ocr/moba/state.json`** — it holds the current team
   names/logos/live stats/prematch draft/post-match data. Keep it if you
   want to walk into the new PC with everything already filled in;
   delete it (or just don't copy it) if you'd rather start that PC fresh
   — a new one will be generated automatically on first run.

6. **Run it the same way as before**: `start_moba.bat` (in `ocr/`), then open
   `dashboard/dashboard.html`, `overlay/overlay.html`, `prematch.html`,
   and `postmatch.html` — add the three overlay HTMLs as OBS Browser
   Sources exactly as in Step 6 above, just browsing to their new
   location on the new PC.

---

## Troubleshooting

- **"tesseract is not installed or it's not in your PATH"** — fix the
  `tesseract_path` value in `ocr/moba/config.json` to point at your actual
  `tesseract.exe`.
- **Numbers don't update** — check the crop preview in the dashboard; if
  it's blank or misaligned, re-run `calibrate_hud.bat` (in `ocr/moba/`).
- **Gold shows wrong for values like "11.7k"** — the engine already parses
  a `k` suffix as ×1000; if it's still off, make the crop box tighter so
  only the digits and the `k` are inside it (no `$` sign or icon).
- **Port 8765 already in use** — change `"server_port"` in `config.json`
  to something else (e.g. 8766), and it will be picked up automatically by
  overlay.html/dashboard.html only if you also update the `WS_URL` constant
  near the top of their `<script>` sections to match.
- **Dashboard says DISCONNECTED** — the OCR engine (`start_moba.bat` (in `ocr/`)) must be
  running first; the dashboard and overlay are just viewers of it.
- **Error mentioning `--oem` or "failed loading language"** — your
  Tesseract install is missing the LSTM traineddata. Re-run the UB-Mannheim
  installer and make sure "Additional language data" / the default `eng`
  component is checked, or edit `TESS_CONFIG` in `ocr_engine.py` and drop
  `--oem 1` to fall back to the default engine mode.

### Tuning speed vs. stability

`ocr/moba/config.json` has two settings that trade off latency against
misreads:

- `poll_interval_seconds` — how often each region is re-read. Lower =
  faster updates, more CPU load. Default is `0.35`.
- `debounce_frames` — how many consecutive identical readings are
  required before a value is trusted and pushed to the graphic. Lower =
  faster to react to a real change, but more likely to flash a one-frame
  misread onto the graphic. Default is `2` (so worst-case delay is about
  `poll_interval_seconds * 2`).

If you still see stale/laggy numbers after this update, check Task
Manager while `start_moba.bat` (in `ocr/`) is running — if CPU is pegged, raise
`poll_interval_seconds` slightly (e.g. `0.5`) rather than lowering
`debounce_frames`, since a busy CPU causes more OCR misreads, not fewer.
