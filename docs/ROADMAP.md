# Roadmap: the improvement list

From the AeroGen review (October 2026). Effort is working time for the
change and its tests; "needs you" means an account, a payment, a machine
or a decision only the owner has.

## Done

| Item | Where |
|---|---|
| Sign-in form instead of `prompt()`; signed-in badge, change, log out | `dashboard/dashboard.html` (`showLogin`) |
| Each game's dashboard code isolated: one script block per game, so one that throws stops only itself | `dashboard/dashboard.html` |
| Dependency versions pinned; Python 3.12 pinned | `ocr/requirements.txt`, `ocr/relay/requirements.txt`, `.python-version` |
| All 41 tests in one runner; the stale lobby-strip test fixed | `tools/run_tests.py` |
| Tests on every push (Windows, Python 3.12) -- reporting only | `.github/workflows/tests.yml` |
| The engine follows whichever copy of the client is running | `follow_running_client` |
| No secrets in tracked files (checked: sheet URLs are placeholders; `ocr/.relay_token` never committed) | -- |

## Next (no outside dependency)

| Item | Effort | Notes |
|---|---|---|
| Health strip per OBS source (red/green from the relay's lag) | ½ day | the relay already measures each page's lag |
| Undo + audit log for Swap Teams, Reset All Stats, Clear Matches, New Game; "Restore snapshot" | 1 day | the watchdog already archives every state |
| Stream Deck / Companion hook: push, pull, next slide as plain HTTP calls | ½ day | a small authenticated HTTP endpoint on the engine |
| Logos out of the state: served as files, only URLs in the state | 1 day | ~80% of every state (224 KB of 269 KB) is base64 logos |
| OCR in a worker process, off the WebSocket loop | 1–2 days | |
| Per-device tokens, revocable one at a time (no shared token in every URL) | 1 day | relay + dashboard; the OBS URLs change once |
| Operator vs setup roles in the dashboard | 1 day | needs the per-device tokens above |

## Needs you

| Item | What you do | What I do |
|---|---|---|
| Deploy only after the tests pass | add a GitHub secret; agree to the change | the workflow calls the deploy webhook on success; the push webhook goes |
| Staging relay | a second (small) VM | the config and the deploy target |
| Rotate the three relay tokens | run it on the VM; update the OBS source URLs at a break | the commands and the checklist |
| Private repo (or history scrub) | flip it in GitHub; a deploy key on the VM | update `deploy_webhook.py` for the key |
| A real domain instead of DuckDNS | buy one, point it at Cloudflare and the VM's static IP | update Caddy, the pages' relay default, the docs |
| Packaged engine (PyInstaller, Tesseract bundled) | test it on a fresh PC | the build script and installer |

## Big refactors (between events, behind the test suite)

| Item | Effort |
|---|---|
| Split `freefire_engine.py` (~15,000 lines) into packages, with a typed, versioned state schema | 3–5 days, in stages |
| One shared core for the five engines (socket server, relay client, state saving), one port map | 3–5 days |
| Dashboard split into per-game JS modules with a Vite build | 2–3 days |

## 3D map analysis

The replay `.bin` already gives every player's track in three dimensions
(x, y, z in metres, with vertical speed -- `bin_decode.py`), so a 3D view
needs nothing from the game's own files:

| Step | Effort |
|---|---|
| 3D replay viewer: the map image as the ground, orbit camera, each squad's track at its real height (plane, parachute, rooftops), zone cylinders, kills | 1–2 days |
| Terrain: the ground's height learned from where players walk across many replays, the map image draped over it | +1 day (fills in as more matches are played) |
| Broadcast page: scripted camera moves (the drop, the zone closing, the last fight) | +1 day |
